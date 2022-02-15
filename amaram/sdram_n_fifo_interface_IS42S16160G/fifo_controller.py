
from amaranth.hdl import (Memory, ClockDomain, ResetSignal,
	ClockSignal, Elaboratable, Module, Signal, Mux, Cat,
	Const, C, Shape, Array, Record, Value)
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
from amaranth.utils import bits_for

# build/upload
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
# for testing only?
# from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.sim import Simulator, Delay, Tick, Passive, Active

import struct, enum
import numpy as np

from .base import rw_cmds, sdram_base

class fifo_controller(sdram_base):
	""" 
	todo: improve the name
	This should be the thing that holds the fifos, and implements the
	sdram control logic, using some capabilities enabled by other modules.

	todo:
		- make the src_fifo immediately fill the dst_fifo, then use sdram for 'overflow'
		- make the src_fifo roll through the available memory like a circular buffer, rather than just using the starting region
	"""
	def __init__(self, core, num_fifos = 4):
		super().__init__()
		self.core = core

		self.num_fifos = num_fifos
		self.fifo_index = Signal(shape=bits_for(num_fifos-1)) # to empty, fill each fifo

		# let's go for burstlen of 8words and numbursts of 2
		self.burstlen = 8 #4 # 2 is not enough to interleave with 4 banks, 4 is enough but 8 has fewer needed commands
		self.burst_index = Signal(shape=bits_for(self.burstlen-1))
		self.numbursts = 2 # per fifo
		self.numburst_index = Signal(shape=bits_for(self.numbursts-1))

		# this is the unique location of a word in the sdram chip
		# other than here, the fifocontroller class does not care about bank/row/col etc
		self.global_word_addr_bits = self.core.col_bits + self.core.row_bits + self.core.bank_bits
		burstlength_bits = bits_for(self.burstlen-1) # 1bit for burstlen of 2
		self.fifo_id_bits = bits_for(self.num_fifos-1) # 2bits for 4fifos

		# assuming each fifo has equal space
		self.fifo_buf_word_addr_bits = self.global_word_addr_bits - self.fifo_id_bits
		self.buf_words_available = Const((1<<self.fifo_buf_word_addr_bits)-1, shape=self.fifo_buf_word_addr_bits) # (1<<20)-1 = 0xfffff
		self.read_pipeline_clk_delay = 3 # is this realistic? so it's cas_delay + ??? ?

		# interface for the read_write_controller
		self.sdram_addr = Signal(self.global_word_addr_bits)
		self.sdram_data = Signal(16)

		self.readback_sdram_pipeline_active = Signal()
		self.readback_sdram_addr = Signal(self.global_word_addr_bits) # will be coincident with valid data
		self.readback_sdram_data = Signal(16)

		self.src_fifos = Array(AsyncFIFOBuffered(width=16, depth=self.core.depth, r_domain="sdram", w_domain=f"write_{i}") for i in range(self.num_fifos))
		self.dst_fifos = Array(AsyncFIFOBuffered(width=16, depth=self.core.depth, r_domain=f"read_{i}", w_domain="sdram") for i in range(self.num_fifos))
		self.fully_read = Array(Signal(reset=0) for i in range(self.num_fifos))

	def link_fifo_ui(self, m, ui_fifos):
		"""
		- core <--> rest of fpga interface 
		- Called from the top level, to link the fifo user interface to the underlying fifos.
		
		- this function ignores the read part of src_fifo, and the write part of dst_fifo

            ____________________________________________________
            |ui_fifo[<i>]                                        |
            |                                                    |
        -->-|-->-[src_fifo[<i>]]-->- ... ->--[dst_fifo[<i>]]->--|--->--
            |                                                    |
            |___________________________________________________|

        clock domains:

        |_________|        |_______________________|        |___________|
            write_<i>                sdram                        read_<i>


		"""

		assert len(ui_fifos) == 4, "fixed to 4 fifos as of 11nov2021"

		for i, ui_fifo in enumerate(ui_fifos):
			src_fifo = self.src_fifos[i]
			dst_fifo = self.dst_fifos[i]

			m.d.comb += [
				src_fifo.w_data.eq(ui_fifo.w_data),
				ui_fifo.w_rdy.eq(src_fifo.w_rdy),
				src_fifo.w_en.eq(ui_fifo.w_en),
				ui_fifo.w_level.eq(src_fifo.w_level),

				ui_fifo.r_data.eq(dst_fifo.r_data),
				ui_fifo.r_rdy.eq(dst_fifo.r_rdy),
				dst_fifo.r_en.eq(ui_fifo.r_en),
				ui_fifo.r_level.eq(dst_fifo.r_level), # plus src_fifo?
				# local_fifo_ui.r_en.eq(self.fifo_controller.dst_fifos[i].r_en)

				ui_fifo.fully_read.eq(self.fully_read[i])
			]


	def elaborate(self, platform = None):
		"""
		- ideas:
			- if fifo_en is on, then increment a counter, which would indicate the address of that data
			- have a fsm for each fifo?
				a. fifo_in directly filling fifo_out
				b. fifo_in writing to sdram, sdram writing to fifo_out

			- have a 'ram word addr', 
				- set by the fifo controller from [buf_id, read_ctr]
				- read by the read_write controller for [row, col, bank_id]

		
			- so the fifo_controller outputs:
				- sets req_rw to read or write
					- sets addr

					if write:
					- sets write_data

					if read:
						(note, the read addr must not exceed the written address)
					- gets read_ready; is Past(task, clks=xxx)==read
					- gets read_addr; is Past(addr, clks=xxx), so data and the addr line up
						(note - this is to make the code simpler, we only will use the buf_id bits, then put it in the dst_fifo)
					- gets read_data

				- valid or not




			for the scenario where a burst write happens, then later a burst read happens:
				stage 1. The src_fifo is routed straight to the dst_fifo, until the dst_fifo fills up, let's say for data 0x0 to data 0x40
				stage 2. The src_fifo is routed to sdram, lets say from data 0x41 to 0xXX, until the burst is done
				stage 3. The dst_fifo is constantly topped up from sdram, as it is read
				stage 4. Again the src_fifo is routed straight to dst_fifo, until it is emptied
			__________________________________________
		
			domain: sdram

			Assuming fifos have more than <burstlen>*<numbursts> data available to be read.

			...][	fifo a	]								 													 [	fifo a	]					 																 [	fifo b	][...	
			...][read/write	]			 					 													 [read/write]					 													 			 [read/write][...
			...][	addr a0	]								 													 [	addr a8	]					 																 [	addr b0	][...
			...][	data a0	][	data a1	][	data a2	][	data a3	][	data a4	][	data a5	][	data a6	][	data a7	][	data a8	][	data a9	][	data a10][	data a11][	data a12][	data a13][	data a14][	data a15][	data b0	][...

				|<--------------------------------------------------------------------------------------------->|	burstlen = 8, for now

			"""
		super().elaborate(platform)

		# all_srcfifos_read = Signal()
		next_srcfifo_readable_to_sdram = Signal()
		next_srcfifo_index = Signal(shape=self.fifo_index.shape())
		
		# all_dstfifos_written = Signal()
		next_dstfifo_writeable_from_sdram = Signal()
		next_dstfifo_index = Signal(shape=self.fifo_index.shape())

		# fifos add to submodules
		for i, (src_fifo, dst_fifo) in enumerate(zip(self.src_fifos, self.dst_fifos)):
			self.m.submodules[f"fifo_{i}_src"] = src_fifo
			self.m.submodules[f"fifo_{i}_dst"] = dst_fifo


		cmds = rw_cmds

		per_fifo_values = [
			# ("ram_stores_data", 1), # is this if w_next_addr_16W != r_next_addr_16W?
			("words_stored_in_ram", self.fifo_buf_word_addr_bits),
			("request_to_store_data_in_ram", 1),
			("w_next_addr", self.fifo_buf_word_addr_bits), # can we assume this resets to zero? # todo - remove magic number of 15
			("r_next_addr", self.fifo_buf_word_addr_bits) # can we assume this resets to zero?
		]
		self.fifo_controls = Array(Record(per_fifo_values) for _ in self.src_fifos)


		def route_data_through_sdram_or_bypass():
			# for i in ?
			# route src_fifo to dst_fifo, until it's full, and if the sdram is not storing fifo data
			# to start with, stream data straight from src_fifo to dst_fifo
			for i, (src_fifo, dst_fifo) in enumerate(zip(self.src_fifos, self.dst_fifos)):
				with self.m.FSM(domain="sdram", name=f"fifo_{i}_router_fsm"):
					with self.m.State("BYPASS_SDRAM"): 
						with self.m.If(self.fifo_controls[i].words_stored_in_ram != 0):
							self.m.next = "USE_SDRAM"
						
						with self.m.Elif(src_fifo.r_rdy):
							with self.m.If(dst_fifo.w_rdy):
								# route it from src_fifo
								self.m.d.comb += [
									src_fifo.r_en.eq(dst_fifo.w_rdy),
									dst_fifo.w_data.eq(src_fifo.r_data),
									dst_fifo.w_en.eq(src_fifo.r_rdy)
								]
							with self.m.Else():
								# then we can't store the data in src_fifo, so we try to store it in ram.
								self.m.d.comb += [
									self.fifo_controls[i].request_to_store_data_in_ram.eq(1),

									src_fifo.r_en.eq(0),
									dst_fifo.w_data.eq(0), # so the traces look cleaner
									dst_fifo.w_en.eq(0)
								]
						
						self.m.d.comb += self.fully_read[i].eq(src_fifo.w_rdy & ~dst_fifo.r_rdy)

					with self.m.State("USE_SDRAM"):
						with self.m.If(self.fifo_controls[i].words_stored_in_ram == 0):
							self.m.next = "BYPASS_SDRAM"

						with self.m.Elif(dst_fifo.w_rdy): # todo - should this be 'containe at least a burstlen of space'?
							pass


		def handle_read_pipeline():
			readback_fifo_id = Signal(shape=self.fifo_index.shape())
			readback_buf_addr = Signal(shape=self.fifo_buf_word_addr_bits)
			readback_addr = Signal(shape=self.global_word_addr_bits)

			readback_phase = Signal(shape=bits_for(self.burstlen-1), reset=0)

			with self.m.If(self.readback_sdram_pipeline_active):
				self.m.d.sdram += [
					readback_phase.eq(Mux(readback_phase < (self.burstlen -1), readback_phase + 1, 0))
				]
				with self.m.If(readback_phase == 0):
					self.m.d.comb += readback_addr.eq(self.readback_sdram_addr)
				with self.m.Else():
					# self.m.d.comb += readback_addr.eq(readback_addr)
					pass


				self.m.d.comb += [
					# readback_addr.eq(Past(self.readback_sdram_addr, clocks=burstlen-i, domain="sdram")),
					# readback_addr.eq(Past(self.readback_sdram_addr, clocks=self.read_pipeline_clk_delay, domain="sdram")), # is burstlen the right thing here?


					readback_fifo_id.eq(readback_addr[-self.fifo_id_bits:]),
					readback_buf_addr.eq(readback_addr[:self.fifo_buf_word_addr_bits]),

					self.dst_fifos[readback_fifo_id].w_en.eq(1),
					self.dst_fifos[readback_fifo_id].w_data.eq(self.readback_sdram_data),
				]

				# readback_buf_addr not presently used - could we use it to do checks?
			
		def determine_how_much_sdram_is_used_per_fifo():
			# check_if_srcfifo_ready_to_be_writen_to_sdram
			# check how much storage space is currently stored in ram for this fifo. 
			# note that we use it as if it's a circular buffer
			for i in range(self.num_fifos):
				with self.m.If(self.fifo_controls[i].r_next_addr <= self.fifo_controls[i].w_next_addr):
					self.m.d.comb += self.fifo_controls[i].words_stored_in_ram.eq(self.fifo_controls[i].w_next_addr - self.fifo_controls[i].r_next_addr)
				with self.m.Else():
					self.m.d.comb += self.fifo_controls[i].words_stored_in_ram.eq(self.fifo_controls[i].w_next_addr + (self.buf_words_available-self.fifo_controls[i].r_next_addr))
		
		def determine_what_to_do_next():
			""" 
			todo - add in read stuff
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
			

			srcfifo_r_level_enough = Signal()
			ram_wont_overfill = Signal()
			using_ram = Signal()

			ram_wont_overread = Signal()
			dstfifo_w_space_enough = Signal()

			# self.m.d.comb += [
			# 	all_srcfifos_read.eq(srcfifo_readable_to_sdram == 0),
			# 	all_dstfifos_written.eq(dstfifo_writeable_from_sdram == 0)
			# ]

			with self.m.Switch(self.fifo_index):
				for i in range(self.num_fifos):
					with self.m.Case(i):
						# for src_fifo -> sdram
						with self.m.If(i == (self.num_fifos-1)):
							self.m.d.comb += next_srcfifo_index.eq(0) 
						with self.m.Else():
							self.m.d.comb += next_srcfifo_index.eq(i+1) 

						s = next_srcfifo_index

						self.m.d.comb += [
							srcfifo_r_level_enough.eq(self.src_fifos[s].r_level >= (self.burstlen*self.numbursts)),
							ram_wont_overfill.eq(self.fifo_controls[s].words_stored_in_ram < (self.buf_words_available-(self.burstlen*self.numbursts))),
							using_ram.eq(self.fifo_controls[s].request_to_store_data_in_ram | (self.fifo_controls[s].words_stored_in_ram != 0)),
						
							next_srcfifo_readable_to_sdram.eq(srcfifo_r_level_enough & ram_wont_overfill & using_ram)
						]

						# for sdram -> dst_fifo
						with self.m.If(i == (self.num_fifos-1)):
							self.m.d.comb += next_dstfifo_index.eq(0) 
						with self.m.Else():
							self.m.d.comb += next_dstfifo_index.eq(i+1) 

						d = next_dstfifo_index
							
						self.m.d.comb += [
							ram_wont_overread.eq(self.fifo_controls[d].words_stored_in_ram >= (self.burstlen*self.numbursts)),
							dstfifo_w_space_enough.eq((self.dst_fifos[d].depth - self.dst_fifos[d].r_level) >= (((self.burstlen*self.numbursts)+self.read_pipeline_clk_delay))),
						
							next_dstfifo_writeable_from_sdram.eq(ram_wont_overread & dstfifo_w_space_enough)
						]

		
		route_data_through_sdram_or_bypass()
		handle_read_pipeline()
		determine_how_much_sdram_is_used_per_fifo()	
		determine_what_to_do_next()


		with self.m.FSM(domain="sdram", name="fifo_controller_fsm"):
			with self.m.State("WAITING_FOR_INITIALISE"):
				with self.m.If(self.core.refresh_controller.initialised):
					self.m.d.comb += [
						self.core.read_write_controller.task_request.eq(cmds.RW_IDLE)
					]
					# set the reset values here, which are not set elsewhere
					self.m.d.sdram += [self.fifo_controls[i].w_next_addr.eq(i<<self.fifo_buf_word_addr_bits) for i in range(self.num_fifos)]
					self.m.d.sdram += [self.fifo_controls[i].r_next_addr.eq(i<<self.fifo_buf_word_addr_bits) for i in range(self.num_fifos)]
					
					self.m.d.sdram += self.fifo_index.eq(0)
					self.m.next = "REFRESH_OR_IDLE"

			with self.m.State("REFRESH_OR_IDLE"):
				""" 
				do refresh, or wait,
				in case there's nothing to do, perhaps we could later implement some power down/optimisation thing here.

				Note that this needs to be an.. even number of clock cycles (or equal to the burstlen cycles?), if doing a memory access at the moment? so trying REFRESH_OR_IDLE_2 state to see if that fixes a bug
				"""
				with self.m.If(self.core.refresh_controller.request_to_refresh_soon):
					with self.m.If(~self.core.read_write_controller.in_progress): # wait for sany reads/writes to finish / banks to go idle, is this needed?
						self.m.d.comb += self.core.refresh_controller.trigger_refresh.eq(1)

				with self.m.Elif(self.core.refresh_controller.refresh_in_progress):
					pass # wait for it to finish

				with self.m.Else():
					# self.m.next = "REFRESH_OR_IDLE_2"
					with self.m.If(next_srcfifo_readable_to_sdram):
						self.m.next = "WRITE_SRCFIFOS_TO_SDRAM"

					# with self.m.Elif(~all_dstfifos_written):
					with self.m.If(next_dstfifo_writeable_from_sdram):
						self.m.next = "READ_SDRAM_TO_DSTFIFOS"
			# with self.m.State("REFRESH_OR_IDLE_2"):
				# with self.m.If(~all_srcfifos_read):
				# with self.m.If(next_srcfifo_readable_to_sdram):
				# 	self.m.next = "WRITE_SRCFIFOS_TO_SDRAM"

				# # with self.m.Elif(~all_dstfifos_written):
				# with self.m.If(next_dstfifo_writeable_from_sdram):
				# 	self.m.next = "READ_SDRAM_TO_DSTFIFOS"
									

			with self.m.State("WRITE_SRCFIFOS_TO_SDRAM"):
				self.m.d.comb += [
					self.core.read_write_controller.task_request.eq(cmds.RW_WRITE_16W)
				]

				with self.m.Switch(self.fifo_index):

					for i in range(self.num_fifos): # for each fifo, 
						# next_i = i + 1 if (i+1)<self.num_fifos else 0
						with self.m.Case(i):									
							def write_word_address_for_word_at_start_of_burst():
								with self.m.If(self.burst_index == 0):
									# todo - which of these is right?
									self.m.d.comb += self.sdram_addr.eq(Cat(self.fifo_controls[i].w_next_addr, self.fifo_index))
									# self.m.d.comb += self.sdram_addr.eq(Cat(self.fifo_index, self.fifo_controls[i].w_next_addr))
								with self.m.Else():
									self.m.d.comb += self.sdram_addr.eq(0)
								
							def write_word_data_for_each_word_in_burst():
								self.m.d.comb += [
									self.sdram_data.eq(self.src_fifos[self.fifo_index].r_data),
									self.src_fifos[self.fifo_index].r_en.eq(1),
								]
								self.m.d.sdram += [
									self.fifo_controls[i].w_next_addr.eq(self.fifo_controls[i].w_next_addr + 1)
								]

							def when_burst_ends_change_fifo_or_readwrite():

								with self.m.If((self.burst_index + 1) == self.burstlen): # burst finished
									self.m.d.sdram += self.burst_index.eq(0)

									with self.m.If((self.numburst_index + 1) == self.numbursts): # done several bursts with this fifo, now move on
										self.m.d.sdram += self.numburst_index.eq(0)

										self.m.d.sdram += self.fifo_index.eq(next_srcfifo_index) # prepare to do the next fifo

										with self.m.If(self.core.refresh_controller.request_to_refresh_soon):# | all_dstfifos_written):
											self.m.next = "REFRESH_OR_IDLE"

										with self.m.Else():
											with self.m.If(~next_srcfifo_readable_to_sdram):
												self.m.d.sdram += self.fifo_index.eq(0)

												with self.m.If(next_dstfifo_writeable_from_sdram):
													self.m.next = "READ_SDRAM_TO_DSTFIFOS"
												with self.m.Else():
													self.m.next = "REFRESH_OR_IDLE"
											
											# with self.m.Else():
											# 	self.m.d.sdram += self.fifo_index.eq(next_srcfifo_index) # prepare to do the next fifo

									with self.m.Else():
										self.m.d.sdram += self.numburst_index.eq(self.numburst_index + 1)

								with self.m.Else():
									self.m.d.sdram += self.burst_index.eq(self.burst_index + 1)
												
							write_word_address_for_word_at_start_of_burst()
							write_word_data_for_each_word_in_burst()
							when_burst_ends_change_fifo_or_readwrite()

			with self.m.State("READ_SDRAM_TO_DSTFIFOS"):
				
				self.m.d.comb += [
					self.core.read_write_controller.task_request.eq(cmds.RW_READ_16W)
				]

				with self.m.Switch(self.fifo_index):

					for i in range(self.num_fifos): # for each fifo,
						with self.m.Case(i):

							def write_word_address_for_word_at_start_of_burst():
								with self.m.If(self.burst_index == 0):
									self.m.d.comb += self.sdram_addr.eq(Cat(self.fifo_controls[i].r_next_addr, self.fifo_index))
								with self.m.Else():
									self.m.d.comb += self.sdram_addr.eq(0)

							def increment_address_read_counter():
								""" 
								Note - due to the sdram cas delay, the read back data is dealt with elsewhere,
								this just helps to record how much data is still unread in sdram.
								Note that we should only trust this after <cas_delay> cycles.
								"""
								self.m.d.sdram += [
									self.fifo_controls[i].r_next_addr.eq(self.fifo_controls[i].r_next_addr + 1)
								]
								

							def when_burst_ends_change_fifo_or_readwrite():
								with self.m.If((self.burst_index + 1) == self.burstlen): # burst finished
									self.m.d.sdram += self.burst_index.eq(0)

									with self.m.If((self.numburst_index + 1) == self.numbursts): # done several bursts with this fifo, now move on
										self.m.d.sdram += self.numburst_index.eq(0)

										self.m.d.sdram += self.fifo_index.eq(next_dstfifo_index) # prepare to do the next fifo

										with self.m.If(self.core.refresh_controller.request_to_refresh_soon):
											self.m.next = "REFRESH_OR_IDLE"
											
										with self.m.Else():
											with self.m.If(~next_dstfifo_writeable_from_sdram):
												self.m.d.sdram += self.fifo_index.eq(0)

												with self.m.If(next_srcfifo_readable_to_sdram):
													self.m.next = "WRITE_SRCFIFOS_TO_SDRAM"
												with self.m.Else():
													self.m.next = "REFRESH_OR_IDLE"
																								
											# with self.m.Else():
											# 	self.m.d.sdram += self.fifo_index.eq(next_dstfifo_index) # do the next fifo

									with self.m.Else():
										self.m.d.sdram += self.numburst_index.eq(self.numburst_index + 1)

								with self.m.Else():
									self.m.d.sdram += self.burst_index.eq(self.burst_index + 1)

							increment_address_read_counter()
							write_word_address_for_word_at_start_of_burst()
							when_burst_ends_change_fifo_or_readwrite()
			
			with self.m.State("ERROR"):
				pass

		return self.m

