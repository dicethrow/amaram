import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const, Shape
from amaranth.hdl.ast import Rose, Stable, Fell, Past, Initial, Array
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
from amaranth.hdl.mem import Memory
from amaranth.hdl.xfrm import DomainRenamer
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active, Settle
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer
from amaranth.lib.cdc import FFSynchronizer
from amaranth.build import Platform
from amaranth.utils import bits_for

from amaranth_boards.ulx3s import ULX3S_85F_Platform

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amlib.utils import Timer

from amtest.boards.ulx3s.common.upload import platform, UploadBase
from amtest.boards.ulx3s.common.clks import add_clock
from amtest.utils import FHDLTestCase, Params

from parameters_standard_sdram import rw_cmds, sdram_cmds

import controller_pin
import controller_readwrite
import controller_refresh


""" 
This file is intended as the user interface, that will allow access to 
persistent data storage in sdram through a fifo interface.
"""

def get_ui_layout(config_params):
	ui_layout = [
		("contains_data",	1,					DIR_FANIN) # not used yet
	]
	return ui_layout

def get_ui_fifo_layout(config_params):
	# todo: add the ability to make the fifo io not always 16bits wide
	fifo_layout = [
		("w_data", 	16,							DIR_FANOUT),
		("w_rdy", 	1,							DIR_FANIN), # ugh, this was the wrong way around
		("w_en", 	1,							DIR_FANOUT),
		("w_level",	bits_for(50 + 1),			DIR_FANIN), 

		("r_data",	16,							DIR_FANIN),
		("r_rdy",	1,							DIR_FANIN),
		("r_en",	1,							DIR_FANOUT),
		("r_level",	bits_for(50 + 1),			DIR_FANIN),
	]
	return fifo_layout


