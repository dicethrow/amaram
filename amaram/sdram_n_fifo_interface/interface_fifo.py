import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import (Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, 
	ResetSignal, Cat, Const, Array)
from amaranth.hdl.ast import Rose, Stable, Fell, Past, Initial
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
from amaranth.hdl.mem import Memory
from amaranth.hdl.xfrm import DomainRenamer
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active, Settle
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import SyncFIFO, AsyncFIFO#, AsyncFIFOBuffered, SyncFIFOBuffered
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

from controller_pin import controller_pin
from Delayer import Delayer

from parameters_standard_sdram import sdram_cmds, rw_cmds

from controller_refresh import controller_refresh
from controller_pin import controller_pin
from controller_readwrite import controller_readwrite

from _module_interfaces import controller_pin_interfaces, sdram_fifo_interfaces, controller_readwrite_interfaces


""" 
7apr2022

interface_fifo

This is meant as a smaller/simpler single-fifo version of the n-fifo interface.


assumptions:
- that the ui fifos are not used with a clock domain faster than the sdram clock freq

"""

class interface_fifo(Elaboratable):
	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):

		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		# user interface
		self.ui_fifo = Record(sdram_fifo_interfaces.get_ui_fifo_layout(self.config_params))

		# put in constructor so we can access in simulation processes
		self.readwriter = controller_readwrite(self.config_params)
		self.refresher = controller_refresh(self.config_params)
		self.pin_ctrl = controller_pin(self.config_params, self.utest_params)

		# calculate some relations
		rw_params = self.config_params.rw_params
		self.config_params.global_word_addr_bits = rw_params.BANK_BITS.value + rw_params.ROW_BITS.value + rw_params.COL_BITS.value
		self.config_params.fifo_buf_word_addr_bits = self.config_params.global_word_addr_bits
		# assuming each fifo buf has equal space
		self.config_params.buf_words_available = (1<<self.config_params.fifo_buf_word_addr_bits)-1 #Const((1<<self.config_params.fifo_buf_word_addr_bits)-1, shape=self.config_params.fifo_buf_word_addr_bits) # (1<<20)-1 = 0xfffff

		# self.config_params.read_pipeline_clk_delay = 10 # todo - change the logic to get rid of this? or is this needed to avoid overreading?
		self.config_params.num_adjacent_words = self.config_params.burstlen*self.config_params.numbursts
	
	def get_sim_sync_processes(self):
		for process, domain in self.pin_ctrl.get_sim_sync_processes():
			yield process, domain
	
	def elaborate(self, platform = None):
		
		m = Module()

		m.submodules.readwriter = self.readwriter
		m.submodules.refresher = self.refresher
		m.submodules.pin_ctrl = self.pin_ctrl

		##### get and set up buffer fifos ############################################333
		# src_fifo is a temporary buffer between data for data going from fpga->sdram
		# link a small (and slow) async fifo to a larger (and faster) sync fifo, for 
		# an effectively faster and larger fifo
		""" ____________________________________________________
			|self.ui_fifos                                      |
			|                                                   |
		-->-|-->-[src_fifo]-->-      ...      ->--[dst_fifo]->--|--->--
			|                                                   |
			|___________________________________________________|

		clock domains:

		|_________|        |_______________________|        |___________|
		  <write>                  sync                         <read>
		"""

		async_src_fifo = AsyncFIFO(
			width=self.config_params.fifo_width, 
			depth=4, 
			r_domain="sync", # this is the clock domain used by the sdram 
			w_domain=self.config_params.fifo_write_domain) # this is the clock domain used by the user fpga 
		src_fifo = SyncFIFO(
			width=self.config_params.fifo_width, 
			depth=self.config_params.fifo_depth)

		# dst_fifo is a temporary buffer between data for data going from sdram->fpga
		dst_fifo = SyncFIFO(
			width=self.config_params.fifo_width, 
			depth=self.config_params.fifo_depth)
		async_dst_fifo = AsyncFIFO(
			width=self.config_params.fifo_width, 
			depth=4, 
			r_domain=self.config_params.fifo_read_domain,  # this is the clock domain used by the user fpga 
			w_domain="sync")  # this is the clock domain used by the sdram 

		# add them as submodules
		m.submodules["fifo_src"] = src_fifo
		m.submodules["fifo_dst"] = dst_fifo
		m.submodules["fifo_src_async"] = async_src_fifo
		m.submodules["fifo_dst_async"] = async_dst_fifo


		def connect_fifo_interface(outer_ui, inner_src, inner_dst):
			# directly link the user interface to the outer async fifos
			A = outer_ui
			B = inner_src
			C = inner_dst

			statements = [
				B.w_data.eq(A.w_data),
				A.w_rdy.eq(B.w_rdy & self.refresher.ui.initialised), # add this so it doesn't start unitl ram ready
				B.w_en.eq(A.w_en),
				
				A.r_data.eq(C.r_data),
				A.r_rdy.eq(C.r_rdy),
				C.r_en.eq(A.r_en),
				# A.r_level.eq(C.r_level),
				
			]
			return statements
	
		def chain_two_fifos_together(src, dst):
			statements = [
				dst.w_data.eq(src.r_data),
				src.r_en.eq(src.r_rdy & dst.w_rdy),
				dst.w_en.eq(src.r_en)
			]
			return statements

		m.d.comb += connect_fifo_interface(outer_ui = self.ui_fifo, inner_src = async_src_fifo, inner_dst = async_dst_fifo)
		m.d.comb += chain_two_fifos_together(src = async_src_fifo, dst = src_fifo)
		m.d.comb += chain_two_fifos_together(src = dst_fifo, dst = async_dst_fifo)


		# and make some control signals 
		fifo_control = Record([
			("words_stored_in_ram", 			self.config_params.fifo_buf_word_addr_bits),
			("fully_read",						1),
			("request_to_store_data_in_ram", 	1),
			("w_next_addr", 					self.config_params.fifo_buf_word_addr_bits),
			("r_next_addr", 					self.config_params.fifo_buf_word_addr_bits),
		])		
		

		# check how much storage space is currently stored in ram for this fifo. 
		# note that we use it as if it's a circular buffer
		with m.If(fifo_control.r_next_addr <= fifo_control.w_next_addr):
			m.d.sync += fifo_control.words_stored_in_ram.eq(fifo_control.w_next_addr - fifo_control.r_next_addr)
		with m.Else():
			m.d.sync += fifo_control.words_stored_in_ram.eq(fifo_control.w_next_addr + (self.config_params.buf_words_available - fifo_control.r_next_addr))
		
		##### route src_fifo data to fill dst_fifo until it's full, then store overflow in sdram ###############################
		
		rw_ui = Record(controller_readwrite_interfaces.get_ui_layout(self.config_params))
		m.d.sync += rw_ui.connect(self.readwriter.ui)

		with m.FSM(name=f"fifo_router_fsm") as fsm:
			# If we can't read anything else from dst_fifo, 
			# and src_fifo is ready to be added to,
			# and we're in BYPASS_SDRAM state,
			# then this fifo is now fully read
			m.d.comb += fifo_control.fully_read.eq(src_fifo.w_rdy & ~dst_fifo.r_rdy & fsm.ongoing("BYPASS_SDRAM"))
			
			# src_fifo_w_rdy_monitor = Signal())
			# m.d.comb += src_fifo_w_rdy_monitor.eq(src_fifo.w_rdy)


			""" 
			ideas for how to improve:
			- 
			"""

			with m.State("BYPASS_SDRAM"):
				with m.If(fifo_control.words_stored_in_ram != 0):
					# todo: should there be a check done around here that sdram contains at least a burstlen of space?	
					m.next = "USE_SDRAM"
				
				with m.Elif(src_fifo.r_rdy):
					with m.If(dst_fifo.w_rdy):
						# immediately empty src_fifo into dst_fifo
						m.d.comb += [
							src_fifo.r_en.eq(dst_fifo.w_rdy & src_fifo.r_rdy), # new - needed? 7apr2022
							dst_fifo.w_data.eq(src_fifo.r_data),
							dst_fifo.w_en.eq(src_fifo.r_en)
						]
					
					with m.Else():
						# dst_fifo is now full, and so src_fifo starts to fill
						# while this is happening, signal that we now want to route data through ram
						m.d.comb += [
							fifo_control.request_to_store_data_in_ram.eq(1), # should this only happen if enough data is available?

							# set these to 0 so the traces look cleaner
							src_fifo.r_en.eq(0),
							dst_fifo.w_data.eq(0), 
							dst_fifo.w_en.eq(0)
						]
			
			with m.State("USE_SDRAM"):
				with m.If(fifo_control.words_stored_in_ram == 0):
					# so in this scenario, assume that we have up to num_adjacent_words data in src_fifo.
					# let's wait until we can gently attach it to dst_fifo.
					with m.If(~rw_ui.r_cipo.read_active & (dst_fifo.r_level < self.config_params.num_adjacent_words)): # wait for the final read_active. Assume there's just this last one.
						m.next = "BYPASS_SDRAM"

				# route readback pipeline from sdram to dst_fifo
				with m.If(rw_ui.r_cipo.read_active):
					m.d.comb += [
						dst_fifo.w_en.eq(1),
						dst_fifo.w_data.eq(rw_ui.r_cipo.r_data)
					]
	
		
		##### connect readwriter and refresher through pincontroller to sdram chip/model ##############################
		m.d.sync += [
			self.pin_ctrl.ui.bus_is_refresh_not_readwrite.eq(self.refresher.ui.enable_refresh | self.refresher.ui.refresh_in_progress),
			self.refresher.controller_pin_ui.connect(self.pin_ctrl.ui.refresh),
			self.readwriter.controller_pin_ui.connect(self.pin_ctrl.ui.readwrite)
		]

		##### determine what to do next ##############################


		next_srcfifo_readable_to_sdram = Signal()
		srcfifo_r_level_high_enough_to_burstread = Signal()
		too_busy_to_refresh = Signal() # 7apr2022 - how to deal with this case better?
		ram_wont_overfill = Signal()
		using_ram = Signal()

		next_dstfifo_writeable_from_sdram = Signal()
		ram_wont_overread = Signal()
		dstfifo_w_space_enough = Signal()

		m.d.sync += [
			srcfifo_r_level_high_enough_to_burstread.eq(src_fifo.r_level > (self.config_params.num_adjacent_words)),
			ram_wont_overfill.eq(fifo_control.words_stored_in_ram < (self.config_params.buf_words_available - self.config_params.num_adjacent_words)),
			using_ram.eq(fifo_control.request_to_store_data_in_ram | (fifo_control.words_stored_in_ram != 0)),
		
		# ]
		# m.d.comb += [
			next_srcfifo_readable_to_sdram.eq(srcfifo_r_level_high_enough_to_burstread & ram_wont_overfill & using_ram)
		]

		m.d.sync += [
			ram_wont_overread.eq(fifo_control.words_stored_in_ram >= self.config_params.num_adjacent_words),
			dstfifo_w_space_enough.eq((dst_fifo.depth - dst_fifo.r_level) >= ((2*self.config_params.num_adjacent_words + self.config_params.read_pipeline_clk_delay))),
		# ]
		# m.d.comb += [
			next_dstfifo_writeable_from_sdram.eq(ram_wont_overread & dstfifo_w_space_enough)
		]

		# m.d.comb += [
		# 	too_busy_to_refresh.eq(src_fifo.r_rdy | dst_fifo.w_rdy)
		# ]

		##### implement the fsm to control the sdram chip/model ##############################
		with m.FSM(name="fifo_controller_fsm") as fsm:
			
			burst_index = Signal(shape=bits_for(self.config_params.burstlen-1))
			numburst_index = Signal(shape=bits_for(self.config_params.numbursts-1))

			with m.State("WAITING_FOR_INITIALISE"):
				with m.If(self.refresher.ui.initialised):
					m.d.sync += [ # comb?
						rw_ui.rw_copi.task.eq(rw_cmds.RW_IDLE)
					]
					# set the reset values here, which are not set elsewhere
					m.d.sync += [
						fifo_control.w_next_addr.eq(0),#i<<self.config_params.fifo_buf_word_addr_bits),
						fifo_control.r_next_addr.eq(0),#i<<self.config_params.fifo_buf_word_addr_bits)
					]					
					m.next = "REFRESH_OR_IDLE"

			with m.State("REFRESH_OR_IDLE"):
				""" 
				do refresh, or wait,
				in case there's nothing to do, perhaps we could later implement some power down/optimisation thing here.

				Note that this needs to be an.. even number of clock cycles (or equal to the burstlen cycles?), if doing a memory access at the moment? so trying REFRESH_OR_IDLE_2 state to see if that fixes a bug
				"""
				with m.If(self.refresher.ui.request_to_refresh_soon):
					with m.If(~rw_ui.in_progress): # wait for sany reads/writes to finish / banks to go idle, is this needed?
						m.d.sync += self.refresher.ui.enable_refresh.eq(1) # sync?

				with m.Elif(self.refresher.ui.refresh_in_progress):
					pass # wait for it to finish, 
					m.d.sync += self.refresher.ui.enable_refresh.eq(0)

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

				def write_word_address_for_word_at_start_of_burst():
					with m.If(burst_index == 0):
						# todo - which of these is right?
						m.d.sync += rw_ui.rw_copi.addr.eq(fifo_control.w_next_addr) # or comb
					with m.Else():
						m.d.sync += rw_ui.rw_copi.addr.eq(0)
					
				def write_word_data_for_each_word_in_burst():
					# this assumes that r_rdy has already been dealt with - so put this error transition to catch failure early
					srcfifo_error = Signal()
					with m.If(~src_fifo.r_rdy):
						# m.next = "ERROR"
						m.d.comb += srcfifo_error.eq(1)

					m.d.comb += [
						src_fifo.r_en.eq(1), 
					]

					m.d.sync += [
						rw_ui.rw_copi.w_data.eq(src_fifo.r_data),
						fifo_control.w_next_addr.eq(fifo_control.w_next_addr + 1)
					]
									
				write_word_address_for_word_at_start_of_burst()
				write_word_data_for_each_word_in_burst()
					

				def when_burst_ends_change_fifo_or_readwrite():
					with m.If((burst_index + 1) == self.config_params.burstlen): # burst finished
						m.d.sync += burst_index.eq(0)

						with m.If((numburst_index + 1) == self.config_params.numbursts): # done several bursts with this fifo, now move on
							m.d.sync += numburst_index.eq(0)

							# m.d.sync += fifo_index.eq(next_srcfifo_index) # prepare to do the next fifo

							with m.If(self.refresher.ui.request_to_refresh_soon & ~too_busy_to_refresh):# | all_dstfifos_written):
								m.next = "REFRESH_OR_IDLE"

							with m.Else():
								with m.If(~next_srcfifo_readable_to_sdram):
									# m.d.sync += fifo_index.eq(0)

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

				def write_word_address_for_word_at_start_of_burst():
					with m.If(burst_index == 0):
						m.d.sync += rw_ui.rw_copi.addr.eq(fifo_control.r_next_addr)
					with m.Else():
						m.d.sync += rw_ui.rw_copi.addr.eq(0)

				def increment_address_read_counter():
					""" 
					Note - due to the sdram cas delay, the read back data is dealt with elsewhere,
					this just helps to record how much data is still unread in sdram.
					Note that we should only trust this after <cas_delay> cycles.
					"""
					m.d.sync += [
						fifo_control.r_next_addr.eq(fifo_control.r_next_addr + 1)
					]
					

				def when_burst_ends_change_fifo_or_readwrite():
					with m.If((burst_index + 1) == self.config_params.burstlen): # burst finished
						m.d.sync += burst_index.eq(0)

						with m.If((numburst_index + 1) == self.config_params.numbursts): # done several bursts with this fifo, now move on
							m.d.sync += numburst_index.eq(0)

							# m.d.sync += fifo_index.eq(next_dstfifo_index) # prepare to do the next fifo

							with m.If(self.refresher.ui.request_to_refresh_soon & ~too_busy_to_refresh):
								m.next = "REFRESH_OR_IDLE"
								
							with m.Else():
								with m.If(~next_dstfifo_writeable_from_sdram):
									# m.d.sync += fifo_index.eq(0)

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

		# if we want to control the flags from sync domain
		if hasattr(self.utest_params, "debug_flags"):
			for flag in self.utest_params.debug_flags:
				m.d.nsync += flag.eq(flag) # needed to prevent it being optimised out?
			
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

	class Testbench(Elaboratable):
		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
			super().__init__()

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

			# put in constructor so we can access in simulation processes
			self.interface_fifo = interface_fifo(self.config_params, self.utest_params)

		def get_sim_sync_processes(self):
			for process, domain in self.interface_fifo.get_sim_sync_processes():
				yield process, domain

			test_id = self.utest.get_test_id()
			if test_id == "fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack":
				def read_counter_values_from_fifo(fifo, num_reads):
					# todo - add asserts that this reads the expected values (i.e. incrementing)
					def func():
						yield Active()
						yield Delay(150e-6) # approx when chip init done
						yield Delay(30e-6) # aprox when when writes done
						last_read = None
						i = 0
						timeout_period = self.utest_params.timeout_period
						timeout_clks = int(timeout_period * self.utest_params.read_clk_freq)
						not_timeout = False

						while timeout_clks > 0:
							# yield fifo.r_en.eq((yield fifo.r_rdy))

							yield
							yield fifo.r_en.eq((yield fifo.r_rdy))
							timeout_clks -= 1

							if i == (num_reads-1): # right?
								not_timeout = True
								timeout_clks = -1 # end
							
							print(f"i is {i} and test is {num_reads-1}")
							
							# check if still ready? this fixed a bug where the same value was read twice
							if (yield fifo.r_rdy):
								data = (yield fifo.r_data)
								status = f"fifo: read={hex(i)}: data={hex(data)}"
								status += f" delta={data-last_read}" if (last_read != None) else ""
								cprint(status, "green")
								last_read = data
								i += 1
							
							
						
						if not not_timeout:
							print("Read timeout!")
						
						# yield fifo.r_en.eq((yield fifo.r_rdy))
						yield

						# yield a few more times
						for x in range(20):
							yield

					return func

				def write_counter_values_to_fifo(fifo, num_writes):
					# todo - add some random waits, to make this more realistic?
					def func():
						yield Active()
						yield Delay(150e-6) # approx when chip init done
						timeout_period = self.utest_params.timeout_period
						timeout_clks = int(timeout_period * self.utest_params.write_clk_freq)
						# yield Passive()
						i = 0
						# for i in range(num_writes):
						timeout = True
						while timeout_clks > 0:
							...
							if (yield fifo.w_rdy):
								# data = ((fifo_id << 4*3)|(i & 0xFFF))
								data = i & 0xFFFF # assuming 16bit wide
								yield fifo.w_data.eq(data)
								yield fifo.w_en.eq(1)

								print(f"on write {i} ({hex(i)}) out of {num_writes}, {(yield fifo.w_level)}")

								if i == (num_writes-1):
									timeout = False
									timeout_clks = -1 # to break?
									continue

								i += 1

							yield
							timeout_clks -= 1

						if timeout:
							print("Write timeout!")

						yield # so the final value is propagated
						yield fifo.w_en.eq(0)

						yield
						yield

					return func

				process = write_counter_values_to_fifo(self.interface_fifo.ui_fifo, self.utest_params.num_fifo_writes)
				domain = self.config_params.fifo_write_domain
				yield process, domain

				process = read_counter_values_from_fifo(self.interface_fifo.ui_fifo, self.utest_params.num_fifo_writes)
				domain = self.config_params.fifo_read_domain
				yield process, domain

		def elaborate(self, platform = None):
			m = Module()

			m.submodules.interface_fifo = self.interface_fifo

			# test_id = self.utest.get_test_id()
			# if (test_id == "fifoInterfaceTb_sim_thatWrittenFifosUsingFSM_canBeReadBack"):
			if True:

				fifo_domain = self.config_params.fifo_write_domain

				with m.FSM(name="testbench_fsm", domain=fifo_domain) as fsm:

					write_counter = Signal.like(self.interface_fifo.ui_fifo.w_data)
					read_value = Signal.like(write_counter)

					with m.State("INITIAL"):
						
						with m.If(self.interface_fifo.ui_fifo.w_rdy):
							m.next = "FILL_FIFOS"
					
					with m.State("FILL_FIFOS"):
						
						with m.If(write_counter == self.utest_params.num_fifo_writes):
							m.d[fifo_domain] += self.interface_fifo.ui_fifo.w_en.eq(0)
							m.next = "READ_BACK_FIFOS"

						with m.Else():
							m.d[fifo_domain] += self.interface_fifo.ui_fifo.w_en.eq(self.interface_fifo.ui_fifo.w_rdy)
							m.d[fifo_domain] += write_counter.eq(write_counter + 1)
							with m.If(self.interface_fifo.ui_fifo.w_rdy):
								m.d[fifo_domain] += self.interface_fifo.ui_fifo.w_data.eq(write_counter)

						...

					# with m.State("WAIT"): # to confirm that refresh can preserve the data
						# ...

					with m.State("READ_BACK_FIFOS"):
						m.d[fifo_domain] += write_counter.eq(write_counter - 1)

						with m.If(write_counter == 0):
							m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(0),
							m.next = "DONE"

						with m.Else():
							m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(self.interface_fifo.ui_fifo.r_rdy),# & (self.interface_fifo.ui_fifo.r_level != 1)),
							with m.If(self.interface_fifo.ui_fifo.r_rdy):
								read_value.eq(self.interface_fifo.ui_fifo.r_data)

						...

					with m.State("ERROR"): # not used yet
						...
					
					with m.State("DONE"):
						...

			return m

	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing
		if False:
			class fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack(FHDLTestCase):
				def test_sim(self):
					from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
								
					config_params = Params()
					config_params.ic_timing = ic_timing
					config_params.ic_refresh_timing = ic_refresh_timing
					config_params.rw_params = rw_params
					config_params.clk_freq = 143e6
					config_params.burstlen = 8
					config_params.latency = 3
					config_params.numbursts = 2 
					# config_params.num_fifos = 4
					config_params.fifo_read_domain = "read"
					config_params.fifo_write_domain = "write"
					config_params.fifo_width = 16
					config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 4 # 64
					config_params.read_pipeline_clk_delay = 10 # ??
					# config_params.readback_fifo_depth = 50

					utest_params = Params()
					utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
					utest_params.use_sdram_model = True
					utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
					utest_params.timeout_period = 20e-6 # seconds
					utest_params.read_clk_freq = 16e6 #[60e6] 
					utest_params.write_clk_freq = 40e6 #[40e6]
					utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 10 # =160 #30 # 50 # 200
					utest_params.enable_detailed_model_printing = True

					tb = Testbench(config_params, utest_params, utest=self)

					sim = Simulator(tb)

					sim.add_clock(period=1/config_params.clk_freq, 		domain="sync")
					sim.add_clock(period=1/utest_params.read_clk_freq, 	domain=config_params.fifo_read_domain)
					sim.add_clock(period=1/utest_params.write_clk_freq, domain=config_params.fifo_write_domain)
				
					for process, domain in tb.get_sim_sync_processes():
						print(process, domain)
						sim.add_sync_process(process, domain=domain)
					
					def run_for_longer():
						yield Active()
						yield Delay(300e-6)
					sim.add_process(run_for_longer)

					with sim.write_vcd(
						f"{current_filename}_{self.get_test_id()}.vcd"):
						sim.run()
		if True:
			class fifoInterfaceTb_sim_thatWrittenFifosUsingFSM_canBeReadBack(FHDLTestCase):
				def test_sim(self):
					from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
								
					config_params = Params()
					config_params.ic_timing = ic_timing
					config_params.ic_refresh_timing = ic_refresh_timing
					config_params.rw_params = rw_params
					config_params.clk_freq = 143e6
					config_params.burstlen = 8
					config_params.latency = 3
					config_params.numbursts = 2 
					# config_params.num_fifos = 4
					config_params.fifo_read_domain = "fifo"
					config_params.fifo_write_domain = config_params.fifo_read_domain
					config_params.fifo_width = 16
					config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 4 # 64
					config_params.read_pipeline_clk_delay = 10 # ??
					# config_params.readback_fifo_depth = 50

					utest_params = Params()
					utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
					utest_params.use_sdram_model = True
					utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
					utest_params.timeout_period = 20e-6 # seconds
					utest_params.read_clk_freq = 40e6 #[60e6] 
					utest_params.write_clk_freq = utest_params.read_clk_freq
					utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 50 # =160 #30 # 50 # 200
					utest_params.enable_detailed_model_printing = True

					tb = Testbench(config_params, utest_params, utest=self)

					sim = Simulator(tb)
					sim.add_clock(period=1/config_params.clk_freq, 		domain="sync")
					sim.add_clock(period=1/utest_params.read_clk_freq,	domain=config_params.fifo_read_domain)

					for process, domain in tb.get_sim_sync_processes():
						print(process, domain)
						sim.add_sync_process(process, domain=domain)
					
					def run_for_longer():
						yield Active()
						yield Delay(300e-6)
					sim.add_process(run_for_longer)

					with sim.write_vcd(
						f"{current_filename}_{self.get_test_id()}.vcd"):
						sim.run()
	
	
	if args.action in ["generate", "simulate"]:
		# now run each FHDLTestCase above 
		import unittest
		sys.argv[1:] = [] # so the args used for this file don't interfere with unittest
		unittest.main()
	
	else: # upload
		...
		class Upload(UploadBase):
			def elaborate(self, platform = None):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params

				config_params = Params()
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params
				config_params.clk_freq = 143e6
				config_params.burstlen = 8
				config_params.latency = 3
				config_params.numbursts = 2 
				# config_params.num_fifos = 4
				config_params.fifo_read_domain = "sync"
				config_params.fifo_write_domain = config_params.fifo_read_domain
				config_params.fifo_width = 16
				config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 2#4 # 64
				config_params.read_pipeline_clk_delay = 10 # ??
				config_params.sync_mode = "sync_and_143e6_sdram_from_pll"
				self.config_params = config_params

				utest_params = Params()
				utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
				utest_params.use_sdram_model = False
				utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
				utest_params.timeout_period = 20e-6 # seconds
				utest_params.read_clk_freq = 16e6 #[60e6] 
				utest_params.write_clk_freq = 40e6 #[40e6]
				utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 10 # =160 #30 # 50 # 200
				utest_params.enable_detailed_model_printing = True

				m = super().elaborate(platform) 

				m.submodules.tb = tb = DomainRenamer("sdram")(Testbench(config_params, utest_params))
				# m.submodules.tb = tb = Testbench(config_params, utest_params)

				return m

		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")
