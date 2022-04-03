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

from parameters_standard_sdram import sdram_cmds, rw_cmds

def get_rw_pipeline_layout(config_params, _dir):
	# this is to enable the ability to read back pipelined data easily

	rw_pipeline_layout = [
		("dq",			config_params.rw_params.DATA_BITS.value,		_dir),
		("read_active",	1,			_dir),		# whether or not a read will be active on the dq bus in read mode
		("a",			config_params.rw_params.A_BITS.value,			_dir),
		("ba",			config_params.rw_params.BANK_BITS.value,		_dir),
		("addr",		config_params.rw_params.get_ADDR_BITS(),		_dir),
	]

	return rw_pipeline_layout

def get_common_io_layout(config_params):
	io_layout_common = [
		("clk_en", 		1,		DIR_FANOUT),
		("dqm",			1, 		DIR_FANOUT),

		("rw_copi", 	get_rw_pipeline_layout(config_params, DIR_FANOUT)), 
		("rw_cipo", 	get_rw_pipeline_layout(config_params, DIR_FANIN)),  
	]
	return io_layout_common

def get_ui_layout(config_params):
	# this represents the inter-module user interface
	ui_layout = [
		("cmd", sdram_cmds, 	DIR_FANOUT), # a high-level representation of the desired cmd
	] + get_common_io_layout(config_params)

	return ui_layout

def get_io_layout(config_params):
	# this is between the inter-module ui and the pins of the sdram chip
	io_layout = [
		("cs",			1,		DIR_FANOUT),
		("we",			1,		DIR_FANOUT),
		("ras",			1,		DIR_FANOUT),
		("cas",			1,		DIR_FANOUT)
	] + get_common_io_layout(config_params)

	return io_layout