class sdram_n_fifo(Elaboratable):
	
	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		self.ui = Record(get_ui_layout(self.config_params))
		self.pin_ui = Record(controller_pin.get_ui_layout(self.config_params))
		self.ui_fifos = Array(Record(get_ui_fifo_layout(self.config_params)) for _ in range(self.config_params.num_fifos))

		# calculate some relations
		rw_params = self.config_params.rw_params
		self.config_params.global_word_addr_bits = rw_params.BANK_BITS.value + rw_params.ROW_BITS.value + rw_params.COL_BITS.value
		self.config_params.fifo_buf_id_bits = bits_for(self.config_params.num_fifos-1)
		self.config_params.fifo_buf_word_addr_bits = self.config_params.global_word_addr_bits - self.config_params.fifo_buf_id_bits
		# assuming each fifo buf has equal space
		self.config_params.buf_words_available = (1<<self.config_params.fifo_buf_word_addr_bits)-1 #Const((1<<self.config_params.fifo_buf_word_addr_bits)-1, shape=self.config_params.fifo_buf_word_addr_bits) # (1<<20)-1 = 0xfffff

		self.config_params.read_pipeline_clk_delay = 10 # todo - change the logic to get rid of this? or is this needed to avoid overreading?
		self.config_params.num_adjacent_words = self.config_params.burstlen*self.config_params.numbursts

	def elaborate(self, platform = None):
		def get_and_set_up_buffer_fifos():
			# src_fifos[i] is a temporary buffer between data for data going from fpga->sdram
			src_fifos = Array(AsyncFIFOBuffered(
				width=self.config_params.fifo_width, 
				depth=self.config_params.fifo_depth, 
				r_domain="sync", # this is the clock domain used by the sdram 
				w_domain=self.config_params.fifo_write_domains[i] # this is the clock domain used by the user fpga 
				) for i in range(self.config_params.num_fifos))

			# dst_fifos[i] is a temporary buffer between data for data going from sdram->fpga
			dst_fifos = Array(AsyncFIFOBuffered(
				width=self.config_params.fifo_width, 
				depth=self.config_params.fifo_depth, 
				r_domain=self.config_params.fifo_read_domains[i],  # this is the clock domain used by the user fpga 
				w_domain="sync" # this is the clock domain used by the sdram 
				) for i in range(self.config_params.num_fifos))

			# add them as submodules
			for i, (src_fifo, dst_fifo) in enumerate(zip(src_fifos, dst_fifos)):
				m.submodules[f"fifo_{i}_src"] = src_fifo
				m.submodules[f"fifo_{i}_dst"] = dst_fifo

			# and make some control signals 
			fifo_controls = Array(Record([
				("words_stored_in_ram", 			self.config_params.fifo_buf_word_addr_bits),
				("fully_read",						1),
				("request_to_store_data_in_ram", 	1),
				("w_next_addr", 					self.config_params.fifo_buf_word_addr_bits),
				("r_next_addr", 					self.config_params.fifo_buf_word_addr_bits),
			]) for _ in range(self.config_params.num_fifos))

			# now make a 'virtual' fifo for each pair, made by tying the inputs and outputs together
			""" ____________________________________________________
				|self.ui_fifos[<i>]                                       |
				|                                                   |
			-->-|-->-[src_fifo[<i>]]-->- ... ->--[dst_fifo[<i>]]->--|--->--
				|                                                   |
				|___________________________________________________|

			clock domains:

			|_________|        |_______________________|        |___________|
			 <write_i>                sync                         <read_i>
			"""

			for i, (src_fifo, dst_fifo, ui_fifo) in enumerate(zip(src_fifos, dst_fifos, self.ui_fifos)):

				m.d.comb += [ # should it be comb? I think yes, so the rw strobes line up 17mar2022
					src_fifo.w_data.eq(ui_fifo.w_data),
					ui_fifo.w_rdy.eq(src_fifo.w_rdy),
					src_fifo.w_en.eq(ui_fifo.w_en),
					ui_fifo.w_level.eq(src_fifo.w_level),

					ui_fifo.r_data.eq(dst_fifo.r_data),
					ui_fifo.r_rdy.eq(dst_fifo.r_rdy),
					dst_fifo.r_en.eq(ui_fifo.r_en),
					ui_fifo.r_level.eq(dst_fifo.r_level),
				]
			
			return src_fifos, dst_fifos, fifo_controls
	
		def determine_how_much_sdram_is_used_per_fifo():
			# check_if_srcfifo_ready_to_be_writen_to_sdram
			# check how much storage space is currently stored in ram for this fifo. 
			# note that we use it as if it's a circular buffer
			for fifo_control in fifo_controls:
				with m.If(fifo_control.r_next_addr <= fifo_control.w_next_addr):
					m.d.comb += fifo_control.words_stored_in_ram.eq(fifo_control.w_next_addr - fifo_control.r_next_addr)
				with m.Else():
					m.d.comb += fifo_control.words_stored_in_ram.eq(fifo_control.w_next_addr + (self.config_params.buf_words_available - fifo_control.r_next_addr))


		def route_data_through_sdram_or_bypass():
			# route src_fifo to dst_fifo, until it's full, and if the sdram is not storing fifo data
			# to start with, stream data straight from src_fifo to dst_fifo
			for i, (src_fifo, dst_fifo, fifo_control) in enumerate(zip(src_fifos, dst_fifos, fifo_controls)):
				with m.FSM(name=f"fifo_{i}_router_fsm") as fsm:

					# If we can't read anything else from dst_fifo, 
					# and src_fifo is ready to be added to,
					# and we're in BYPASS_SDRAM state,
					# then this fifo is now fully read
					m.d.comb += fifo_control.fully_read.eq(src_fifo.w_rdy & ~dst_fifo.r_rdy & fsm.ongoing("BYPASS_SDRAM"))
					
					src_fifo_w_rdy_monitor = Signal(name=f"src_fifo_w_rdy_monitor_{i}")
					m.d.comb += src_fifo_w_rdy_monitor.eq(src_fifo.w_rdy)

					with m.State("BYPASS_SDRAM"):
						with m.If(fifo_control.words_stored_in_ram != 0):
							# todo: should there be a check done around here that sdram contains at least a burstlen of space?	
							m.next = "USE_SDRAM"
						
						with m.Elif(src_fifo.r_rdy):
							with m.If(dst_fifo.w_rdy):
								# immediately empty src_fifo into dst_fifo
								m.d.comb += [
									src_fifo.r_en.eq(dst_fifo.w_rdy),
									dst_fifo.w_data.eq(src_fifo.r_data),
									dst_fifo.w_en.eq(src_fifo.r_rdy)
								]
							
							with m.Else():
								# dst_fifo is now full, and so src_fifo starts to fill
								# while this is happening, signal that we now want to route data through ram
								m.d.comb += [
									fifo_control.request_to_store_data_in_ram.eq(1),

									# set these to 0 so the traces look cleaner
									src_fifo.r_en.eq(0),
									dst_fifo.w_data.eq(0), 
									dst_fifo.w_en.eq(0)
								]
					
					with m.State("USE_SDRAM"):
						with m.If(fifo_control.words_stored_in_ram == 0):
							m.next = "BYPASS_SDRAM"
										

		
		def get_interface_and_set_up_readwrite_module():
			m.submodules.rw_ctrl = rw_ctrl = controller_readwrite.controller_readwrite(self.config_params)
			rw_ui = Record.like(rw_ctrl.ui)
			# rw_pin_ui = Record.like(rw_ctrl.pin_ui)

			m.d.sync += [
				rw_ui.connect(rw_ctrl.ui),	# rw_ctrl.ui.connect(rw_ui),
				rw_ctrl.pin_ui.connect(self.pin_ui),
				# _pin_ui.connect(self.pin_ui
			]

			return rw_ui#, rw_pin_ui
		
		def route_readback_pipeline_to_dstfifos():
			readback_fifo_id = Signal(shape=self.config_params.fifo_buf_id_bits)
			readback_buf_addr = Signal(shape=self.config_params.fifo_buf_word_addr_bits) # note - not presently used for this fifo interface
			# readback_global_addr = Signal(shape=self.config_params.global_word_addr_bits)
			# assuming the phase thing is accomplished by checking the low bits of the readback addr are zero

			with m.If(rw_ui.r_cipo.read_active):
				m.d.sync += [
					Cat(readback_buf_addr, readback_fifo_id).eq(rw_ui.r_cipo.addr)
				]
				m.d.comb += [ # sync?
					dst_fifos[readback_fifo_id].w_en.eq(1), #rw_ui.r_cipo.read_active),
					dst_fifos[readback_fifo_id].w_data.eq(Past(rw_ui.r_cipo.r_data))
				]
				

		def determine_what_to_do_next():
			""" 
			todo - add in error flag when for src_fifo and sdram and dst_fifo being full

			contains data:		empty:		fifo[a], looping through each until and as they empty, before splitting time to also go through reads
			a					bcd			[a][a][a][a][a][...][a][a]
			ab					cd			[ab][ab][a...][ab][ab][a][a][a][a][a][a]
			abc					d			[abc][abc][...][ab][ab][..][a][a][a]
			abcd				-			[abcd][abcd][...][abc][abc][..][ab][ab][..][a][a][a]

			So the next fifo after a is b, if b is empty is c, if c is empty is d, if d is empty is next_state
			So the next fifo after i is i+1, if i+1 is empty is i+2, ... , if i+n is empty is next_state

			note:
			- with fifos, '.r_level' means 'how many more words are there in the fifo, other than the one currently available on .r_data'
			"""

			fifo_index = Signal(shape=self.config_params.fifo_buf_id_bits)

			

			next_srcfifo_readable_to_sdram = Signal()
			next_srcfifo_index = Signal(shape=self.config_params.fifo_buf_id_bits)

			next_dstfifo_writeable_from_sdram = Signal()
			next_dstfifo_index = Signal(shape=self.config_params.fifo_buf_id_bits)
			
			srcfifo_r_level_enough = Signal()
			ram_wont_overfill = Signal()
			using_ram = Signal()

			ram_wont_overread = Signal()
			dstfifo_w_space_enough = Signal()

			with m.Switch(fifo_index):
				for i in range(self.config_params.num_fifos):
					with m.Case(i):
						# for src_fifo -> sdram
						with m.If(i == (self.config_params.num_fifos-1)):
							m.d.sync += next_srcfifo_index.eq(0) 
						with m.Else():
							m.d.sync += next_srcfifo_index.eq(i+1) 

						s = next_srcfifo_index

						m.d.comb += [
							srcfifo_r_level_enough.eq(src_fifos[s].r_level >= self.config_params.num_adjacent_words),
							ram_wont_overfill.eq(fifo_controls[s].words_stored_in_ram < (self.config_params.buf_words_available - self.config_params.num_adjacent_words)),
							using_ram.eq(fifo_controls[s].request_to_store_data_in_ram | (fifo_controls[s].words_stored_in_ram != 0)),
						
							next_srcfifo_readable_to_sdram.eq(srcfifo_r_level_enough & ram_wont_overfill & using_ram)
						]
						# m.d.sync += next_srcfifo_readable_to_sdram.eq(srcfifo_r_level_enough & ram_wont_overfill & using_ram & src_fifos[s].r_rdy) # is this r_rdy needed?

						# for sdram -> dst_fifo
						with m.If(i == (self.config_params.num_fifos-1)):
							m.d.sync += next_dstfifo_index.eq(0) 
						with m.Else():
							m.d.sync += next_dstfifo_index.eq(i+1) 

						d = next_dstfifo_index
							
						m.d.comb += [
							ram_wont_overread.eq(fifo_controls[d].words_stored_in_ram >= self.config_params.num_adjacent_words),
							dstfifo_w_space_enough.eq((dst_fifos[d].depth - dst_fifos[d].r_level) >= ((self.config_params.num_adjacent_words + self.config_params.read_pipeline_clk_delay))),
						
							next_dstfifo_writeable_from_sdram.eq(ram_wont_overread & dstfifo_w_space_enough)
						]
						# m.d.sync += next_dstfifo_writeable_from_sdram.eq(ram_wont_overread & dstfifo_w_space_enough & dst_fifos[d].w_rdy) # is this w_rdy needed?

			return fifo_index, next_srcfifo_index, next_srcfifo_readable_to_sdram, next_dstfifo_index, next_dstfifo_writeable_from_sdram

		def get_and_set_up_refresh_module():
			m.submodules.refresh_ctrl = refresh_ctrl = controller_refresh.controller_refresh(self.config_params)

			refresh_ui = Record.like(refresh_ctrl.ui)

			m.d.sync += [
				# refresh_ctrl.ui.connect(refresh_ui)
				refresh_ui.connect(refresh_ctrl.ui)
			]

			return refresh_ui

		m = Module()

		ic_timing = self.config_params.ic_timing
		ic_refresh_timing = self.config_params.ic_refresh_timing

		src_fifos, dst_fifos, fifo_controls = get_and_set_up_buffer_fifos()
		determine_how_much_sdram_is_used_per_fifo()
		route_data_through_sdram_or_bypass()

		rw_ui = get_interface_and_set_up_readwrite_module()
		route_readback_pipeline_to_dstfifos()

		fifo_index, next_srcfifo_index, next_srcfifo_readable_to_sdram, next_dstfifo_index, next_dstfifo_writeable_from_sdram = determine_what_to_do_next()

		refresh_ui = get_and_set_up_refresh_module()

		with m.FSM(name="fifo_controller_fsm") as fsm:
			
			burst_index = Signal(shape=bits_for(self.config_params.burstlen-1))
			numburst_index = Signal(shape=bits_for(self.config_params.numbursts-1))

			with m.State("WAITING_FOR_INITIALISE"):
				with m.If(refresh_ui.initialised):
					m.d.sync += [ # comb?
						rw_ui.rw_copi.task.eq(rw_cmds.RW_IDLE)
					]
					# set the reset values here, which are not set elsewhere
					m.d.sync += [fifo_controls[i].w_next_addr.eq(i<<self.config_params.fifo_buf_word_addr_bits) for i in range(self.config_params.num_fifos)]
					m.d.sync += [fifo_controls[i].r_next_addr.eq(i<<self.config_params.fifo_buf_word_addr_bits) for i in range(self.config_params.num_fifos)]
					
					m.d.sync += fifo_index.eq(0)
					m.next = "REFRESH_OR_IDLE"

			with m.State("REFRESH_OR_IDLE"):
				""" 
				do refresh, or wait,
				in case there's nothing to do, perhaps we could later implement some power down/optimisation thing here.

				Note that this needs to be an.. even number of clock cycles (or equal to the burstlen cycles?), if doing a memory access at the moment? so trying REFRESH_OR_IDLE_2 state to see if that fixes a bug
				"""
				with m.If(refresh_ui.request_to_refresh_soon):
					with m.If(~rw_ui.in_progress): # wait for sany reads/writes to finish / banks to go idle, is this needed?
						m.d.sync += refresh_ui.enable_refresh.eq(1) # sync?

				with m.Elif(refresh_ui.refresh_in_progress):
					pass # wait for it to finish, 
					m.d.sync += refresh_ui.enable_refresh.eq(0)

				with m.Else():
					# m.next = "REFRESH_OR_IDLE_2"
					with m.If(next_srcfifo_readable_to_sdram):
						m.next = "WRITE_SRCFIFOS_TO_SDRAM"

					# with m.Elif(~all_dstfifos_written):
					with m.If(next_dstfifo_writeable_from_sdram):
						m.next = "READ_SDRAM_TO_DSTFIFOS"
			# with m.State("REFRESH_OR_IDLE_2"):
				# with m.If(~all_srcfifos_read):
				# with m.If(next_srcfifo_readable_to_sdram):
				# 	m.next = "WRITE_SRCFIFOS_TO_SDRAM"

				# # with m.Elif(~all_dstfifos_written):
				# with m.If(next_dstfifo_writeable_from_sdram):
				# 	m.next = "READ_SDRAM_TO_DSTFIFOS"
									

			with m.State("WRITE_SRCFIFOS_TO_SDRAM"):
				m.d.sync += rw_ui.rw_copi.task.eq(Mux(burst_index==0, rw_cmds.RW_WRITE, rw_cmds.RW_IDLE))

				with m.Switch(fifo_index):
					for i, (src_fifo, dst_fifo, fifo_control) in enumerate(zip(src_fifos, dst_fifos, fifo_controls)):
						# next_i = i + 1 if (i+1)<self.num_fifos else 0
						with m.Case(i):									
							def write_word_address_for_word_at_start_of_burst():
								with m.If(burst_index == 0):
									# todo - which of these is right?
									m.d.sync += rw_ui.rw_copi.addr.eq(Cat(fifo_control.w_next_addr, Const(i, shape=2)))
									# m.d.comb += self.sdram_addr.eq(Cat(i, fifo_control.w_next_addr))
								with m.Else():
									m.d.sync += rw_ui.rw_copi.addr.eq(0)
								
							def write_word_data_for_each_word_in_burst():
								# this assumes that r_rdy has already been dealt with - so put this error transition to catch failure early
								srcfifo_error = Signal()
								with m.If(~src_fifo.r_rdy):
									# m.next = "ERROR"
									m.d.comb += srcfifo_error.eq(1)

								m.d.comb += [
									rw_ui.rw_copi.w_data.eq(src_fifo.r_data),
									src_fifo.r_en.eq(1), 
								]
								m.d.sync += [
									fifo_control.w_next_addr.eq(fifo_control.w_next_addr + 1)
								]

							
												
							write_word_address_for_word_at_start_of_burst()
							write_word_data_for_each_word_in_burst()

							# debug_rw_copi_addr = Signal(name=f"debug_{i}_rw_copi_addr", shape=Shape(rw_ui.rw_copi.addr))
							# debug_rw_copi_w_data = Signal(name=f"debug_{i}_rw_copi_w_data", shape=Shape(rw_ui.rw_copi.w_data))
							# debug_rw_copi_w_rdy = Signal(name=f"debug_{i}_copi_w_rdy")
							# debug_rw_copi_w_en = Signal(name=f"debug_{i}_copi_w_en")
							# m.d.comb += [
							# 	debug_rw_copi_addr.eq(Mux(burst_index == 0, 	Cat(fifo_controls[i].w_next_addr, fifo_index)), 	0),
							# 	debug_rw_copi_w_data.eq(src_fifos[i].r_data),
							# 	src_fifos[i].r_en.eq(1), 
							# 	# debug_rw_copi_w_rdy.eq(src_fifos[fifo_index].r_data),
							# 	# debug_rw_copi_r_en.eq(src_fifos[fifo_index].r_data),
							# ]
						
					

				def when_burst_ends_change_fifo_or_readwrite():
					with m.If((burst_index + 1) == self.config_params.burstlen): # burst finished
						m.d.sync += burst_index.eq(0)

						with m.If((numburst_index + 1) == self.config_params.numbursts): # done several bursts with this fifo, now move on
							m.d.sync += numburst_index.eq(0)

							m.d.sync += fifo_index.eq(next_srcfifo_index) # prepare to do the next fifo

							with m.If(refresh_ui.request_to_refresh_soon):# | all_dstfifos_written):
								m.next = "REFRESH_OR_IDLE"

							with m.Else():
								with m.If(~next_srcfifo_readable_to_sdram):
									m.d.sync += fifo_index.eq(0)

									with m.If(next_dstfifo_writeable_from_sdram):
										m.next = "READ_SDRAM_TO_DSTFIFOS"
									with m.Else():
										m.next = "REFRESH_OR_IDLE"
								
								# with m.Else():
								# 	m.d.sync += fifo_index.eq(next_srcfifo_index) # prepare to do the next fifo

						with m.Else():
							m.d.sync += numburst_index.eq(numburst_index + 1)

					with m.Else():
						m.d.sync += burst_index.eq(burst_index + 1)
						
				when_burst_ends_change_fifo_or_readwrite()

			with m.State("READ_SDRAM_TO_DSTFIFOS"):
			# 	# this state exists to ensure that the dqm pin is kept high for <latency> clock cycles,
			# 	# to prevent driver-driver conflict on the sdram chip
			# 	# m.d.sync += self.pin_ui.dqm.eq(1)
			# 	with m.If(Cat([Past(self.pin_ui.dqm, clocks=1+j) for j in range(3)]) == 0b111):
			# 		m.next = "_READ_SDRAM_TO_DSTFIFOS"
				
			# 	assert 0, "Ensure the switching-from-write-to-read behavior here is as expected. Perhaps ignore this dqm thing for now?"

			# with m.State("_READ_SDRAM_TO_DSTFIFOS"):
				m.d.sync += rw_ui.rw_copi.task.eq(Mux(burst_index==0, rw_cmds.RW_READ, rw_cmds.RW_IDLE))

				with m.Switch(fifo_index):

					for i in range(self.config_params.num_fifos): # for each fifo,
						with m.Case(i):

							def write_word_address_for_word_at_start_of_burst():
								with m.If(burst_index == 0):
									m.d.sync += rw_ui.rw_copi.addr.eq(Cat(fifo_controls[i].r_next_addr, fifo_index))
								with m.Else():
									m.d.sync += rw_ui.rw_copi.addr.eq(0)

							def increment_address_read_counter():
								""" 
								Note - due to the sdram cas delay, the read back data is dealt with elsewhere,
								this just helps to record how much data is still unread in sdram.
								Note that we should only trust this after <cas_delay> cycles.
								"""
								m.d.sync += [
									fifo_controls[i].r_next_addr.eq(fifo_controls[i].r_next_addr + 1)
								]
								

							def when_burst_ends_change_fifo_or_readwrite():
								with m.If((burst_index + 1) == self.config_params.burstlen): # burst finished
									m.d.sync += burst_index.eq(0)

									with m.If((numburst_index + 1) == self.config_params.numbursts): # done several bursts with this fifo, now move on
										m.d.sync += numburst_index.eq(0)

										m.d.sync += fifo_index.eq(next_dstfifo_index) # prepare to do the next fifo

										with m.If(refresh_ui.request_to_refresh_soon):
											m.next = "REFRESH_OR_IDLE"
											
										with m.Else():
											with m.If(~next_dstfifo_writeable_from_sdram):
												m.d.sync += fifo_index.eq(0)

												with m.If(next_srcfifo_readable_to_sdram):
													m.next = "WRITE_SRCFIFOS_TO_SDRAM"
												with m.Else():
													m.next = "REFRESH_OR_IDLE"
																								
											# with m.Else():
											# 	m.d.sync += fifo_index.eq(next_dstfifo_index) # do the next fifo

									with m.Else():
										m.d.sync += numburst_index.eq(numburst_index + 1)

								with m.Else():
									m.d.sync += burst_index.eq(burst_index + 1)

							increment_address_read_counter()
							write_word_address_for_word_at_start_of_burst()
							when_burst_ends_change_fifo_or_readwrite()
			
			with m.State("ERROR"):
				pass


		if isinstance(self.utest, FHDLTestCase):
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")

			# add negedge (sync_n), which is used for the model, and preventing debug flags from being optimised out

			# and for negedge - because clk_edge=neg doesn't work? according to something I read? 
			# I lost the link, but this workaround was recommended
			sync_n = ClockDomain("sync_n", clk_edge="pos")#, local=True)
			# clki_n = ClockDomain("clki_n", clk_edge="neg")
			m.domains += sync_n
			m.d.comb += sync_n.clk.eq(~ClockDomain("sync").clk) # 

			test_id = self.utest.get_test_id()

			if test_id == "fifoInterface_sim_thatWrittenFifos_canBeReadBack":
				assert platform == None, f"This is a time simulation, requiring a platform of None. Unexpected platform status of {platform}"
				
				# if we want to control the flags from sync domain
				for flag in self.utest_params.debug_flags:
					m.d.sync_n += flag.eq(flag) # needed to prevent it being optimised out?

				# if we want to control the flags from sync_n domain
				# for flag in self.utest_params.debug_flags:
				# 	m.d.sync += flag.eq(flag) # needed to prevent it being optimised out?

		return m
		

