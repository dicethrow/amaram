
from amaranth.hdl import (Memory, ClockDomain, ResetSignal,
	ClockSignal, Elaboratable, Module, Signal, Mux, Cat,
	Const, C, Shape, Array, Record, Value)
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
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

# from .base import dram_ic_timing, cmd_to_dram_ic, sdram_base, sdram_quad_timer
from .pin_controller import pin_controller
from ..common import Delayer

""" 
Refresh controller

Goals
	- To periodically 'refresh' the data stored in the sdram chip, as required by the datasheet
	- To keep track of timing and when the next refresh is due
		- And to indicate to other modules (mainly the fifo module) that the sdram is due for refresh,
		  by asserting 'request_soon'. 
		- The other modules should keep the ORed signal 'disable' low unless actively using the sdram, 
		  and should regularly check the 'request_soon' flag so they can transition to a state safe 
		  for refresh, and then assert 'disable'.
		- To indicate to other modules when any refresh is occuring or ends with the 'idle' flag,
		  so they are able to transition back into using the sdram.
"""



refresh_controller_interface_layout = [
	# ("initialised", 	1, DIR_FANOUT), # is this the right dir? so initialised will flow out to subordinates.. sounds right
	("do_soon",	1, DIR_FANOUT), # oh this is elegant
	("disable",			1, DIR_FANIN)	# this should be how this is triggered
	("done",			1, DIR_FANOUT)	# this is how other modules know they can do their thing
]

