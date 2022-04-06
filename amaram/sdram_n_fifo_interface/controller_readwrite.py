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

# from controller_pin import controller_pin
import controller_pin
from Delayer import Delayer

from parameters_standard_sdram import sdram_cmds, rw_cmds

""" 
Read/write controller

Goals:
- Inputs to this module:
	- Whether to read or write
	- Word address (each different address corresponds to a different 8/16/32/etc bit word on the dq bus)
		- Note that this address needs to be increment by one for each clock, for a an effective burst length of 16 (implemented as 2x 8word chip bursts)
	If write:
		- Word data, the same width as the dq bus, 8/16/32/etc bits
	If read:
		- A delayed/pipelined read bus is available, containing
			- readback pipeline active or not,
			- readback address
			- readback data

Ideas:
- ah! so burst ID (i.e. which of the two 8-word bursts we're in) is entirely a concept for the fifo controller, not the rw controller! let's remove it from here then


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

def min_num_of_clk_cycles(freq_hz, period_sec):
	return int(np.ceil(period_sec * freq_hz))

def get_ui_layout(config_params):
	ui_layout = [
		("rw_copi", [
			# This is to either do a write with this w_data, 
			# or to trigger a pipelined read on the address
			("task",	rw_cmds,	DIR_FANOUT),
			("addr",	config_params.rw_params.get_ADDR_BITS(),	DIR_FANOUT),
			("w_data",	config_params.rw_params.DATA_BITS.value,	DIR_FANOUT),
		]),
		("r_cipo", [
			# this is to recieve the pipelined read that is
			# scheduled using the above pipeline
			("read_active",	1,	DIR_FANIN),
			("addr",	config_params.rw_params.get_ADDR_BITS(),	DIR_FANIN),
			("r_data",	config_params.rw_params.DATA_BITS.value,	DIR_FANIN),
		]),
		("in_progress",		1,		DIR_FANIN)
	]

	return ui_layout


class controller_readwrite(Elaboratable):
	

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):

		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		self.ui = Record(get_ui_layout(self.config_params))
		self.pin_ui = Record(controller_pin.get_ui_layout(self.config_params))

		# add some default parameters. Could this be done better?
		if not hasattr(self.config_params, "burstlen"): 	self.config_params.burstlen = 8
		if not hasattr(self.config_params, "readback_addr_offset"): self.config_params.readback_addr_offset = 4 # to match timing of readback data when going through various clocked buffers
		
		
	def elaborate(self, platform = None):
		
		m = Module()

		ic_timing = self.config_params.ic_timing
		rw_params = self.config_params.rw_params

		# make inter-module interfaces
		_ui = Record.like(self.ui)
		_pin_ui = Record.like(self.pin_ui)
		myui = Record.like(self.pin_ui)

		# default io values
		m.d.sync += [
			_pin_ui.cmd.eq(sdram_cmds.CMD_NOP),
			_pin_ui.clk_en.eq(1), # best done here or elsewhere?
			# _pin_ui.dqm.eq(1), 
			# _pin_ui.rw_copi.dq.eq(0),
			# _pin_ui.rw_cipo.dq.eq(0),
			_pin_ui.rw_copi.a.eq(0),
			_pin_ui.rw_copi.ba.eq(0),

			_pin_ui.rw_copi.read_active.eq(0)
		]

		# connect the write signals to the package pins,
		# excluding the readback pipeline
		m.d.sync += [
			self.ui.connect(_ui),
			_pin_ui.connect(self.pin_ui, exclude=["rw_cipo"]),
		]

		# now connect the readback pipeline
		m.d.sync += [
			# connect the readback pipeline
			_pin_ui.rw_cipo.dq.eq(self.pin_ui.rw_cipo.dq), # cipo!

			_pin_ui.rw_cipo.addr.eq(Past(self.pin_ui.rw_cipo.addr, clocks=self.config_params.latency)),
			_pin_ui.rw_cipo.read_active.eq(self.pin_ui.rw_cipo.read_active),	

			# and connect it back to the module interface
			_ui.r_cipo.read_active.eq(_pin_ui.rw_cipo.read_active),
			_ui.r_cipo.addr.eq(_pin_ui.rw_cipo.addr),
			_ui.r_cipo.r_data.eq(_pin_ui.rw_cipo.dq),
					
		]

		# make_row_column_and_bank_from_address
		row = Signal(rw_params.ROW_BITS.value)
		col = Signal(rw_params.COL_BITS.value)
		bank = Signal(rw_params.BANK_BITS.value)
		data = Signal(rw_params.DATA_BITS.value)

		num_banks = 1<<rw_params.BANK_BITS.value

		burst_bits = bits_for(self.config_params.burstlen-1)
		burst_index = Signal(burst_bits)
		m.d.sync += [
			Cat(col[:burst_bits], bank, col[burst_bits:], row).eq(_ui.rw_copi.addr),
			data.eq(_ui.rw_copi.w_data),

			# this index is where are we in the current burst,
			burst_index.eq(_ui.rw_copi.addr[:burst_bits])
		]

		# assume this for now, but later generate the values from these from the given timing settings
		t_ra_clks = 3
		t_cas_clks = 3

		# # set default values
		# m.d.sync += [
		# 	_ui.readback_active.eq(0),
		# 	_ui.readback_addr.eq(0),
		# 	_ui.readback_data.eq(0)
		# ]

		# main logic, per bank
		bank_using_array = Array(Record([
			("in_progress",			1),
			("cmd_and_addr_bus",	1),
			("data_bus",			1),
			("readback_bus",		1),
			("dqm",					1)
		]) for i in range(num_banks)) # for debuging, so we can see bus contention etc

		adding_to_readback_bus = Signal()
		m.d.comb += [
			adding_to_readback_bus.eq(Cat([_bank_using.readback_bus for _bank_using in bank_using_array]).any()),
			_pin_ui.dqm.eq(Cat([_bank_using.dqm for _bank_using in bank_using_array]).all()),
			_ui.in_progress.eq(Cat([_bank_using.in_progress for _bank_using in bank_using_array]).any())
		]

		# for bank_id, bank_using in enumerate([bank_using_array[0]]):
		for bank_id, bank_using in enumerate(bank_using_array):
			with m.FSM(name=f"rw_bank_{bank_id}_fsm") as fsm:
				""" 
				todo:
				- verify the timing of all this. Note that this is based on logic which used .comb's everywhere,
				  here .sync's are used everywhere instead
				
				"""
				# defaults (note! must use 'bank_using', as this is called in a loop, otherwise will be overwritten)
				# default to not using resources
				m.d.sync += [
					bank_using.readback_bus.eq(0),
					bank_using.dqm.eq(1),
					bank_using.cmd_and_addr_bus.eq(0),
					bank_using.data_bus.eq(0),
					bank_using.in_progress.eq(~fsm.ongoing("IDLE"))
				]

				with m.State("IDLE"):
					with m.If((bank == bank_id) & (burst_index == 0) & (Past(_ui.rw_copi.task) != rw_cmds.RW_IDLE)):
						m.d.sync += [
							bank_using.cmd_and_addr_bus.eq(1),
							_pin_ui.cmd.eq(sdram_cmds.CMD_ACT),
							_pin_ui.rw_copi.ba.eq(bank_id),#Past(bank_id)),	 # past of a const doesn't make sense
							_pin_ui.rw_copi.a.eq(row),#Past(row)),
						]
						m.next = "WAS_ACTIVE_NOP1"

				with m.State("WAS_ACTIVE_NOP1"): # todo - instead of this gap thing, use a delayer with T_RA
					m.next = "NOP3"

				with m.State("NOP3"):
					with m.If(Past(_ui.rw_copi.task, clocks=t_ra_clks) == rw_cmds.RW_WRITE):
						m.next = "WRITE_0"
					
					with m.Elif(Past(_ui.rw_copi.task, clocks=t_ra_clks) == rw_cmds.RW_READ):
						m.next = "READ_-3"

					with m.Else():
						m.next = "ERROR"
				
				##################### write ###############################

				with m.State("WRITE_0"):
					m.d.sync += [
						bank_using.cmd_and_addr_bus.eq(1),
						bank_using.data_bus.eq(1),
						_pin_ui.cmd.eq(sdram_cmds.CMD_WRITE_AP),
						_pin_ui.rw_copi.ba.eq(bank_id), # constant for this bank

						# 13mar2022 note: bug if this does not start from zero. It seems that the use of past(<clks>) here is used before <clks> has elapsed, 
						# resulting in a zero-value, that can be bypassed if we start from zero. And potentially this goes away if we refresh first... let's start from zero for now.
						_pin_ui.rw_copi.a.eq(Past(col, clocks=t_ra_clks)),
						_pin_ui.rw_copi.dq.eq(Past(data, clocks=t_ra_clks)),

						bank_using.dqm.eq(0),  # dqm low synchronous with write data
					]
					m.next = "WRITE_1"
				
				for i in range(self.config_params.burstlen-1): # is the -1 needed?
					byte_id = i+1
					with m.State(f"WRITE_{byte_id}"):
						m.d.sync += [
							bank_using.data_bus.eq(1),
							_pin_ui.rw_copi.dq.eq(Past(data, clocks=t_ra_clks)),
							bank_using.dqm.eq(0),  # dqm low synchronous with write data
						]

						if byte_id < (self.config_params.burstlen)-1:
							m.next = f"WRITE_{byte_id+1}"
						else:
							m.next = "IDLE" #"IDLE_END"

				##################### read ###############################

				with m.State("READ_-3"):
					# do a check to see if the dqm condition was met. Should this be in simulation
					# rather than in rtl?
					# with m.If(Cat([Past(_pin_ui.dqm, clocks=1+j) for j in range(3)]) != 0b111):
					# 	m.next = "ERROR"

					m.d.sync += [
						bank_using.cmd_and_addr_bus.eq(1),
						_pin_ui.cmd.eq(sdram_cmds.CMD_READ_AP),
						_pin_ui.rw_copi.ba.eq(bank_id), # constant for this bank
						_pin_ui.rw_copi.a.eq(Past(col, clocks=t_ra_clks)),
						bank_using.dqm.eq(0),

						# this records the global address that the read occurred at,
						# so it can more easily identify read data in the read pipeline
						_pin_ui.rw_copi.addr.eq(Past(_ui.rw_copi.addr, clocks=t_ra_clks+1)),
					]
					m.next = "READ_-2"
				
				for i in range(self.config_params.burstlen+2):
					byte_id = i-2
					with m.State(f"READ_{byte_id}"):
						if byte_id in [b-2 for b in range(self.config_params.burstlen-1)]:
							m.d.sync += [
								bank_using.dqm.eq(0), # assuming this is 2 clks before a read
								_pin_ui.rw_copi.addr.eq(_pin_ui.rw_copi.addr + 1),
							]
						
						if byte_id in [b for b in range(self.config_params.burstlen+1)]:
							m.d.sync += [
								# bank_using.dqm.eq(0),  # dqm low synchronous with write data
								_pin_ui.rw_copi.read_active.eq(1),
							]

						if byte_id < (self.config_params.burstlen)-1:
							m.next = f"READ_{byte_id+1}"
						else:
							m.next = "IDLE" #"IDLE_END"

				with m.State("ERROR"):
					...


		...

		if isinstance(self.utest, FHDLTestCase):
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()

			if test_id == "readwriteCtrl_sim_thatGivenAddress_generatesCorrectBankColumnAndRowValues":
				...
			
			elif test_id == "readwriteCtrl_sim_thatBurstIncrementingAddress_generatesCorrectWriteSequences":
				...
		
		
		elif isinstance(platform, ULX3S_85F_Platform): 
			...
		
		else:
			... # This case means that a test is occuring and this is not the top-level module.
		
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

	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing
		if False:
			class readwriteCtrl_sim_thatGivenAddress_generatesCorrectBankColumnAndRowValues(FHDLTestCase):
				def test_sim(self):
					from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params

					config_params = Params()
					config_params.clk_freq = 143e6
					config_params.ic_timing = ic_timing
					config_params.ic_refresh_timing = ic_refresh_timing
					config_params.rw_params = rw_params

					utest_params = Params()

					dut = controller_readwrite(config_params, utest_params, utest=self)

					sim = Simulator(dut)
					sim.add_clock(period=1/config_params.clk_freq, domain="sync")

					def pass_address_and_inspect_row_col_and_bank():
						for i in range(100):
							yield dut.ui.addr.eq(i)
							yield
						
						for i in [0b0, 0b111, 0b11000, 0b11111100111, 0b1111111111100000000000]:
							yield dut.ui.addr.eq(i)
							yield

						for i in range(22):
							yield dut.ui.addr.eq(1<<i)
							yield
						
						for _ in range(10):
							yield

					sim.add_sync_process(pass_address_and_inspect_row_col_and_bank)


					with sim.write_vcd(
						f"{current_filename}_{self.get_test_id()}.vcd"):
						sim.run()

			class readwriteCtrl_sim_thatBurstIncrementingAddress_generatesInspectableWriteAndReadSequences(FHDLTestCase):
				def test_sim(self):
					from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params

					config_params = Params()
					config_params.ic_timing = ic_timing
					config_params.ic_refresh_timing = ic_refresh_timing
					config_params.rw_params = rw_params
					config_params.clk_freq = 143e6
					config_params.burstlen = 8
					# config_params.numbursts = 2 # only used in the fifo controller, not in rw controller

					utest_params = Params()

					dut = controller_readwrite(config_params, utest_params, utest=self)

					sim = Simulator(dut)
					sim.add_clock(period=1/config_params.clk_freq, domain="sync")

					def use_ui_and_see_if_correct_rw_behaviour():
						num_full_bursts = 5 # e.g.
						addr_offset = 0xABCDE
						for action in [rw_cmds.RW_WRITE, rw_cmds.RW_READ]:
							for i in range(config_params.burstlen * num_full_bursts):
								i += addr_offset

								if action == rw_cmds.RW_WRITE:
									yield dut.ui.data.eq(i)
								elif action == rw_cmds.RW_READ:
									# yield dut.pin_ui.rw_cipo.dq.eq(i) # is this right? reading data from the chip?
									... # no: use the chip model for this

								yield dut.ui.addr.eq(i)

								if ((i % config_params.burstlen) == 0):
									yield dut.ui.task.eq(action)
								else:
									yield dut.ui.task.eq(action)

								yield
							
							# a few extra clocks at the end
							for _ in range(10):
								yield

					sim.add_sync_process(use_ui_and_see_if_correct_rw_behaviour)

					
					with sim.write_vcd(
						f"{current_filename}_{self.get_test_id()}.vcd"):
						sim.run()
				
		class readwriteCtrl_sim_thatWritingThenReadingBack_readsCorrectValues(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

				config_params = Params()
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params
				config_params.clk_freq = 143e6
				config_params.burstlen = 8
				config_params.latency = 3
				# config_params.numbursts = 2 # only used in the fifo controller, not in rw controller

				utest_params = Params()
				utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks


				tb = Testbench(config_params, utest_params, utest=self)

				sim = Simulator(tb)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				for process, domain in tb.get_sim_sync_processes():
					sim.add_sync_process(process, domain=domain)

				with sim.write_vcd(
					f"{current_filename}_{self.get_test_id()}.vcd"):
					sim.run()

				################## old below

				dut = controller_readwrite(config_params, utest_params, utest=self)



				sim = Simulator(dut)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				# and a negedge clock for the 'propagate_i_dq_reads' simulation process

				# clki_n = ClockDomain("clki_n", clk_edge="pos")#, local=True)
				# # clki_n = ClockDomain("clki_n", clk_edge="neg")
				# self.m.domains += clki_n
				# self.m.d.comb += clki_n.clk.eq(~self.dut.o_clk) # 
				

				sdram_model = model_sdram(config_params, utest_params)
				for i in range(4): # num of banks
					sim.add_sync_process(sdram_model.get_readwrite_process_for_bank(bank_id = i, pin_ui=dut.pin_ui))
				sim.add_sync_process(sdram_model.propagate_i_dq_reads(pin_ui=dut.pin_ui))

				def use_ui_and_see_if_correct_rw_behaviour():
					num_full_bursts = 8 # e.g.

					# note: bug if this does not start from zero. It seems that the use of past(<clks>) here is used before <clks> has elapsed, 
					# resulting in a zero-value, that can be bypassed if we start from zero. And potentially this goes away if we refresh first... let's start from zero for now.
					addr_offset = 0x0000

					for action in [rw_cmds.RW_WRITE, rw_cmds.RW_READ]:
						for i in range(config_params.burstlen * num_full_bursts):
							i += addr_offset

							if action == rw_cmds.RW_WRITE:
								yield dut.ui.rw_copi.w_data.eq(i)
							elif action == rw_cmds.RW_READ:
								# assert (yield dut.pin_ui.rw_cipo.dq) == i # is this right? reading data from the chip?
								... # no: use the chip model for this

							yield dut.ui.rw_copi.addr.eq(i)

							if ((i % config_params.burstlen) == 0):
								yield dut.ui.rw_copi.task.eq(action)
							else:
								yield dut.ui.rw_copi.task.eq(rw_cmds.RW_IDLE)
								...

							yield
						
						# a few extra clocks at the end
						for _ in range(10):
							yield
					
					# a few extra clocks at the end
					for _ in range(20):
						yield

				def print_readback_data():
					yield Passive()
					while True:
						if (yield dut.ui.r_cipo.read_active):
							data = (yield dut.ui.r_cipo.r_data)
							addr = (yield dut.ui.r_cipo.addr)
							print(f"Read at address={hex(addr)}, data={hex(data)}")
						yield

				def start_readback_pipeline():
					# this should be done close to where the copi_dq and cipo_dq split
					yield Passive()
					while True:
						yield dut.pin_ui.rw_cipo.addr.eq((dut.pin_ui.rw_copi.addr))
						yield dut.pin_ui.rw_cipo.read_active.eq((dut.pin_ui.rw_copi.read_active))
						yield Settle()
						yield
						yield Settle()

				sim.add_sync_process(use_ui_and_see_if_correct_rw_behaviour)
				sim.add_sync_process(print_readback_data)
				sim.add_sync_process(start_readback_pipeline)

				with sim.write_vcd(
					f"{current_filename}_{self.get_test_id()}.vcd"):
					sim.run()

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