if __name__ == "__main__":
	""" 
	feb2022 - apr2022

	Adding tests to each file, so I can more easily make 
	changes in order to improve timing performance.

	"""
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	def get_tb_ui_layout(config_params):
		ui_layout = [
			("sdram_pin_ui", controller_pin.get_ui_layout(config_params))
		]
		return ui_layout

	class Testbench(Elaboratable):

		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
			super().__init__()
			self.ui = Record(get_tb_ui_layout(config_params))

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

		def elaborate(self, platform = None):
			m = Module()

			m.submodules.sdram_dut = sdram_dut = DomainRenamer({"sync":"sdram"})(sdram_n_fifo(self.config_params, self.utest_params, utest=self))
			m.d.sdram += [
				self.ui.sdram_pin_ui.connect(sdram_dut.pin_ui) # right way around?
			]
			
			
			m.submodules.model_sdram_rtl = model_sdram_rtl = self.utest_params.model_sdram_rtl(self.config_params, self.utest_params)




			with m.FSM(name="testbench_fsm") as fsm:
				with m.State("INITIAL"):
					write_counter = Signal(shape=bits_for(utest_params.num_fifo_writes))
					m.next = "FILL_FIFOS"
				
				with m.State("FILL_FIFOS"):
					m.d.sync += write_counter.eq(write_counter + 1)
					with m.If(write_counter == utest_params.num_fifo_writes):
						m.next = "READ_BACK_FIFOS"

					for i, each_fifo in enumerate(sdram_dut.ui_fifos):
						m.d.sdram += [
							each_fifo.w_en.eq(each_fifo.w_rdy),
							each_fifo.w_data.eq((i << 4*3)|(each_fifo.w_data & 0xFFF)),
						]
							

					...

				# with m.State("WAIT"): # to confirm that refresh can preserve the data
					# ...

				with m.State("READ_BACK_FIFOS"):
					m.d.sync += write_counter.eq(write_counter - 1)
					with m.If(write_counter == 0):
						m.next = "DONE"

					for i, each_fifo in enumerate(sdram_dut.ui_fifos):
						last_read = Signal(name = f"last_read_{i}")
						m.d.sync += [
							each_fifo.r_en.eq(each_fifo.r_rdy),
							
						]
						with m.If(each_fifo.r_en):
							m.d.sync += last_read.eq(each_fifo.r_data) # not used yet
						
					...

				with m.State("ERROR"): # not used yet
					...
				
				with m.State("DONE"):
					...

			if isinstance(self.utest, FHDLTestCase):
				add_clock(m, "sync")
				# add_clock(m, "sync_1e6")
				test_id = self.utest.get_test_id()
				
				if test_id == "fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack":
					...


			elif isinstance(platform, ULX3S_85F_Platform): 
				# then this is the test that is run when uploaded
				...

			else:
				assert 0

			return m

	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing

		class fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram, model_sdram_rtl

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.burstlen = 8
				config_params.latency = 3
				config_params.numbursts = 2
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params
				config_params.num_fifos = 4
				config_params.fifo_read_domains = ["sync"] * config_params.num_fifos  #[f"read_{i}" for i in range(config_params.num_fifos)]
				config_params.fifo_write_domains =  ["sync"] * config_params.num_fifos  #[[f"write_{i}" for i in range(config_params.num_fifos)]
				config_params.fifo_width = 16
				config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 4 # 64
				config_params.readback_fifo_depth = 50

				utest_params = Params()
				utest_params.model_sdram_rtl = model_sdram_rtl
				utest_params.timeout_period = 20e-6 # seconds
				# utest_params.read_clk_freqs = config_params.num_fifos * [40e6]#[60e6] 
				# utest_params.write_clk_freqs = config_params.num_fifos * [40e6]#[40e6]
				utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 10 # =160 #30 # 50 # 200
				utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
				utest_params.enable_detailed_model_printing = True


				tb = Testbench(config_params, utest_params, utest=self)

				sim = Simulator(dut)

				sim.add_clock(period=1/config_params.clk_freq, domain="sync")

				sdram_model = model_sdram(config_params, utest_params)

				####

				dut = sdram_n_fifo(config_params, utest_params, utest=self)

				sim = Simulator(dut)

				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				# now add the read/write domains
				for i, (r_domain, w_domain) in enumerate(zip(config_params.fifo_read_domains, config_params.fifo_write_domains)):
					sim.add_clock(period=1/utest_params.read_clk_freqs[i], domain=r_domain) # represents faster reads
					sim.add_clock(period=1/utest_params.write_clk_freqs[i], domain=w_domain) # represents slower reads
				


				sdram_model = model_sdram(config_params, utest_params)
				for i in range(4): # num of banks
					sim.add_sync_process(sdram_model.get_readwrite_process_for_bank(bank_id = i, pin_ui=dut.pin_ui))#, domain="sync_n")
				sim.add_sync_process(sdram_model.propagate_i_dq_reads(pin_ui=dut.pin_ui))#, domain="sync_n")

				# all_writes_done = Signal(shape=range(config_params.num_fifos+1), reset=config_params.num_fifos)

				def write_counter_values_to_fifo(fifo, num_writes, fifo_id = 0, write_domain="sync"):
					# todo - add some random waits, to make this more realistic?
					def func():
						yield Active()
						yield Delay(150e-6) # approx when chip init done
						timeout_period = utest_params.timeout_period
						timeout_clks = int(timeout_period * utest_params.write_clk_freqs[fifo_id])
						# yield Passive()
						i = 0
						# for i in range(num_writes):
						timeout = True
						while timeout_clks > 0:
							...
							if (yield fifo.w_rdy):
								data = ((fifo_id << 4*3)|(i & 0xFFF))
								yield fifo.w_data.eq(data)
								yield fifo.w_en.eq(1)

								if i == (num_writes-1):
									timeout = False
									timeout_clks = -1 # to break?
									continue

								print(f"on write {i} ({hex(i)}) out of {num_writes}, {(yield fifo.w_level)}")

								i += 1

							yield Settle()
							yield
							yield Settle()
							timeout_clks -= 1

						if timeout:
							print("Write timeout!")

						yield fifo.w_en.eq(0)

						yield
						yield

					return func
				
				def read_counter_values_from_fifo(fifo, num_reads, fifo_id):
					# todo - add asserts that this reads the expected values (i.e. incrementing)
					def func():
						yield Active()
						yield Delay(150e-6) # approx when chip init done
						yield Delay(30e-6) # aprox when when writes done
						last_read = None
						i = 0
						timeout_period = utest_params.timeout_period
						timeout_clks = int(timeout_period * utest_params.read_clk_freqs[fifo_id])
						timeout = True

						while timeout_clks > 0:
							yield fifo.r_en.eq((yield fifo.r_rdy))

							yield
							timeout_clks -= 1

							if i == (num_reads-1): # right?
								timeout = False
								timeout_clks = -1 # end
							
							# check if still ready? this fixed a bug where the same value was read twice
							if (yield fifo.r_rdy):
								data = (yield fifo.r_data)
								colors = ["red", "green", "yellow", "blue"]
								status = f"fifo={hex(fifo_id)}, read={hex(i)}: {hex(data)}"
								status += f" delta={data-last_read}" if (last_read != None) else ""
								cprint(status, colors[fifo_id])
								last_read = data
								i += 1
							
						
						if timeout:
							print("Read timeout!")
						
						yield
						yield

					return func
					

				for i in range(config_params.num_fifos):
					sim.add_sync_process(write_counter_values_to_fifo(
						dut.ui_fifos[i], utest_params.num_fifo_writes, i), 
						domain=config_params.fifo_write_domains[i])
					
					sim.add_sync_process(read_counter_values_from_fifo(
						dut.ui_fifos[i], utest_params.num_fifo_writes, i), 
						domain=config_params.fifo_read_domains[i])

				def start_readback_pipeline():
					# this should be done close to where the copi_dq and cipo_dq split
					yield Passive()
					while True:
						yield dut.pin_ui.rw_cipo.addr.eq((dut.pin_ui.rw_copi.addr))
						yield dut.pin_ui.rw_cipo.read_active.eq((dut.pin_ui.rw_copi.read_active))
						yield Settle()
						yield
						yield Settle()
				sim.add_sync_process(start_readback_pipeline)

				def run_for_longer():
					yield Active()
					yield Delay(300e-6)
				sim.add_process(run_for_longer)
				
				with sim.write_vcd(
					f"{current_filename}_{self.get_test_id()}.vcd"):
					sim.run()

		# class fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack(FHDLTestCase):
		# 	def test_sim(self):
		# 		from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
		# 		from model_sdram import model_sdram, model_sdram_rtl

		# 		config_params = Params()
		# 		config_params.clk_freq = 143e6
		# 		config_params.burstlen = 8
		# 		config_params.latency = 3
		# 		config_params.numbursts = 2
		# 		config_params.ic_timing = ic_timing
		# 		config_params.ic_refresh_timing = ic_refresh_timing
		# 		config_params.rw_params = rw_params
		# 		config_params.num_fifos = 4
		# 		config_params.fifo_read_domains = [f"read_{i}" for i in range(config_params.num_fifos)]
		# 		config_params.fifo_write_domains = [f"write_{i}" for i in range(config_params.num_fifos)]
		# 		config_params.fifo_width = 16
		# 		config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 4 # 64
		# 		config_params.readback_fifo_depth = 50

		# 		utest_params = Params()
		# 		utest_params.model_sdram_rtl = model_sdram_rtl
		# 		utest_params.timeout_period = 20e-6 # seconds
		# 		utest_params.read_clk_freqs = config_params.num_fifos * [40e6]#[60e6] 
		# 		utest_params.write_clk_freqs = config_params.num_fifos * [40e6]#[40e6]
		# 		utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 10 # =160 #30 # 50 # 200
		# 		utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
		# 		utest_params.enable_detailed_model_printing = True

		# 		dut = sdram_n_fifo(config_params, utest_params, utest=self)

		# 		sim = Simulator(dut)

		# 		sim.add_clock(period=1/config_params.clk_freq, domain="sync")
		# 		# now add the read/write domains
		# 		for i, (r_domain, w_domain) in enumerate(zip(config_params.fifo_read_domains, config_params.fifo_write_domains)):
		# 			sim.add_clock(period=1/utest_params.read_clk_freqs[i], domain=r_domain) # represents faster reads
		# 			sim.add_clock(period=1/utest_params.write_clk_freqs[i], domain=w_domain) # represents slower reads
				


		# 		sdram_model = model_sdram(config_params, utest_params)
		# 		for i in range(4): # num of banks
		# 			sim.add_sync_process(sdram_model.get_readwrite_process_for_bank(bank_id = i, pin_ui=dut.pin_ui))#, domain="sync_n")
		# 		sim.add_sync_process(sdram_model.propagate_i_dq_reads(pin_ui=dut.pin_ui))#, domain="sync_n")

		# 		# all_writes_done = Signal(shape=range(config_params.num_fifos+1), reset=config_params.num_fifos)

		# 		def write_counter_values_to_fifo(fifo, num_writes, fifo_id = 0, write_domain="sync"):
		# 			# todo - add some random waits, to make this more realistic?
		# 			def func():
		# 				yield Active()
		# 				yield Delay(150e-6) # approx when chip init done
		# 				timeout_period = utest_params.timeout_period
		# 				timeout_clks = int(timeout_period * utest_params.write_clk_freqs[fifo_id])
		# 				# yield Passive()
		# 				i = 0
		# 				# for i in range(num_writes):
		# 				timeout = True
		# 				while timeout_clks > 0:
		# 					...
		# 					if (yield fifo.w_rdy):
		# 						data = ((fifo_id << 4*3)|(i & 0xFFF))
		# 						yield fifo.w_data.eq(data)
		# 						yield fifo.w_en.eq(1)

		# 						if i == (num_writes-1):
		# 							timeout = False
		# 							timeout_clks = -1 # to break?
		# 							continue

		# 						print(f"on write {i} ({hex(i)}) out of {num_writes}, {(yield fifo.w_level)}")

		# 						i += 1

		# 					yield Settle()
		# 					yield
		# 					yield Settle()
		# 					timeout_clks -= 1

		# 				if timeout:
		# 					print("Write timeout!")

		# 				yield fifo.w_en.eq(0)

		# 				yield
		# 				yield

		# 			return func
				
		# 		def read_counter_values_from_fifo(fifo, num_reads, fifo_id):
		# 			# todo - add asserts that this reads the expected values (i.e. incrementing)
		# 			def func():
		# 				yield Active()
		# 				yield Delay(150e-6) # approx when chip init done
		# 				yield Delay(30e-6) # aprox when when writes done
		# 				last_read = None
		# 				i = 0
		# 				timeout_period = utest_params.timeout_period
		# 				timeout_clks = int(timeout_period * utest_params.read_clk_freqs[fifo_id])
		# 				timeout = True

		# 				while timeout_clks > 0:
		# 					yield fifo.r_en.eq((yield fifo.r_rdy))

		# 					yield
		# 					timeout_clks -= 1

		# 					if i == (num_reads-1): # right?
		# 						timeout = False
		# 						timeout_clks = -1 # end
							
		# 					# check if still ready? this fixed a bug where the same value was read twice
		# 					if (yield fifo.r_rdy):
		# 						data = (yield fifo.r_data)
		# 						colors = ["red", "green", "yellow", "blue"]
		# 						status = f"fifo={hex(fifo_id)}, read={hex(i)}: {hex(data)}"
		# 						status += f" delta={data-last_read}" if (last_read != None) else ""
		# 						cprint(status, colors[fifo_id])
		# 						last_read = data
		# 						i += 1
							
						
		# 				if timeout:
		# 					print("Read timeout!")
						
		# 				yield
		# 				yield

		# 				# #########
		# 				# stop = False
		# 				# while True:
		# 				# 	if timeout_clks == 0:
		# 				# 		print("Read timeout!")
		# 				# 		break
							

		# 				# 	if (yield fifo.r_rdy):# and ((yield all_writes_done)==0):
		# 				# 		yield fifo.r_en.eq(1)

		# 				# 		yield
		# 				# 		timeout_clks -= 1
								
		# 				# 		if i == (num_reads-1): # right?
		# 				# 			stop = True
							
		# 				# 		# check if still ready? this fixed a bug where the same value was read twice
		# 				# 		if (yield fifo.r_rdy):
		# 				# 			data = (yield fifo.r_data)
		# 				# 			colors = ["red", "green", "yellow", "blue"]
		# 				# 			status = f"fifo={hex(fifo_id)}, read={hex(i)}: {hex(data)}"
		# 				# 			status += f" delta={data-last_read}" if (last_read != None) else ""
		# 				# 			cprint(status, colors[fifo_id])
		# 				# 			last_read = data
		# 				# 			i += 1
							
		# 				# 	else:
		# 				# 		yield fifo.r_en.eq(0)
		# 				# 		yield
		# 				# 		timeout_clks -= 1
							
		# 				# 	if stop:
		# 				# 		break
						
		# 				# # some end clocks
		# 				# for _ in range(10):
		# 				# 	yield

		# 			return func
					

		# 		for i in range(config_params.num_fifos):
		# 			sim.add_sync_process(write_counter_values_to_fifo(
		# 				dut.ui_fifos[i], utest_params.num_fifo_writes, i), 
		# 				domain=config_params.fifo_write_domains[i])
					
		# 			sim.add_sync_process(read_counter_values_from_fifo(
		# 				dut.ui_fifos[i], utest_params.num_fifo_writes, i), 
		# 				domain=config_params.fifo_read_domains[i])

		# 		def start_readback_pipeline():
		# 			# this should be done close to where the copi_dq and cipo_dq split
		# 			yield Passive()
		# 			while True:
		# 				yield dut.pin_ui.rw_cipo.addr.eq((dut.pin_ui.rw_copi.addr))
		# 				yield dut.pin_ui.rw_cipo.read_active.eq((dut.pin_ui.rw_copi.read_active))
		# 				yield Settle()
		# 				yield
		# 				yield Settle()
		# 		sim.add_sync_process(start_readback_pipeline)

		# 		def run_for_longer():
		# 			yield Active()
		# 			yield Delay(300e-6)
		# 		sim.add_process(run_for_longer)
				
		# 		with sim.write_vcd(
		# 			f"{current_filename}_{self.get_test_id()}.vcd"):
		# 			sim.run()

	if args.action in ["generate", "simulate"]:
		# now run each FHDLTestCase above 
		import unittest
		sys.argv[1:] = [] # so the args used for this file don't interfere with unittest
		unittest.main()

	else: # upload
		class Upload(UploadBase):
			def __init__(self):
				super().__init__(sync_mode="sync_and_143e6_sdram_from_pll")
				
			def elaborate(self, platform = None):
				m, platform = super().elaborate(platform) 

				# from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing
				# from model_sdram import model_sdram

				# config_params = Params()
				# config_params.clk_freq = 143e6
				# config_params.ic_timing = ic_timing
				# config_params.ic_refresh_timing = ic_refresh_timing

				# m.submodules.tb = tb = DomainRenamer({"sync":"sdram"})(Testbench(config_params))

				# ui = Record.like(tb.ui)
				# m.d.sync += ui.connect(tb.ui)

				# def start_on_left_button():
				# 	start = Signal.like(self.i_buttons.left)
				# 	m.d.sync += [
				# 		start.eq(self.i_buttons.left),
				# 		ui.tb_fanout_flags.trigger.eq(Rose(start))
				# 	]

				# def reset_on_right_button():
				# 	# don't manually route the reset - do this, 
				# 	# otherwise, if Records are used, they will oscillate, as can't be reset_less
				# 	m.d.sync += ResetSignal("sync").eq(self.i_buttons.right) 

				# def display_on_leds():
				# 	m.d.comb += self.leds.eq(Cat([
				# 		ui.tb_fanin_flags.in_normal_operation,
				# 		ui.tb_fanin_flags.in_requesting_refresh,
				# 		ui.tb_fanin_flags.in_performing_refresh,
				# 		self.i_buttons.right,  		# led indicates that the start button was pressed
				# 		self.i_buttons.left			# led indicates that the reset button was pressed
				# 	]))

				# start_on_left_button()
				# reset_on_right_button()
				# display_on_leds()

				# return DomainRenamer("sdram")(m)
				return m
		
		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")