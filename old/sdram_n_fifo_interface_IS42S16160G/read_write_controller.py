
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

from .base import cmd_to_dram_ic, rw_cmds, sdram_base, sdram_quad_timer


class read_write_controller(sdram_quad_timer):
	"""
	based on the datasheet on p.35 and p.45  
	note that this only does 16-word burst reads and writes, of 8 writes each with a numbursts of 2

	When the fifo controller's outputs follow this structure

	...][	fifo a	]								 													 [	fifo a	]					 																 [	fifo b	][...	
	...][read/write	]			 					 													 [read/write]					 													 			 [read/write][...
	...][	addr a0	]								 													 [	addr a8	]					 																 [	addr b0	][...
	...][	data a0	][	data a1	][	data a2	][	data a3	][	data a4	][	data a5	][	data a6	][	data a7	][	data a8	][	data a9	][	data a10][	data a11][	data a12][	data a13][	data a14][	data a15][	data b0	][...

	for writes, this class needs to do:

		[	ACTIVE	]						 [	WRITE	]
		[	bank	]						 [	bank	]
		[	row		]							
												[	col		]
												[	data 0	][	data 1	][	data 2	][	data 3	][	data 4	][	data 5	][	data 6	][	data 7	]

		[	dqm=1	][	dqm=1	][	dqm=1	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	]		dqm=1 defult

		^--this dqm only needed after read--^
			to prevent possible bus contention?
			default dqm to 1 so it should fulfil this by defualt?

		|<---t_ra ?------------------------>|	
				(assume t_ra_clks = 3)
											col from Past(..., clocks=t_ra_clks)
											data from col from Past(..., clocks=t_ra_clks, t_ra_clks+1)


	for reads, similar but with a 'pipeline' thing to send data back to the fifo controller
	in a way that lets its fsm focus on what to do next, and handle the read data in the background

		[	ACTIVE	]						 [	READ	]
		[	bank	]						 [	bank	]
		[	row		]							
												[	col		]
																				[	data 0	][	data 1	][	data 2	][	data 3	][	data 4	][	data 5	][	data 6	][	data 7	]
					dqm=1 defult						 [	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	][	dqm=0	]
															^--2clks before dq-----^	

		|<---t_ra ?------------------------>|<---------------t_cas_clks-------->|
				(assume t_ra_clks = 3)				(assume t_cas_clks = 3)


		________________________________________________________________
	OLD:

	for writes, this class needs to do:

		[	ACTIVE	]						 [	WRITE	]
		[	bank	]						 [	bank	]
		[	row		]							
												[	col		]
												[	data 0	][	data 1	]

		[	dqm=1	][	dqm=1	][	dqm=1	][	dqm=0	][	dqm=0	]		dqm=1 defult

		^--this dqm only needed after read--^
			to prevent possible bus contention?
			default dqm to 1 so it should fulfil this by defualt?

		|<---t_ra ?------------------------>|	
				(assume t_ra_clks = 3)
											col from Past(..., clocks=t_ra_clks)
											data from col from Past(..., clocks=t_ra_clks, t_ra_clks+1)


	for reads, similar but with a 'pipeline' thing to send data back to the fifo controller
	in a way that lets its fsm focus on what to do next, and handle the read data in the background

		[	ACTIVE	]						 [	READ	]
		[	bank	]						 [	bank	]
		[	row		]							
												[	col		]
																				[	data 0	][	data 1	]
					dqm=1 defult						 [	dqm=0	][	dqm=0	]	
															^--2clks before dq-----^	

		|<---t_ra ?------------------------>|<---------------t_cas_clks-------->|
				(assume t_ra_clks = 3)				(assume t_cas_clks = 3)

											col from Past(..., clocks=t_ra_clks+t_cas_clks)
											data from col from Past(..., clocks=t_ra_clks+t_cas_clks, t_ra_clks+t_cas_clks+1)

	dqm should be high 2 clocks before data is to be read...?

	dqm: from datasheet
		- for normal operation, dqm should be low
			- in read mode, dqm controls the output buffer, and functions like OEn; when low, the buffer is enabled; when high, the pins are high-z
				- DQ's read data is subject to the state of DQM 2 clock cycles earlier. if DQM is high, the DQs will be high-z 2 clock cycles later
			- in write mode, dqm controls the input buffer; when low, data can be written to the device; when high, data is ignored
				- DQ's write data is subject to the state of DQM at the same time instant as the DQ data.
		- to switch from read to write,
			- DQM must be asserted high at least three clock cycles before the WRITE command to supress data-out from read.
			- DQM must be deasserted low synchronous with the read command, so it is not masked
		- to switch from write to read,
			- 
		



	then, imagine these commands, but interlaced, so the sdram has near 100% uptime!
	note that there is a bit of 'wasted' clock cycles when transitioning from read to writes, but writes commands (from the fifo controller) can transition to reads in the next clock cycle..


	"""
		

	def __init__(self, core):
		super().__init__()
		self.core = core

		cmds = rw_cmds

		# link to the fifo controller
		self.task_request = Signal(shape=cmds, reset=cmds.RW_IDLE)
		self.data = Signal(16)
		self.addr_16W = Signal(15)
		self.buf_id = Signal(2) # assumes 4 fifos
		self.in_progress = Signal()
		# self.write_ready = Signal()
		# self.read_ready = Signal()

		# link to the pin controller
		self.ios = Record(core.pin_controller.ios_layout)

	def elaborate(self, platform = None):
		super().elaborate(platform)


		"""
		sdram_addr: unique word address on the sdram chip.
		bit:					21	20	19	18	17	16	15	14	13	12	11	10	9	8	7	6	5	4	3	2	1	0
								|	
		buf id: 4bufs is 2bit	1	0
									|-->|
		buf_addr: 						19	18	17	16	15	14	13	12	11	10	9	8	7	6	5	4	3	2	1	0

																													|
		burst index	(e.g. 3bits for 8byte burst)														3	2	1	0
																									|<--|
																											^-------^
																											this width is burst,
																											currently 8bytes, so 
																											3 bits

																											^-------^
																											this width is burst + 
																											bursts_per_fifo, 
																											aka num of banks,
																											currently 2, so that
																											the bit can select which 
																											of two banks to interleave
																											between
																										
																										this bit represents which set
																										of banks will be used
		_________________________________________________________________________________________________________________________
	
		bank index: 2bits, after burst index, as bursts are within bank								1	0		
																								|<--|	|-->|
		column index, 9bits													8	7	6	5	4	3			2	1	0
																		|<--|
		row index, 11bits		10	9	8	7	6	5	4	3	2	1	0
			
		_________________________________________________________________________________________________________________________
		
		considerations:
		- bank accesses need need to be interleaved in a deterministic way, so we can read it back accurately
			- memory accesses should be a series of bursts to each of two fifos, 
			- hence the shortest possible memory access would be a burst to fifo A then a burst to fifo B
				- 2byte bursts fail timing requirements when trying to interleave with 4 banks (by one clock... ugh),
				- 4byte bursts fail timing when trying to interleave with 2 banks (but would work with 4, but that's 16x words) s
				- 8byte bursts pass when trying to interleave with 2 banks (and 8x2=16), but has fewer commands - perhaps lower power usage? [do this]
				- hence the shortest memory access (for 100% utilisation) is 2x 4byte bursts - let's go with that for now
		"""


		# default to this always being true....?
		self.m.d.comb += self.ios.o_clk_en.eq(1)

		num_banks = 1<<self.core.bank_bits

		cmds = rw_cmds
		
		burst_len = self.core.fifo_controller.burstlen #2 # have this somewhere else


		# self.col.eq(Cat(Const(0, shape=2), self.buf_id, self.addr_16W[:5]))
		# self.bank.eq(),
		# self.row.eq(self.addr_16W[5:])

		# num_active_banks = Signal(shape=bits_for(num_banks-1))

		t_ra_clks = 3
		t_cas_clks = 3


		o_row = Signal(11)
		o_bank = Signal(2)
		o_col = Signal(9)
		w_data = Signal(16)

		# do_read_in_cas_delay = Signal()
		burst_bits = bits_for(burst_len-1)

		addr_temp = Signal(shape=self.core.fifo_controller.sdram_addr.shape())
		
		# these are just split up, no timing information is added
		self.m.d.comb += [
			addr_temp.eq(self.core.fifo_controller.sdram_addr),

			o_bank.eq(addr_temp[burst_bits : burst_bits+2]), 
			o_col.eq(Cat(addr_temp[: burst_bits], addr_temp[burst_bits+2 : self.core.col_bits+self.core.bank_bits])),
			o_row.eq(addr_temp[self.core.col_bits+self.core.bank_bits:]),

			w_data.eq(self.core.fifo_controller.sdram_data)
		]

		# to represent the delayed-read thingo
		# with self.m.If(Past(self.task_request, clocks=t_ra_clks + t_cas_clks, domain="sdram") == cmds.RW_READ_16W):
		# 	self.m.d.comb += [
		# 		self.core.fifo_controller.readback_sdram_pipeline_active.eq(1),
		# 		self.core.fifo_controller.readback_sdram_addr.eq(Past(self.core.fifo_controller.sdram_addr, clocks=t_ra_clks + t_cas_clks, domain="sdram")),

		# 		# placeholder for real read data
		# 		self.core.fifo_controller.readback_sdram_data.eq(self.ios.i_dq) 
		# 	]	
		# with self.m.Else():
		# 	self.m.d.comb += [
		# 		self.core.fifo_controller.readback_sdram_pipeline_active.eq(0)
		# 	]


		bank_data = Array(Record([
			# ("NOP", 1)
			# ("ACT", 1),
			# ("WRITE", 1),
			("IN_PROGRESS", 1),
			("W_DATA", 16),
			("R_DATA", 16)
		]) for _ in range(num_banks))

		self.m.d.comb += [
			self.in_progress.eq(Cat([bank_data[i].IN_PROGRESS for i in range(num_banks)]) != 0),
			self.ios.o_dqm.eq(1) # by default
		]
	

		for bank_id in range(num_banks): # num banks
			self.m.d.comb += [
				bank_data[bank_id].W_DATA.eq(0),
				bank_data[bank_id].IN_PROGRESS.eq(1)
			]

			with self.m.FSM(domain="sdram", name=f"rw_bank_{bank_id}_fsm"):
				""" 
				The DQM signal must be as-serted (HIGH) at least three clocks prior 
				to the WRITE command (DQM latency is two clocks for output buffers) 
				to suppress data-out from the READ. Once the WRITE command is registered,
				the DQs will go High-Z (or remain High-Z), regardless of the state 
				of the DQM signal, providedthe DQM was active on the clock just 
				prior to the WRITE command that truncated the READ command. If not,
				the second WRITE will be an invalid' - p.29, sdram datasheet
				"""
				with self.m.State("IDLE"):
					with self.m.If((o_bank == bank_id) & (self.core.fifo_controller.burst_index == 0) & (self.task_request != cmds.RW_IDLE)): # is how this starts up ok? should it start up a different way?
						self.m.d.comb += [
							self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_ACT),
							self.ios.o_ba.eq(o_bank),
							self.ios.o_a[:12].eq(o_row),
						]
						self.m.next = "WAS_ACTIVE_NOP1"
					with self.m.Else():
						self.m.d.comb += bank_data[bank_id].IN_PROGRESS.eq(0)

				with self.m.State("WAS_ACTIVE_NOP1"):
					self.m.next = "NOP3"
					
				with self.m.State("NOP3"):
					with self.m.If(Past(self.task_request, clocks=t_ra_clks-1, domain="sdram") == cmds.RW_WRITE_16W):
						self.m.next = "WRITE_0"
					
					with self.m.Elif(Past(self.task_request, clocks=t_ra_clks-1, domain="sdram") == cmds.RW_READ_16W):
						self.m.next = "READ_-3"

					with self.m.Else():
						self.m.next = "ERROR"
					
				##################### write ###############################

				with self.m.State("WRITE_0"):
					self.m.d.comb += [
						self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_WRITE_AP),
						self.ios.o_ba.eq(Past(o_bank, clocks=t_ra_clks, domain="sdram")),  
						self.ios.o_a[:9].eq(Past(o_col, clocks=t_ra_clks, domain="sdram")),
						self.ios.o_dq.eq(Past(w_data, clocks=t_ra_clks, domain="sdram")),
						
						self.ios.o_dqm.eq(0), # dqm low synchronous with write data

						bank_data[bank_id].W_DATA.eq(self.ios.o_dq)
					]
					self.m.next = "WRITE_1"

				for i in range(burst_len-1):
					byte_id = i+1
					with self.m.State(f"WRITE_{byte_id}"):
						self.m.d.comb += [
							# self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP),
							self.ios.o_dq.eq(Past(w_data, clocks=t_ra_clks, domain="sdram")),
							self.ios.o_dqm.eq(0), # dqm low synchronous with write data

							bank_data[bank_id].W_DATA.eq(self.ios.o_dq)
						]
						# self.m.d.sdram += num_active_banks.eq(num_active_banks - 1)

						if byte_id < burst_len-1:
							self.m.next = f"WRITE_{byte_id+1}"
						else:
							self.m.next = "IDLE" #"IDLE_END"

				##################### read ###############################
				
				with self.m.State("READ_-3"): 
					# do a check to see if the dqm condition was met
					# with self.m.If(Cat([Past(self.ios.o_dqm, clocks=1+j, domain="sdram") for j in range(3)]) != 0b111):
					# 	self.m.next = "ERROR"

					self.m.d.comb += [
						self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_READ_AP),
						self.ios.o_ba.eq(Past(o_bank, clocks=t_ra_clks, domain="sdram")),  
						self.ios.o_a[:9].eq(Past(o_col, clocks=t_ra_clks, domain="sdram")),
						self.ios.o_dqm.eq(0)
					]
					self.m.next = "READ_-2"
				
				for i in range(burst_len+2):
					byte_id = i-2
					with self.m.State(f"READ_{byte_id}"):
						if byte_id in [b-2 for b in range(burst_len-1)]:
							self.m.d.comb += self.ios.o_dqm.eq(0)  # assuming this is 2 clks before a read
						
						if byte_id in [b for b in range(burst_len+1)]:
							self.m.d.comb += [
								self.core.fifo_controller.readback_sdram_pipeline_active.eq(1),
								self.core.fifo_controller.readback_sdram_addr.eq(Past(self.core.fifo_controller.sdram_addr, clocks=t_ra_clks + t_cas_clks, domain="sdram")),
								self.core.fifo_controller.readback_sdram_data.eq(self.ios.i_dq),

								bank_data[bank_id].R_DATA.eq(self.ios.i_dq) # for debugging?
							]
						else:
							self.m.d.comb += [
								# self.core.fifo_controller.readback_sdram_pipeline_active.eq(0),
								# self.core.fifo_controller.readback_sdram_addr.eq(0),
								# self.core.fifo_controller.readback_sdram_data.eq(0),

								bank_data[bank_id].R_DATA.eq(0)
							]
							

						if byte_id < burst_len-1:
							self.m.next = f"READ_{byte_id+1}"
						else:
							self.m.next = "IDLE" #"IDLE_END"

				##########################################################

				with self.m.State("ERROR"):
					pass

		return self.m