class refresh_controller(Elaboratable):
	""" 
	ah! make this controller do
	- initial power up, mode register set
	- refreshes, incl. self- and auto- (which may involve power saving?)

	todo 15nov2021 - make this handle delayed refreshes, 
	so the refresh requirements are never exceeded
	"""
	ui_layout = [
		("uninitialised",	1,			DIR_FANIN),	# high until set low later on
		("request_to_refresh_soon",	1,	DIR_FANIN),	# 
		("trigger_refresh",	1,			DIR_FANOUT),
		("refresh_in_progress",	1,		DIR_FANIN),
		("ios", pin_controller.ui)
	]

	def __init__(self, clk_freq, utest: FHDLTestCase = None):
		super().__init__()
		self.utest = utest
		self.clk_freq = clk_freq

		self.ui = Record(refresh_controller.ui_layout)

		period_s = 32e-3 # todo - make these defined elsewhere, not magic numbers
		refreshes_per_period = 8192
		self.clks_per_period = int((period_s * self.clk_freq) + 0.5)
		self.increment_per_refresh = int(self.clks_per_period / refreshes_per_period)

	def elaborate(self, platform = None):

		refreshes_to_do = Signal(shape=bits_for(5-1), reset=5) #Const(5)
		refresh_level = Signal(shape=bits_for(self.clks_per_period), reset=self.clks_per_period) # is it valid to assume it starts 'full'..??
		
		_ui = Record.like(self.ui)
		m.d.sync += [
			self.ui.connect(_ui, exclude=["ios"]),
			_ui.ios.connect(self.ui.ios) # so 'fanout' signals go the right way etc
		]
		
		self.m.d.sdram += [
			refresh_level.eq(Mux(refresh_level > 0, refresh_level - 1, 0))
		]

		# default to this always being true....?
		self.m.d.comb += _ui.ios.o_clk_en.eq(1)

		m.submodules.delayer = delayer = Delayer(clk_freq=self.core.clk_freq)
		_ui = Record.like(delayer.ui)

		m.d.sync += [
			_ui.connect(delayer.ui)
		]
		# with m.If(delayer.delay_for_time(m, _ui, self.utest.test_period)):

		super().elaborate(platform)
		with self.m.FSM(domain="sdram", name="refresh_controller_fsm"):
			with self.m.State("AFTER_RESET"):
				with self.m.If(self.initialise_and_load_mode_register(trigger = self.initialised == 0)):
					self.m.next = "INITIAL_SETUP"

			with self.m.State("INITIAL_SETUP"):
				with self.m.If(delayer.delay_for_time(m, _ui, 100e-6)):#4e-6)): # should it be 100us, but 4us for now to speed simulation up? define this elsewhere?
					self.m.next = "REQUEST_REFRESH_SOON"

			with self.m.State("READY_FOR_NORMAL_OPERATION"):
				# at this point, the sdram chip is available for normal read/write operation
				with self.m.If(delayer.delay_for_clks(m, _ui, self.increment_per_refresh - (self.clks_per_period-refresh_level))):
					self.m.d.sdram += refreshes_to_do.eq(1)
					self.m.next = "REQUEST_REFRESH_SOON"

			with self.m.State("REQUEST_REFRESH_SOON"):
				self.m.d.sdram += self.request_to_refresh_soon.eq(1)
				with self.m.If(self.trigger_refresh):
					self.m.d.sdram += self.refresh_in_progress.eq(1)
					self.m.next = "DO_ANOTHER_REFRESH?"

			with self.m.State("DO_ANOTHER_REFRESH?"):
				self.m.d.sdram += self.request_to_refresh_soon.eq(0)
				with self.m.If(refreshes_to_do > 0):
					self.m.d.sdram += [
						refreshes_to_do.eq(refreshes_to_do - 1) # so we only do one refresh normally
					]
					self.m.next = "AUTO_REFRESH"

				with self.m.Else():
					# finish up here
					self.m.d.sdram += [
						self.refresh_in_progress.eq(0),
					]
					self.m.next = "READY_FOR_NORMAL_OPERATION"
			
			with self.m.State("AUTO_REFRESH"):
				self.m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
				self.m.d.sdram += [
					refresh_level.eq(Mux(
							refresh_level < (self.clks_per_period - self.increment_per_refresh),
							refresh_level + self.increment_per_refresh,
							self.clks_per_period))					
					]
					
				self.m.next = "AUTO_REFRESH_WAITING"
			with self.m.State("AUTO_REFRESH_WAITING"):
				with self.m.If(delayer.delay_for_time(m, _ui, dram_ic_timing.T_RC)):
					self.m.next = "DO_ANOTHER_REFRESH?"
		
		return self.m
	
	def initialise_and_load_mode_register(self, trigger):
		# replicating p. 22 of datasheet
		complete = Signal()
		with self.m.If(trigger):
			with self.m.FSM(domain="sdram", name="initialise_and_load_mode_register_fsm"):

				with self.m.State("POWERUP"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += [
							_ui.ios.o_clk_en.eq(1),
							_ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
						]
						self.set_timer_delay(dram_ic_timing.T_STARTUP)
					with self.m.Else():
						self.m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "PRECH_BANKS"

				with self.m.State("PRECH_BANKS"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_PALL)
						self.set_timer_delay(dram_ic_timing.T_RP)
					with self.m.Else():
						self.m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "AUTO_REFRESH_1"

				with self.m.State("AUTO_REFRESH_1"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
						self.set_timer_delay(dram_ic_timing.T_RC)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "AUTO_REFRESH_2"

				with self.m.State("AUTO_REFRESH_2"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
						self.set_timer_delay(dram_ic_timing.T_RC)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "LOAD_MODE_REG"

				with self.m.State("LOAD_MODE_REG"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += [
							self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_MRS),
							self.ios.o_a[:10].eq(0b0000110011) # burst=8, sequential; latency=3
							# self.ios.o_a[:10].eq(0b0000110010) # burst=4, sequential; latency=3
							# self.ios.o_a[:10].eq(0b0000110001) # burst=2, sequential; latency=3
						]
						self.set_timer_delay(dram_ic_timing.T_MRD)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
						self.m.next = "DONE"

				with self.m.State("DONE"):
					self.m.d.comb += complete.eq(1)
					self.m.d.sdram += self.initialised.eq(1)
		
		return complete



class refresh_controller_tests(Elaboratable):
	""" 
	- To demonstrate and test timing of
		1. The startup phase, where this module initialises the sdram chip
		2. The initial refreshes
		3. The first assertion of 'idle', allowing other modules to start accessing the sdram
		4. After a timeout, the assertion of 'do_soon'
		5. Waiting for 'disable' to be deasserted
		6. Doing the refresh while deasserting `done`
		7. Reasserting `done`, then looping from step 4. (within a test time limit)

	- Be able to run the test on hardware, as well as in simulation

	- Test variable ideas
		- Vary interval between refreshes
		- Will missed refreshes occur? Skip this test for now

	"""
	def __init__(self):
		pass

	def elaborate(self, platform = None):
		m = Module()

		# todo: 
		#	put this somewhere better.
		refresher = Record(refresh_controller_interface_layout) # this is a subord right?
		# todo: connect it to the refresh controller itself
		
		with m.FSM(domain="sdram", name="refresher_test_fsm"):
			with m.State("AFTER_RESET"):
				with m.If(refresher.done):
					# to prevent the refresher from doing stuff
					m.d.sdram += refresher.disable.eq(1) 
					m.next = "DO_USER_TASK"

			with m.State("DO_USER_TASK"):
				# this represents another module doing something
				# let's just wait until a refresh is requested
				with m.If(refresher.do_soon):
					# and immediately let it do it
					m.d.sdram += refresher.disable.eq(0)
					m.next = "WAIT_FOR_REFRESH"
			
			with m.State("WAIT_FOR_REFRESH"):
				with m.If(refresher.done):
					# to prevent the refresher from doing stuff
					m.d.sdram += refresher.disable.eq(1) 
					m.next = "DO_USER_TASK"

		return m




if __name__ == "__main__":
	""" 
	17feb2022, 5mar2022

	Adding tests to each file, so I can more easily make 
	changes in order to improve timing performance.

	"""
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	class Testbench(Elaboratable):
		ui_layout = [

		]

		def __init__(self, clk_freq = 24e6, utest: FHDLTestCase = None):
			super().__init__()
			self.ui = Record(Testbench.Testbench_ui_layout)
			self.clk_freq = clk_freq
			self.utest = utest
		
		def elaborate(self, platform = None):
			m = Module()

			m.submodules.refresh_ctrl = refresh_ctrl = refresh_controller(core)