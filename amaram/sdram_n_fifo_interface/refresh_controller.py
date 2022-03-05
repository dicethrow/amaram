
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
from .ic_parameters import ic_timing, cmd_to_ic
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
		# ("uninitialised",	1,			DIR_FANIN),	# high until set low later on
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

		m = Module()

		# these four lines allow the concise delayer. ...() structure below
		m.submodules.delayer = delayer = Delayer(clk_freq=self.core.clk_freq)
		delayer_ui = Record.like(delayer.ui)
		m.d.sync += delayer_ui.connect(delayer.ui)
		delayer.set_m_and_ui_to_use(m, delayer_ui)

		_ui = Record.like(self.ui)

		m.d.sync += [
			self.ui.connect(_ui, exclude=["ios"]),
			_ui.ios.connect(self.ui.ios), # so 'fanout' signals go the right way etc
		]

		# default io values
		m.d.sync += [
			_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_NOP),
			_ui.ios.o_clk_en.eq(1)
		]

		with m.FSM(domain="sdram", name="refresh_controller_fsm"):

			refreshes_to_do = Signal(shape=bits_for(5-1), reset=5) #Const(5)
			refresh_level = Signal(shape=bits_for(self.clks_per_period), reset=self.clks_per_period) # is it valid to assume it starts 'full'..??
		
			m.d.sync += refresh_level.eq(Mux(refresh_level > 0, refresh_level - 1, 0))

			with m.State("AFTER_RESET"):
				with m.If(self.initialise_and_load_mode_register()):
					m.next = "REQUEST_REFRESH_SOON"

			with m.State("READY_FOR_NORMAL_OPERATION"):
				# at this point, the sdram chip is available for normal read/write operation
				with m.If(delayer.delay_for_clks(self.increment_per_refresh - (self.clks_per_period-refresh_level))):
					m.d.sync += refreshes_to_do.eq(1)
					m.next = "REQUEST_REFRESH_SOON"

			with m.State("REQUEST_REFRESH_SOON"):
				m.d.sync += _ui.request_to_refresh_soon.eq(1)
				with m.If(_ui.trigger_refresh):
					m.d.sync += _ui.refresh_in_progress.eq(1)
					m.next = "DO_ANOTHER_REFRESH?"

			with m.State("DO_ANOTHER_REFRESH?"):
				m.d.sync += _ui.request_to_refresh_soon.eq(0)
				with m.If(refreshes_to_do > 0):
					m.d.sync += refreshes_to_do.eq(refreshes_to_do - 1) # so we only do one refresh normally
					m.next = "AUTO_REFRESH"

				with m.Else():
					# finish up here
					m.d.sync += _ui.refresh_in_progress.eq(0)
					m.next = "READY_FOR_NORMAL_OPERATION"
			
			with m.State("AUTO_REFRESH"):
				m.d.comb += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
				m.d.sync += [
					refresh_level.eq(Mux(
							refresh_level < (self.clks_per_period - self.increment_per_refresh),
							refresh_level + self.increment_per_refresh,
							self.clks_per_period))					
					]
					
				m.next = "AUTO_REFRESH_WAITING"
			with m.State("AUTO_REFRESH_WAITING"):
				with m.If(delayer.delay_for_time(dram_ic_timing.T_RC)):
					m.next = "DO_ANOTHER_REFRESH?"
		
		return m
	
	def initialise_and_load_mode_register(self):
		# replicating p. 22 of datasheet
		complete = Signal()
		with m.FSM(domain="sync", name="initialise_and_load_mode_register_fsm") as fsm:

			m.d.sync += complete.eq(fsm.ongoing("DONE"))

			with m.State("POWERUP"):
				m.next = "POWERUP_WAITING"
			with m.State("POWERUP_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_STARTUP)):
					m.next = "PRECH_BANKS"

			with m.State("PRECH_BANKS"):
				m.d.sync += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_PALL)
				m.next = "PRECH_BANKS_WAITING"
			with m.State("PRECH_BANKS_WAITING"):
				with m.If(delayer.delay_for_time(T_RP)):
					m.next = "AUTO_REFRESH_1"

			with m.State("AUTO_REFRESH_1"):
				m.d.sync += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
				m.next = "AUTO_REFRESH_1_WAITING"
			with m.State("AUTO_REFRESH_1_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_RC)):
					m.next = "AUTO_REFRESH_2"
			
			with m.State("AUTO_REFRESH_2"):
				m.d.sync += _ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
				m.next = "AUTO_REFRESH_2_WAITING"
			with m.State("AUTO_REFRESH_2_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_RC)):
					m.next = "LOAD_MODE_REG"

			with m.State("LOAD_MODE_REG"):
				m.d.sync += [
					_ui.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_MRS),
					_ui.ios.o_a[:10].eq(0b0000110011) # burst=8, sequential; latency=3
					# _ui.ios.o_a[:10].eq(0b0000110010) # burst=4, sequential; latency=3
					# _ui.ios.o_a[:10].eq(0b0000110001) # burst=2, sequential; latency=3
				]
				m.next = "LOAD_MODE_REG_WAITING"
			with m.State("LOAD_MODE_REG_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_MRD)):
					m.next = "DONE"

			with m.State("DONE"):
				# m.d.sync += _ui.uninitialised.eq(0)
				...
		
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
					m.d.sync += refresher.disable.eq(1) 
					m.next = "DO_USER_TASK"

			with m.State("DO_USER_TASK"):
				# this represents another module doing something
				# let's just wait until a refresh is requested
				with m.If(refresher.do_soon):
					# and immediately let it do it
					m.d.sync += refresher.disable.eq(0)
					m.next = "WAIT_FOR_REFRESH"
			
			with m.State("WAIT_FOR_REFRESH"):
				with m.If(refresher.done):
					# to prevent the refresher from doing stuff
					m.d.sync += refresher.disable.eq(1) 
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