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

from ic_parameters import ic_timing, cmd_to_ic, ic_refresh_timing
from pin_controller import pin_controller
from Delayer import Delayer

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



# refresh_controller_interface_layout = [
# 	# ("initialised", 	1, DIR_FANOUT), # is this the right dir? so initialised will flow out to subordinates.. sounds right
# 	("do_soon",	1, DIR_FANOUT), # oh this is elegant
# 	("disable",			1, DIR_FANIN)	# this should be how this is triggered
# 	("done",			1, DIR_FANOUT)	# this is how other modules know they can do their thing
# ]

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
		("enable_refresh",	1,			DIR_FANOUT),
		("refresh_in_progress",	1,		DIR_FANIN),
		("refresh_lapsed",	1,			DIR_FANIN) # to indicate whether data loss from a lack of refreshing has occurred
	]

	def __init__(self, clk_freq, utest: FHDLTestCase = None):
		super().__init__()
		self.utest = utest
		self.clk_freq = clk_freq

		self.ui = Record(refresh_controller.ui_layout)
		self.pin_controller_ui = Record(pin_controller.ui)

		# period_s = 32e-3 # todo - make these defined elsewhere, not magic numbers
		self.clks_per_period = int(np.ceil(ic_refresh_timing.T_REF.value * self.clk_freq))
		self.increment_per_refresh = int(self.clks_per_period / ic_refresh_timing.NUM_REF.value)

	def elaborate(self, platform = None):
		
		m = Module()

		# these four lines allow the concise delayer. ...() structure below
		m.submodules.delayer = delayer = Delayer(clk_freq=self.clk_freq)
		delayer_ui = Record.like(delayer.ui)
		m.d.sync += delayer_ui.connect(delayer.ui)
		delayer.set_m_and_ui_to_use(m, delayer_ui)

		_ui = Record.like(self.ui)
		_pin_controller_ui = Record.like(self.pin_controller_ui)

		m.d.sync += [
			self.ui.connect(_ui),
			# self.ui.ios.connect(_ui.ios) # does not cause errors.... hmm
			# _ui.ios.connect(self.ui.ios), # so 'fanout' signals go the right way etc # note this line causes driver conflict
			_pin_controller_ui.connect(self.pin_controller_ui) # so 'fanout' signals go the right way etc
		]

		# default io values
		m.d.sync += [
			_pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_NOP),
			_pin_controller_ui.ios.o_clk_en.eq(1)
		]

		with m.FSM(domain="sync", name="refresh_controller_fsm") as fsm:

			 # up to 8192. set the level lower down, based on refresh level
			refreshes_to_do = Signal(shape=bits_for(ic_refresh_timing.NUM_REF.value))

			# assuming refreshing exists to preserve data that has already been loaded,
			# i.e. after reset, there is no data yet to preserve, so the reset value reflects this 
			refresh_level = Signal(shape=bits_for(self.clks_per_period), reset=self.clks_per_period)
		
			m.d.sync += [
				refresh_level.eq(Mux(refresh_level > 0, refresh_level - 1, 0)),

				# so if refresh_level is 0, then refreshes_to_do needs to be 8192. note that 8192 is 1<<13
				# and if refresh_level is self.clks_per_period, then refreshes_to_do would be zero. 
				# so we should scale the highest 14 bytes of self.clks_per_period-refresh_level and set it here,
				# in order to get a number between 0 and 8192. Note that this will round down, which provides a 
				# way to actually reach zero and so finish periodically.
				refreshes_to_do.eq((self.clks_per_period-refresh_level)[-14:]),

				# provide an external indicator, e.g. for a LED or some error flag
				_ui.refresh_lapsed.eq(fsm.ongoing("ERROR_REFRESH_LAPSED"))
			]

			with m.State("AFTER_RESET"):
				def initialise_and_load_mode_register():
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
							m.d.sync += _pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_PALL)
							m.next = "PRECH_BANKS_WAITING"
						with m.State("PRECH_BANKS_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RP)):
								m.next = "AUTO_REFRESH_1"

						with m.State("AUTO_REFRESH_1"):
							m.d.sync += _pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_REF)
							m.next = "AUTO_REFRESH_1_WAITING"
						with m.State("AUTO_REFRESH_1_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RC)):
								m.next = "AUTO_REFRESH_2"
						
						with m.State("AUTO_REFRESH_2"):
							m.d.sync += _pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_REF)
							m.next = "AUTO_REFRESH_2_WAITING"
						with m.State("AUTO_REFRESH_2_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RC)):
								m.next = "LOAD_MODE_REG"

						with m.State("LOAD_MODE_REG"):
							m.d.sync += [
								_pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_MRS),
								_pin_controller_ui.ios.o_a[:10].eq(0b0000110011) # burst=8, sequential; latency=3
								# _pin_controller_ui.ios.o_a[:10].eq(0b0000110010) # burst=4, sequential; latency=3
								# _pin_controller_ui.ios.o_a[:10].eq(0b0000110001) # burst=2, sequential; latency=3
							]
							m.next = "LOAD_MODE_REG_WAITING"
						with m.State("LOAD_MODE_REG_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_MRD)):
								m.next = "DONE"

						with m.State("DONE"):
							# m.d.sync += _ui.uninitialised.eq(0)
							...
					
					return complete
				with m.If(initialise_and_load_mode_register()):
					m.next = "REQUEST_REFRESH_SOON"

			with m.State("READY_FOR_NORMAL_OPERATION"):
				# at this point, the sdram chip is available for normal read/write operation
				# with m.If(delayer.delay_for_clks(self.increment_per_refresh - (self.clks_per_period-refresh_level))):
				with m.If(refreshes_to_do > 0):
					# m.d.sync += refreshes_to_do.eq(1)
					m.next = "REQUEST_REFRESH_SOON"

			with m.State("REQUEST_REFRESH_SOON"):
				m.d.sync += _ui.request_to_refresh_soon.eq(1)
				with m.If(_ui.enable_refresh):
					m.d.sync += [
						_ui.refresh_in_progress.eq(1),
					]
					m.next = "DO_ANOTHER_REFRESH?"
				with m.Elif(refreshes_to_do == ic_refresh_timing.NUM_REF.value):
					m.next = "ERROR_REFRESH_LAPSED"
				
			with m.State("ERROR_REFRESH_LAPSED"): 
				# this means data loss occurred. but if there's no data, then it's fine.
				# However in this case, if we're now treating the data as lost, then
				# we should reset the refresh_level to max, i.e. otherwise we would
				# be wasting time preserving garbage data.
				m.d.sync += _ui.request_to_refresh_soon.eq(1)
				with m.If(_ui.enable_refresh):
					m.d.sync += [
						refresh_level.eq(refresh_level.reset),
						_ui.refresh_in_progress.eq(1),
					]
					m.next = "DO_ANOTHER_REFRESH?"

			with m.State("DO_ANOTHER_REFRESH?"):
				m.d.sync += _ui.request_to_refresh_soon.eq(0)
				with m.If(refreshes_to_do > 0):
					# m.d.sync += refreshes_to_do.eq(refreshes_to_do - 1) # so we only do one refresh normally
					m.next = "AUTO_REFRESH"

				with m.Else():
					# finish up here
					m.d.sync += _ui.refresh_in_progress.eq(0)
					m.next = "READY_FOR_NORMAL_OPERATION"
			
			with m.State("AUTO_REFRESH"):
				m.d.sync += [
					_pin_controller_ui.ios.o_cmd.eq(cmd_to_ic.CMDO_REF),
					refresh_level.eq(Mux(
							refresh_level < (self.clks_per_period - self.increment_per_refresh),
							refresh_level + self.increment_per_refresh,
							self.clks_per_period))					
					]
					
				m.next = "AUTO_REFRESH_WAITING"
			with m.State("AUTO_REFRESH_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_RC)):
					m.next = "DO_ANOTHER_REFRESH?"


		###### testing
		if isinstance(self.utest, FHDLTestCase):
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()
			
			if test_id == "test_refresh_control_expected_behaviour":
				assert platform == None, f"This is a time simulation, requiring a platform of None. Unexpected platform status of {platform}"

				with m.FSM(name="testbench_fsm") as fsm:
					# m.d.sync += [
					# 	_ui.tb_fanin_flags.in_start.eq(fsm.ongoing("START")),
					# 	_ui.tb_fanin_flags.in_done.eq(fsm.ongoing("DONE"))
					# ]

					with m.State("INITIAL"):
						m.next = "START"
					
					with m.State("START"):
						# m.d.sync += _ui.tb_fanout_flags.
						# with m.If(refresher_ui)
						...
						# just hang here for now, and look at the traces

					with m.State("DONE"):
						...

		elif isinstance(platform, ULX3S_85F_Platform): 
			...
		
		else:
			... # In this case, this means that a test is occuring and this is not the top-level module.

		
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
			("tb_fanin_flags", 	[
				("in_start",	1,	DIR_FANIN),
				("in_done",		1,	DIR_FANIN),
			]),
			("tb_fanout_flags",[
				("trigger",		1,	DIR_FANOUT)
			])
		] + refresh_controller.ui_layout

		def __init__(self, clk_freq, utest: FHDLTestCase = None):
			super().__init__()
			self.ui = Record(Testbench.ui_layout)
			self.clk_freq = clk_freq
			self.utest = utest
		
		def elaborate(self, platform = None):
			m = Module()

			m.submodules.refresher = refresher = refresh_controller(clk_freq=self.clk_freq)
			refresher_ui = Record.like(refresher.ui)
			m.d.sync += refresher_ui.connect(refresher.ui)

			_ui = Record.like(self.ui)
			m.d.sync += self.ui.connect(_ui)

			if isinstance(self.utest, FHDLTestCase):
				add_clock(m, "sync")
				# add_clock(m, "sync_1e6")
				test_id = self.utest.get_test_id()
				params = self.utest.params
				
				if test_id == "testDesiredInterface_withExpectedBehaviour":
					assert platform == None, f"This is a time simulation, requiring a platform of None. Unexpected platform status of {platform}"

					with m.FSM(name="testbench_fsm") as fsm:
						m.d.sync += [
							_ui.tb_fanin_flags.in_start.eq(fsm.ongoing("START")),
							_ui.tb_fanin_flags.in_done.eq(fsm.ongoing("DONE"))
						]

						with m.State("INITIAL"):
							m.next = "START"
						
						with m.State("START"):
							# m.d.sync += _ui.tb_fanout_flags.
							# with m.If(refresher_ui)
							...
							# just hang here for now, and look at the traces

						with m.State("DONE"):
							...
				elif test_id == "test_refresh_control_expected_behaviour":
					assert platform == None, f"This is a time simulation, requiring a platform of None. Unexpected platform status of {platform}"
					...

			elif isinstance(platform, ULX3S_85F_Platform): 
				...
			
			else:
				assert 0

			return m
		
	if args.action == "generate": # formal testing
		# todo: add a test for the refresh_lapsed state, and behaviour?
		...

	elif args.action == "simulate": # time-domain testing
		
		class test_refresh_control_expected_behaviour(FHDLTestCase):
			def test_sim(self):
				params = Params()
				self.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
				params.clk_freq = 143e6
				self.params = params
				dut = refresh_controller(clk_freq=params.clk_freq, utest=self)
				sim = Simulator(dut)
				sim.add_clock(period=1/params.clk_freq, domain="sync")

				# def wait_for_200us():
				# 	yield Delay(200e-6)
				# sim.add_process(wait_for_200us)

				def use_refresher_with_resource_blocking_task():

					def resource_blocking_task():
						# e.g. this uses the ic for other stuff, so it cannot be refreshed during this time
						period = 200e-6
						yield Delay(period) # how long could this be? make a test for that?
						self.timeout_runtime -= period

					refresh_count = 0
					yield Active()
					while self.timeout_runtime > 0:
						if not ( (yield dut.ui.refresh_in_progress) or (yield dut.ui.request_to_refresh_soon) ): # how do we prevent a refresh starting while we're in blocking_task()?
							yield from resource_blocking_task()

						if (yield dut.ui.request_to_refresh_soon):
							yield dut.ui.enable_refresh.eq(1) # note - this should be a multi-or ing thing to handle multiple requests
							# wait for it to fall
							while (yield dut.ui.request_to_refresh_soon):
								yield 
								self.timeout_runtime -= 1/params.clk_freq
							yield dut.ui.enable_refresh.eq(0) 
							refresh_count += 1								
						
						yield 
						self.timeout_runtime -= 1/params.clk_freq

						if refresh_count > 3:
							return
					
					if self.timeout_runtime <= 0:
						print("Timeout error!")
				sim.add_sync_process(use_refresher_with_resource_blocking_task)
				

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

