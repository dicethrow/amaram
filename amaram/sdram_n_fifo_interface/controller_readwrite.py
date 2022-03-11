import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
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
"""
def min_num_of_clk_cycles(freq_hz, period_sec):
	return int(np.ceil(period_sec * freq_hz))

class controller_readwrite(Elaboratable):
	ui_layout = [
		("task_request",	rw_cmds,	DIR_FANOUT),
		("addr",			None,		None), # added dynamically below
		("data",			None,		None), # added dynamically below

	]

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		def populate_interface_with_configurable_widths():
			for i in range(len(controller_readwrite.ui_layout)):
				if (controller_readwrite.ui_layout[i][0] == "addr") and (controller_readwrite.ui_layout[i][1]) == None:
					controller_readwrite.ui_layout[i] = ("addr", self.config_params.rw_params.get_ADDR_BITS(), DIR_FANOUT)

				elif (controller_readwrite.ui_layout[i][0] == "data") and (controller_readwrite.ui_layout[i][1]) == None:
					controller_readwrite.ui_layout[i] = ("data", self.config_params.rw_params.DATA_BITS.value, DIR_FANOUT)
		populate_interface_with_configurable_widths()

		self.ui = Record(controller_readwrite.ui_layout)

		# add some default parameters. Could this be done better?
		if not hasattr(self.config_params, "burstlen"): 	self.config_params.burstlen = 8
		if not hasattr(self.config_params, "numbursts"):	self.config_params.numbursts = 2
		
	def elaborate(self, platform = None):
		
		m = Module()

		_ui = Record.like(self.ui)
		m.d.sync += self.ui.connect(_ui)

		ic_timing = self.config_params.ic_timing
		rw_params = self.config_params.rw_params

		row = Signal(rw_params.ROW_BITS.value)
		col = Signal(rw_params.COL_BITS.value)
		bank = Signal(rw_params.BANK_BITS.value)
		data = Signal(rw_params.DATA_BITS.value)

		def link_row_column_and_bank_to_address():
			burst_bits = bits_for(self.config_params.burstlen-1)
			m.d.sync += [
				Cat(col[:burst_bits], bank, col[burst_bits:], row).eq(_ui.addr),
				data.eq(_ui.data)
			]

		link_row_column_and_bank_to_address()

		if isinstance(self.utest, FHDLTestCase):
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()

			if test_id == "readwriteCtrl_sim_thatGivenAddress_generatesCorrectBankColumnAndRowValues":
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
		class readwriteCtrl_sim_thatGivenAddress_generatesCorrectBankColumnAndRowValues(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params

				utest_params = Params()

				dut = controller_readwrite(config_params, utest_params, utest=self)

				sim = Simulator(dut)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")

				# sdram_model = model_sdram(config_params, utest_params)

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