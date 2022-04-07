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
from _module_interfaces import controller_pin_interfaces, controller_refresh_interfaces

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

class controller_refresh(Elaboratable):
	""" 
	ah! make this controller do
	- initial power up, mode register set
	- refreshes, incl. self- and auto- (which may involve power saving?)

	todo 15nov2021 - make this handle delayed refreshes, 
	so the refresh requirements are never exceeded
	"""
	

	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()
		self.controller_pin_ui = Record(controller_pin_interfaces.get_sub_ui_layout(config_params))
		self.ui = Record(controller_refresh_interfaces.get_ui_layout(config_params))

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest
		
		self.clks_per_period = int(np.ceil(self.config_params.ic_refresh_timing.T_REF.value * self.config_params.clk_freq))
		self.increment_per_refresh = int(self.clks_per_period / self.config_params.ic_refresh_timing.NUM_REF.value)

	def elaborate(self, platform = None):
		
		m = Module()

		ic_timing = self.config_params.ic_timing
		ic_refresh_timing = self.config_params.ic_refresh_timing

		# these four lines allow the concise delayer. ...() structure below
		m.submodules.delayer = delayer = Delayer(clk_freq=self.config_params.clk_freq)
		delayer_ui = Record.like(delayer.ui)
		m.d.sync += delayer_ui.connect(delayer.ui)
		delayer.set_m_and_ui_to_use(m, delayer_ui)

		_ui = Record.like(self.ui)
		_controller_pin_ui = Record.like(self.controller_pin_ui)

		m.d.sync += [
			self.ui.connect(_ui),
			# self.ui.connect(_ui.ios) # does not cause errors.... hmm
			# _ui.connect(self.ui.ios), # so 'fanout' signals go the right way etc # note this line causes driver conflict
			_controller_pin_ui.connect(self.controller_pin_ui) # so 'fanout' signals go the right way etc
		]

		# default io values
		m.d.sync += [
			_controller_pin_ui.cmd.eq(sdram_cmds.CMD_NOP),
			_controller_pin_ui.clk_en.eq(1)
		]

		with m.FSM(domain="sync", name="controller_refresh_fsm") as fsm:

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
					with m.FSM(domain="sync", name="initialise_and_load_mode_register_fsm") as fsm:

						m.d.sync += _ui.initialised.eq(fsm.ongoing("DONE"))

						with m.State("POWERUP"):
							m.next = "POWERUP_WAITING"
						with m.State("POWERUP_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_STARTUP)):
								m.next = "PRECH_BANKS"

						with m.State("PRECH_BANKS"):
							m.d.sync += _controller_pin_ui.cmd.eq(sdram_cmds.CMD_PALL)
							m.next = "PRECH_BANKS_WAITING"
						with m.State("PRECH_BANKS_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RP)):
								m.next = "AUTO_REFRESH_1"

						with m.State("AUTO_REFRESH_1"):
							m.d.sync += _controller_pin_ui.cmd.eq(sdram_cmds.CMD_REF)
							m.next = "AUTO_REFRESH_1_WAITING"
						with m.State("AUTO_REFRESH_1_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RC)):
								m.next = "AUTO_REFRESH_2"
						
						with m.State("AUTO_REFRESH_2"):
							m.d.sync += _controller_pin_ui.cmd.eq(sdram_cmds.CMD_REF)
							m.next = "AUTO_REFRESH_2_WAITING"
						with m.State("AUTO_REFRESH_2_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_RC)):
								m.next = "LOAD_MODE_REG"

						with m.State("LOAD_MODE_REG"):
							m.d.sync += [
								_controller_pin_ui.cmd.eq(sdram_cmds.CMD_MRS),
								_controller_pin_ui.rw_copi.a[:10].eq(0b0000110011) # burst=8, sequential; latency=3
								# _controller_pin_ui.a[:10].eq(0b0000110010) # burst=4, sequential; latency=3
								# _controller_pin_ui.a[:10].eq(0b0000110001) # burst=2, sequential; latency=3
							]
							m.next = "LOAD_MODE_REG_WAITING"
						with m.State("LOAD_MODE_REG_WAITING"):
							with m.If(delayer.delay_for_time(ic_timing.T_MRD)):
								m.next = "DONE"

						with m.State("DONE"):
							# m.d.sync += _ui.uninitialised.eq(0)
							...
					
					return _ui.initialised
				with m.If(initialise_and_load_mode_register()):
					m.d.sync += refreshes_to_do.eq(0) 
					m.next = "READY_FOR_NORMAL_OPERATION"


			with m.State("READY_FOR_NORMAL_OPERATION"):
				# at this point, the sdram chip is available for normal read/write operation
				# with m.If(delayer.delay_for_clks(self.increment_per_refresh - (self.clks_per_period-refresh_level))):
				# with m.If(refreshes_to_do > int(0.5 * ic_refresh_timing.NUM_REF.value)): # if we're at 50% refresh level,
				with m.If(refreshes_to_do > 10): # let's arbitarily set 10 as our threshold
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
					with m.If(~_ui.initialised):
						m.d.sync += [
							refresh_level.eq(refresh_level.reset),
							 _ui.initialised.eq(1)
						]
					m.next = "READY_FOR_NORMAL_OPERATION"
			
			with m.State("AUTO_REFRESH"):
				m.d.sync += [
					_controller_pin_ui.cmd.eq(sdram_cmds.CMD_REF),
					refresh_level.eq(Mux(
							refresh_level < (self.clks_per_period - self.increment_per_refresh),
							refresh_level + self.increment_per_refresh,
							self.clks_per_period))					
					]
					
				m.next = "AUTO_REFRESH_WAITING"
			with m.State("AUTO_REFRESH_WAITING"):
				with m.If(delayer.delay_for_time(ic_timing.T_RC)):
					m.next = "DO_ANOTHER_REFRESH?"

		
		return m
	

if __name__ == "__main__":
	""" 
	17feb2022, 5mar2022, 7mar2022

	Adding tests to each file, so I can more easily make 
	changes in order to improve timing performance.

	"""
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	class Testbench(Elaboratable):
		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
			super().__init__()

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

			# put in constructor so we can access in simulation processes
			self.refresher = controller_refresh(self.config_params)
			self.pin_ctrl = controller_pin(self.config_params, self.utest_params)

		def get_sim_sync_processes(self):
			for process, domain in self.pin_ctrl.get_sim_sync_processes():
				yield process, domain
				
			def use_refresher_with_resource_blocking_task():
				def resource_blocking_task():
					# e.g. this uses the ic for other stuff, so it cannot be refreshed during this time
					period = 200e-6
					yield Delay(period) # how long could this be? make a test for that?
					self.utest_params.timeout_runtime -= period

				refresh_count = 0
				yield Active()
				while self.utest_params.timeout_runtime > 0:
					if not ( (yield self.refresher.ui.refresh_in_progress) or (yield self.refresher.ui.request_to_refresh_soon) ): # how do we prevent a refresh starting while we're in blocking_task()?
						yield from resource_blocking_task()

					if (yield self.refresher.ui.request_to_refresh_soon):
						yield self.refresher.ui.enable_refresh.eq(1) # note - this should be a multi-or ing thing to handle multiple requests
						# wait for it to fall
						while (yield self.refresher.ui.request_to_refresh_soon):
							yield 
							self.utest_params.timeout_runtime -= 1/self.config_params.clk_freq
						yield self.refresher.ui.enable_refresh.eq(0) 
						refresh_count += 1								
					
					yield 
					self.utest_params.timeout_runtime -= 1/self.config_params.clk_freq

					if refresh_count > 3:
						return
				
				if self.utest_params.timeout_runtime <= 0:
					print("Timeout error!")


			test_id = self.utest.get_test_id()
			if test_id == "RefreshCtrl_sim_withModelAndBlockingTask_modelStaysRefreshed":
				yield use_refresher_with_resource_blocking_task, "sync"
			
		def elaborate(self, platform = None):
			m = Module()

			m.submodules.refresher = self.refresher

			m.submodules.pin_ctrl = self.pin_ctrl

			# connect the bus-selection mechanism
			placeholder_record = Record.like(self.refresher.controller_pin_ui)
			m.d.sync += [
				self.pin_ctrl.ui.bus_is_refresh_not_readwrite.eq(self.refresher.ui.enable_refresh | self.refresher.ui.refresh_in_progress),
				self.refresher.controller_pin_ui.connect(self.pin_ctrl.ui.refresh),
				placeholder_record.connect(self.pin_ctrl.ui.readwrite)
			]

			if isinstance(self.utest, FHDLTestCase):
				add_clock(m, "sync")
				# add_clock(m, "sync_1e6")
				test_id = self.utest.get_test_id()
				
				# if test_id == "RefreshTestbench_sim_withSdramModelAndBlockingTask_modelStaysRefreshed":
				# 	...


			# elif isinstance(platform, ULX3S_85F_Platform): 
			# 	...
			

			return m
		
	if args.action == "generate": # formal testing
		# todo: add a test for the refresh_lapsed state, and behaviour?
		...

	elif args.action == "simulate": # time-domain testing

		class RefreshCtrl_sim_withModelAndBlockingTask_modelStaysRefreshed(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params

				utest_params = Params()
				utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
				utest_params.use_sdram_model = True

				tb = Testbench(config_params, utest_params, utest=self)

				sim = Simulator(tb)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				for process, domain in tb.get_sim_sync_processes():
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
		class Upload(UploadBase):
			def elaborate(self, platform = None):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params

				self.config_params.sync_mode = "sync_and_143e6_sdram_from_pll"
				self.config_params.clk_freq = 143e6
				self.config_params.ic_timing = ic_timing
				self.config_params.ic_refresh_timing = ic_refresh_timing
				self.config_params.rw_params = rw_params

				m = super().elaborate(platform) 

				m.submodules.tb = tb = DomainRenamer("sdram")(Testbench(self.config_params))

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

				return m
		
		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")

