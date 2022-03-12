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
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
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

from controller_pin import controller_pin
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
"""
def min_num_of_clk_cycles(freq_hz, period_sec):
	return int(np.ceil(period_sec * freq_hz))

class controller_readwrite(Elaboratable):
	ui_layout = [
		("task",	rw_cmds,	DIR_FANOUT),
		("addr",			None,		None), # added dynamically below
		("data",			None,		None), # added dynamically below

	]

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		def populate_interface_with_configurable_widths():
			for i in range(len(controller_readwrite.ui_layout)):
				if (controller_readwrite.ui_layout[i][0] == "addr") and (controller_readwrite.ui_layout[i][1]) == None:
					controller_readwrite.ui_layout[i] = ("addr", self.config_params.rw_params.get_ADDR_BITS(), DIR_FANOUT)

				elif (controller_readwrite.ui_layout[i][0] == "data") and (controller_readwrite.ui_layout[i][1]) == None:
					controller_readwrite.ui_layout[i] = ("data", self.config_params.rw_params.DATA_BITS.value, DIR_FANOUT)

		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		populate_interface_with_configurable_widths()

		self.ui = Record(controller_readwrite.ui_layout)
		self.controller_pin_ui = Record(controller_pin.ui)

		# add some default parameters. Could this be done better?
		if not hasattr(self.config_params, "burstlen"): 	self.config_params.burstlen = 8
		
	def elaborate(self, platform = None):
		
		m = Module()

		ic_timing = self.config_params.ic_timing
		rw_params = self.config_params.rw_params

		# make inter-module interfaces
		_ui = Record.like(self.ui)
		_controller_pin_ui = Record.like(self.controller_pin_ui)
		m.d.sync += [
			self.ui.connect(_ui),
			_controller_pin_ui.connect(self.controller_pin_ui), # so 'fanout' signals go the right way etc
		]

		# default io values
		m.d.sync += [
			_controller_pin_ui.ios.cmd.eq(sdram_cmds.CMD_NOP),
			_controller_pin_ui.ios.clk_en.eq(1), # best done here or elsewhere?
			_controller_pin_ui.ios.dqm.eq(1), 
			_controller_pin_ui.ios.copi_dq.eq(0),
			_controller_pin_ui.ios.cipo_dq.eq(0),
			_controller_pin_ui.ios.a.eq(0),
			_controller_pin_ui.ios.ba.eq(0),
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
			Cat(col[:burst_bits], bank, col[burst_bits:], row).eq(_ui.addr),
			data.eq(_ui.data),

			# this index is where are we in the current burst,
			burst_index.eq(_ui.addr[:burst_bits])
		]

		# assume this for now, but later generate the values from these from the given timing settings
		t_ra_clks = 3
		t_cas_clks = 3

		# main logic, per bank
		bank_idle_array = Array(Signal(name=f"bank_idle_{i}") for i in range(num_banks))
		for bank_id, bank_idle in enumerate(bank_idle_array):
			with m.FSM(name=f"rw_bank_{bank_id}_fsm") as fsm:
				""" 
				todo:
				- verify the timing of all this. Note that this is based on logic which used .comb's everywhere,
				  here .sync's are used everywhere instead
				
				"""
				m.d.sync += bank_idle.eq(fsm.ongoing("IDLE"))

				with m.State("IDLE"):
					with m.If((bank == bank_id) & (burst_index == 0) & (Past(_ui.task) != rw_cmds.RW_IDLE)):
						m.d.sync += [
							_controller_pin_ui.ios.cmd.eq(sdram_cmds.CMD_ACT),
							_controller_pin_ui.ios.ba.eq(Past(bank_id)),	
							_controller_pin_ui.ios.a.eq(Past(row)),
						]
						m.next = "WAS_ACTIVE_NOP1"

				with m.State("WAS_ACTIVE_NOP1"): # todo - instead of this gap thing, use a delayer with T_RA
					m.next = "NOP3"

				with m.State("NOP3"):
					with m.If(Past(_ui.task, clocks=t_ra_clks) == rw_cmds.RW_WRITE):
						m.next = "WRITE_0"
					
					with m.Elif(Past(_ui.task, clocks=t_ra_clks) == rw_cmds.RW_READ):
						m.next = "READ_-3"

					with m.Else():
						m.next = "ERROR"
				
				##################### write ###############################

				with m.State("WRITE_0"):
					m.d.sync += [
						_controller_pin_ui.ios.cmd.eq(sdram_cmds.CMD_WRITE_AP),
						_controller_pin_ui.ios.ba.eq(bank_id), # constant for this bank
						_controller_pin_ui.ios.a.eq(Past(col, clocks=t_ra_clks)),
						_controller_pin_ui.ios.copi_dq.eq(Past(data, clocks=t_ra_clks)),
						_controller_pin_ui.ios.dqm.eq(0),  # dqm low synchronous with write data
					]
					m.next = "WRITE_1"
				
				for i in range(self.config_params.burstlen):
					byte_id = i+1
					with m.State(f"WRITE_{byte_id}"):
						m.d.sync += [
							_controller_pin_ui.ios.copi_dq.eq(Past(data, clocks=t_ra_clks)),
							_controller_pin_ui.ios.dqm.eq(0),  # dqm low synchronous with write data
						]

						if byte_id < (self.config_params.burstlen)-1:
							m.next = f"WRITE_{byte_id+1}"
						else:
							m.next = "IDLE" #"IDLE_END"

				##################### read ###############################

				with m.State("READ_-3"):
					m.next = "ERROR"


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
	feb2022 - mar2022

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
		# class readwriteCtrl_sim_thatGivenAddress_generatesCorrectBankColumnAndRowValues(FHDLTestCase):
		# 	def test_sim(self):
		# 		from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
		# 		from model_sdram import model_sdram

		# 		config_params = Params()
		# 		config_params.clk_freq = 143e6
		# 		config_params.ic_timing = ic_timing
		# 		config_params.ic_refresh_timing = ic_refresh_timing
		# 		config_params.rw_params = rw_params

		# 		utest_params = Params()

		# 		dut = controller_readwrite(config_params, utest_params, utest=self)

		# 		sim = Simulator(dut)
		# 		sim.add_clock(period=1/config_params.clk_freq, domain="sync")

		# 		def pass_address_and_inspect_row_col_and_bank():
		# 			for i in range(100):
		# 				yield dut.ui.addr.eq(i)
		# 				yield
					
		# 			for i in [0b0, 0b111, 0b11000, 0b11111100111, 0b1111111111100000000000]:
		# 				yield dut.ui.addr.eq(i)
		# 				yield

		# 			for i in range(22):
		# 				yield dut.ui.addr.eq(1<<i)
		# 				yield
					
		# 			for _ in range(10):
		# 				yield

		# 		sim.add_sync_process(pass_address_and_inspect_row_col_and_bank)


		# 		with sim.write_vcd(
		# 			f"{current_filename}_{self.get_test_id()}.vcd"):
		# 			sim.run()

		class readwriteCtrl_sim_thatBurstIncrementingAddress_generatesCorrectWriteSequences(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

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
					for i in range(config_params.burstlen * num_full_bursts):
						i += addr_offset
						yield dut.ui.data.eq(i)
						yield dut.ui.addr.eq(i)

						if ((i % config_params.burstlen) == 0):
							yield dut.ui.task.eq(rw_cmds.RW_WRITE)
						else:
							yield dut.ui.task.eq(rw_cmds.RW_IDLE)

						yield
					
					# for i in [0b0, 0b111, 0b11000, 0b11111100111, 0b1111111111100000000000]:
					# 	yield dut.ui.addr.eq(i)
					# 	yield

					# for i in range(22):
					# 	yield dut.ui.addr.eq(1<<i)
					# 	yield
					
					# a few extra clocks at the end
					for _ in range(10):
						yield

				sim.add_sync_process(use_ui_and_see_if_correct_rw_behaviour)

				
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