class controller_pin(Elaboratable):

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		self.ui = Record(get_ui_layout(self.config_params))
		self.io = Record(get_io_layout(self.config_params))

		if isinstance(self.utest, FHDLTestCase):
			from model_sdram import model_sdram, model_sdram_as_module
			# put in the constructor so we can access the simulation processes
			self.sdram_model = model_sdram_as_module(self.config_params, self.utest_params)
	
	def get_sim_sync_processes(self):
		...

	def elaborate(self, platform = None):
		# is using 'comb' ok here?
		
		m = Module()

		ic_timing = self.config_params.ic_timing
		rw_params = self.config_params.rw_params

		# make inter-module interfaces
		_ui = Record.like(self.ui)
		_io = Record.like(self.io)
		m.d.sync += [
			self.ui.connect(_ui),
			_io.connect(self.io)
		]

		# route most signals from _ui to _io,
		# unless overwritten below (e.g. .a and .ba sometimes)
		m.d.comb += _ui.connect(_io, exclude=["cmd"])#, exclude=["cs", "we", "ras", "cas"])

		# decode the _ui cmd into the _io signals
		with m.Switch(_ui.cmd):
			with m.Case(sdram_cmds.CMD_DESL):
				m.d.comb += [
					_io.cs.eq(0)
				]
			
			with m.Case(sdram_cmds.CMD_NOP):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(0),
					_io.we.eq(0)
				]
				
			with m.Case(sdram_cmds.CMD_BST):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(0),
					_io.we.eq(1)
				]
				
			with m.Case(sdram_cmds.CMD_READ):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(1),
					_io.we.eq(0),
					_io.rw_copi.a[10].eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with m.Case(sdram_cmds.CMD_READ_AP):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(1),
					_io.we.eq(0),
					_io.rw_copi.a[10].eq(1)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with m.Case(sdram_cmds.CMD_WRITE):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(1),
					_io.we.eq(1),
					_io.rw_copi.a[10].eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with m.Case(sdram_cmds.CMD_WRITE_AP):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(0),
					_io.cas.eq(1),
					_io.we.eq(1),
					_io.rw_copi.a[10].eq(1)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with m.Case(sdram_cmds.CMD_ACT):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(0),
					_io.we.eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with m.Case(sdram_cmds.CMD_PRE):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(0),
					_io.we.eq(1),
					_io.rw_copi.a[10].eq(0)
					# self.ba needs to be set too
				]
				
			with m.Case(sdram_cmds.CMD_PALL):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(0),
					_io.we.eq(1),
					_io.rw_copi.a[10].eq(1)
				]
				
			with m.Case(sdram_cmds.CMD_REF):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(1),
					_io.we.eq(0)
					# clk_en needs to be 1, rather than just on the previous cycle
				]
				
			with m.Case(sdram_cmds.CMD_SELF):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(1),
					_io.we.eq(0)
					# clk_en needs to be 0, and 1 on the previous cycle
				]
				
			with m.Case(sdram_cmds.CMD_MRS):
				m.d.comb += [
					_io.cs.eq(1),
					_io.ras.eq(1),
					_io.cas.eq(1),
					_io.we.eq(1),
					_io.rw_copi.ba.eq(0b00),
					_io.rw_copi.a[10].eq(0)
					# and self.a[:10] needs to be valid with the desired register bits
				]

		if isinstance(self.utest, FHDLTestCase):
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()

			# now connect up the sdram model
			m.submodules.sdram_model = self.sdram_model
			m.d.sync += [ # comb or sync? sync would be more correct...
				self.sdram_model.io.clk_en.eq(self.io.clk_en),
				self.sdram_model.io.dqm.eq(self.io.dqm),

				self.sdram_model.io.cs.eq(self.io.cs),
				self.sdram_model.io.we.eq(self.io.we),
				self.sdram_model.io.ras.eq(self.io.ras),
				self.sdram_model.io.cas.eq(self.io.cas),

				self.sdram_model.io.a.eq(self.io.rw_copi.a),
				self.sdram_model.io.ba.eq(self.io.rw_copi.ba),
				self.sdram_model.io.dq_copi.eq(self.io.rw_copi.dq),
				# self.sdram_model.io.dq_copi_en.eq(), # not yet in use

				# these are the readback signals. Do these line up as expected?
				self.io.rw_cipo.dq.eq(self.sdram_model.io.dq_cipo),
				self.io.rw_cipo.ba.eq(self.io.rw_copi.ba),
				self.io.rw_cipo.a.eq(self.io.rw_copi.a),
				self.io.rw_cipo.read_active.eq(self.io.rw_copi.read_active)

			]

			if test_id == "pinCtrl_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync":
				...
			
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
		class pinCtrl_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params

				utest_params = Params()

				dut = controller_pin(config_params, utest_params, utest=self)

				sim = Simulator(dut)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")

				def apply_each_cmd_and_strobe_other_signals():
					# let's default to holding clk_end high
					yield dut.ui.clk_en.eq(1)

					# initial delay
					for i in range(10):
						yield 

					for cmd_state in sdram_cmds:
						# required by this command
						if cmd_state == sdram_cmds.CMD_SELF:
							yield dut.ui.clk_en.eq(0) 

						yield dut.ui.cmd.eq(cmd_state)
						yield dut.ui.dqm.eq(-1)
						yield dut.ui.rw_copi.dq.eq(-1)
						yield dut.ui.rw_copi.a.eq(-1)
						yield dut.ui.rw_copi.ba.eq(-1)

						yield

						yield dut.ui.cmd.eq(0)
						yield dut.ui.dqm.eq(0)
						yield dut.ui.rw_copi.dq.eq(0)
						yield dut.ui.rw_copi.a.eq(0)
						yield dut.ui.rw_copi.ba.eq(0)

						# revert it back
						if cmd_state == sdram_cmds.CMD_SELF:
							yield dut.ui.clk_en.eq(1) 
						yield

					# end delay
					for i in range(10):
						yield 

				sim.add_sync_process(apply_each_cmd_and_strobe_other_signals)
				
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