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
from _module_interfaces import controller_pin_interfaces

class controller_pin(Elaboratable):
	""" 
	6apr2022

	Other modules interact with this module through .ui,
	and this module sets up/connects the sdram, either the model or the chip.
	sounds good! hope it works out that nicely
	"""

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		self.ui = Record(controller_pin_interfaces.get_ui_layout(self.config_params))

		# if isinstance(self.utest, FHDLTestCase):
		if (self.utest_params.use_sdram_model if hasattr(self.utest_params, "use_sdram_model") else False):
			from model_sdram import model_sdram, model_sdram_as_module
			# put in the constructor so we can access the simulation processes
			self.sdram_model = model_sdram_as_module(self.config_params, self.utest_params)
	
	def get_sim_sync_processes(self):
		for process, domain in self.sdram_model.get_sim_sync_processes():
			yield process, domain

	def elaborate(self, platform = None):
		# is using 'comb' ok here?
		
		m = Module()

		ic_timing = self.config_params.ic_timing
		rw_params = self.config_params.rw_params

		# make inter-module interfaces

		# allow either the readwrite controller, or the refresh controller, to have access to the chip
		_ui = Record.like(self.ui.refresh) # or .readwrite
		with m.If(self.ui.bus_is_refresh_not_readwrite):
			m.d.sync += self.ui.refresh.connect(_ui)
		with m.Else():
			m.d.sync += self.ui.readwrite.connect(_ui)

		# If the controlling bus changed, then make sure that we indicate it on any readback signal...? 
		# with m.If(~Stable(self.ui.bus_is_refresh_not_RW)):
		# 	m.d.sync += [
		# 	]

		_io = Record(controller_pin_interfaces.get_io_layout(self.config_params))
	
		# route the common signals from _ui to _io, except .cmd. 
		# unless overwritten below (e.g. .a and .ba sometimes)
		# Add the unique signals in _io below
		m.d.comb += _ui.connect(_io, exclude=["cmd"])

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


		if (self.utest_params.use_sdram_model if hasattr(self.utest_params, "use_sdram_model") else False):
			# now connect up the sdram model
			m.submodules.sdram_model = self.sdram_model
			m.d.comb += self.sdram_model.io.clk.eq(~ClockSignal("sync"))
			m.d.comb += [ # comb or sync? sync would be more correct, for time buffering? or comb here, due to .sync above?
				self.sdram_model.io.clk_en.eq(_io.clk_en),
				self.sdram_model.io.dqm.eq(_io.dqm),

				self.sdram_model.io.cs.eq(_io.cs),
				self.sdram_model.io.we.eq(_io.we),
				self.sdram_model.io.ras.eq(_io.ras),
				self.sdram_model.io.cas.eq(_io.cas),

				self.sdram_model.io.a.eq(_io.rw_copi.a),
				self.sdram_model.io.ba.eq(_io.rw_copi.ba),
				self.sdram_model.io.dq_copi.eq(_io.rw_copi.dq),
				self.sdram_model.io.dq_copi_en.eq(_io.rw_copi.dq_oen), # not yet in use

				# these are the readback signals. Do these line up as expected?
				_io.rw_cipo.dq.eq(self.sdram_model.io.dq_cipo),
				_io.rw_cipo.dq_oen.eq(_io.rw_copi.dq_oen),
				_io.rw_cipo.ba.eq(_io.rw_copi.ba),
				_io.rw_cipo.a.eq(_io.rw_copi.a),
				_io.rw_cipo.read_active.eq(_io.rw_copi.read_active)
			]


		if isinstance(self.utest, FHDLTestCase):
			# add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()

			if test_id == "pinCtrl_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync":
				...
		
		elif isinstance(platform, ULX3S_85F_Platform): 
			# todo: test that this is correct, or a useful structure			
			sdram = platform.request("sdram")

			m.d.comb += sdram.clk.eq(~ClockSignal("sync"))
			m.d.comb += [ # or comb?
				# Set the chip output pins
				sdram.clk_en.eq(1),
				sdram.dqm.eq(Cat(_io.dqm, _io.dqm)),

				sdram.cs.eq(_io.cs),
				sdram.we.eq(_io.we),
				sdram.ras.eq(_io.ras),
				sdram.cas.eq(_io.cas),

				sdram.a.eq(_io.rw_copi.a),
				sdram.ba.eq(_io.rw_copi.ba),
				sdram.dq.o.eq(_io.rw_copi.dq),
				sdram.dq.oe.eq(_io.rw_copi.dq_oen), # not yet implementented

				# these are the readback signals. Do these line up as expected?
				_io.rw_cipo.dq.eq(sdram.dq.i),
				_io.rw_cipo.dq_oen.eq(_io.rw_copi.dq_oen),
				_io.rw_cipo.ba.eq(_io.rw_copi.ba),
				_io.rw_cipo.a.eq(_io.rw_copi.a),
				_io.rw_cipo.read_active.eq(_io.rw_copi.read_active),
			]
	
		# else:
		# 	... # This case means that a test is occuring and this is not the top-level module.
			
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

	# def get_tb_ui_layout(config_params):
	# 	ui_layout = [
	# 			("tb_fanin_flags", 	[
	# 				("in_normal_operation",		1,	DIR_FANIN),
	# 				("in_requesting_refresh",	1,	DIR_FANIN),
	# 				("in_performing_refresh",	1,	DIR_FANIN)
	# 			]),
	# 			("tb_fanout_flags",[
	# 				("trigger",		1,	DIR_FANOUT)
	# 			])
	# 		] + get_ui_layout(config_params)
	# 	return ui_layout

	class Testbench(Elaboratable):
		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
			super().__init__()
			# self.ui = Record(get_tb_ui_layout(config_params))

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

			# self.pin_ui = Record(controller_pin.get_ui_layout(self.config_params))

			# put in the constructor so we can access it from sim processes
			self.pin_ctrl = controller_pin(self.config_params, self.utest_params)

		def get_sim_sync_processes(self):
			for process, domain in self.pin_ctrl.get_sim_sync_processes():
				yield process, domain

			test_id = self.utest.get_test_id()
			if test_id == "pinCtrl_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync":
				def apply_each_cmd_and_strobe_other_signals():
					# let's default to holding clk_end high
					yield self.pin_ctrl.ui.refresh.clk_en.eq(1)
					yield self.pin_ctrl.ui.bus_is_refresh_not_readwrite.eq(1)

					# initial delay
					for i in range(10):
						yield 

					for cmd_state in  self.utest_params.test_cmds:
						# required by this command
						if cmd_state == sdram_cmds.CMD_SELF:
							yield self.pin_ctrl.ui.refresh.clk_en.eq(0) 

						yield self.pin_ctrl.ui.refresh.cmd.eq(cmd_state)
						yield self.pin_ctrl.ui.refresh.dqm.eq(-1)
						yield self.pin_ctrl.ui.refresh.rw_copi.dq.eq(-1)
						yield self.pin_ctrl.ui.refresh.rw_copi.a.eq(-1)
						yield self.pin_ctrl.ui.refresh.rw_copi.ba.eq(-1)

						yield

						yield self.pin_ctrl.ui.refresh.cmd.eq(0)
						yield self.pin_ctrl.ui.refresh.dqm.eq(0)
						yield self.pin_ctrl.ui.refresh.rw_copi.dq.eq(0)
						yield self.pin_ctrl.ui.refresh.rw_copi.a.eq(0)
						yield self.pin_ctrl.ui.refresh.rw_copi.ba.eq(0)

						# revert it back
						if cmd_state == sdram_cmds.CMD_SELF:
							yield self.pin_ctrl.ui.refresh.clk_en.eq(1) 
						yield

					# end delay
					for i in range(10):
						yield 

				yield apply_each_cmd_and_strobe_other_signals, "sync"


		def elaborate(self, platform = None):
			m = Module()

			m.submodules.pin_ctrl = self.pin_ctrl

			if isinstance(self.utest, FHDLTestCase):
				# add_clock(m, "sync")
				# add_clock(m, "sync_1e6")
				test_id = self.utest.get_test_id()
				
				# if test_id == "RefreshTestbench_sim_withSdramModelAndBlockingTask_modelStaysRefreshed":
				# 	...

			return m


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
				utest_params.test_cmds = sdram_cmds
				utest_params.use_sdram_model = True

				tb = Testbench(config_params, utest_params, utest=self)

				sim = Simulator(tb)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				for process, domain in tb.get_sim_sync_processes():
					print(process, domain)
					sim.add_sync_process(process, domain=domain)
				
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