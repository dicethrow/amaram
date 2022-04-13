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
from amaranth.lib.fifo import SyncFIFO, AsyncFIFO#, AsyncFIFOBuffered, SyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer
from amaranth.lib.cdc import FFSynchronizer
from amaranth.build import Platform
from amaranth.utils import bits_for

from amaranth_boards.ulx3s import ULX3S_85F_Platform

from amlib.io import SPIRegisterInterface, SPIDeviceInterface, SPIDeviceBus, SPIMultiplexer
from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amlib.utils import Timer

from amtest.boards.ulx3s.common.upload import platform, UploadBase
from amtest.boards.ulx3s.common.clks import add_clock
from amtest.utils import FHDLTestCase, Params

from amaram.interface_fifo import interface_fifo

sys.path.append(os.path.join(os.getcwd(), "tests", "ulx3s_counter"))
from common import fpga_mcu_interface



def get_test_ui_layout(config_params):
	ui_layout = [
			("tb_fanin_flags", 	[
				("fsm_state",		5,		DIR_FANIN),
				("trigger_rdy",		1,		DIR_FANIN),
				("fifo_read_rdy",	1,		DIR_FANIN),
				("write_counter",	16,		DIR_FANIN)
			]),
			("tb_fanout_flags",[
				("trigger",			1,		DIR_FANOUT)
			])
		] #+ get_ui_layout(config_params)
	return ui_layout

class fifo_test_core(Elaboratable):
	def __init__(self, config_params, utest_params = None):
		super().__init__()
		self.config_params = config_params
		self.utest_params = utest_params
		
		self.ui = Record(get_test_ui_layout(self.config_params))
		self.spi = SPIDeviceBus()

		# put in constructor so we can access in simulation processes
		self.interface_fifo = DomainRenamer("sdram")(interface_fifo(self.config_params, self.utest_params))
	
	def elaborate(self, platform = None):
		m = Module()

		m.submodules.interface_fifo = self.interface_fifo


		# Fill a fifo with sequential values from a counter
		# Store the data in sdram
		# Read it back using a spi fifo-reader thing (on the esp32)
		# Are the read back values correct? How robust is the performance?

		transaction_start = Rose(self.spi.cs)
		transaction_end = Fell(self.spi.cs)

		# Connect up our SPI transciever to our public interface.
		m.submodules.spi_interface = spi_interface = SPIDeviceInterface(
			word_size=self.interface_fifo.ui_fifo.w_data.width, # 16bits
			clock_polarity=self.config_params.spi_clock_polarity,
			clock_phase=self.config_params.spi_clock_phase
		)
		m.d.comb += [
			spi_interface.spi.connect(self.spi),
			spi_interface.word_out.eq(self.interface_fifo.ui_fifo.r_data)
			# spi_interface.word_out.eq(0xF0FF)
		]
		
		with m.FSM(name="testbench_fsm") as fsm:

			write_counter = Signal.like(self.interface_fifo.ui_fifo.w_data)
			m.d.comb += self.ui.tb_fanin_flags.write_counter.eq(write_counter)

			m.d.sync += [
				self.ui.tb_fanin_flags.fsm_state.eq(Cat(fsm.ongoing("IDLE"), fsm.ongoing("WRITE_TO_FIFO"),
					fsm.ongoing("READABLE"), fsm.ongoing("READ_FROM_FIFO"), fsm.ongoing("DONE"))),
				self.ui.tb_fanin_flags.trigger_rdy.eq(fsm.ongoing("IDLE")),
				self.ui.tb_fanin_flags.fifo_read_rdy.eq(fsm.ongoing("READABLE") | fsm.ongoing("READ_FROM_FIFO")),

				# spi_interface.word_out.eq(self.interface_fifo.ui_fifo.r_data)
				# spi_interface.word_out.eq(write_counter)
			]
			
			with m.State("IDLE"):
				m.d.sync += write_counter.eq(0)
				with m.If(self.ui.tb_fanout_flags.trigger):
					m.next = "WRITE_TO_FIFO"
			
			with m.State("WRITE_TO_FIFO"):
				with m.If(write_counter == self.utest_params.num_fifo_writes):
					m.d.comb += self.interface_fifo.ui_fifo.w_en.eq(0)
					# m.d.sync += write_counter.eq(write_counter -1)
					m.next = "READABLE"
				
				with m.Else():
					m.d.comb += self.interface_fifo.ui_fifo.w_en.eq(self.interface_fifo.ui_fifo.w_rdy)
					m.d.sync += write_counter.eq(write_counter + 1) # not great! misses stuff!
					with m.If(self.interface_fifo.ui_fifo.w_rdy):
					
						m.d.sync += self.interface_fifo.ui_fifo.w_data.eq(write_counter)

			with m.State("READABLE"):
				with m.If(transaction_start):
					m.next = "READ_FROM_FIFO"
			
			with m.State("READ_FROM_FIFO"):
				with m.If(Rose(spi_interface.word_accepted)):
					# m.d.sync += write_counter.eq(write_counter -1)
					with m.If(self.interface_fifo.ui_fifo.r_rdy):
						m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(1)
					with m.Else():
						m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(0)
						# self.m.next = "DONE"#"ERROR"
				with m.Else():
					m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(0)

				with m.If(transaction_end):
				# with m.If(write_counter == 0):
					m.next = "DONE"				

			with m.State("DONE"):
				# ensure the fifo is empty, then go back to idle
				with m.If(self.interface_fifo.ui_fifo.r_rdy):
					m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(1)
				with m.Else():
					m.d.comb += self.interface_fifo.ui_fifo.r_en.eq(0)
					m.next = "IDLE"

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

	class Testbench(Elaboratable):
		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None,
			copi = None, cipo = None, sclk = None, cs = None, csn = None):
			super().__init__()

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

			self.ui = Record(get_test_ui_layout(self.config_params))
			self.spi = SPIDeviceBus()

			# put in constructor so we can access in simulation processes
			self.fifo_test = DomainRenamer(self.config_params.fifo_write_domain)(fifo_test_core(self.config_params, self.utest_params))


			# set up spi stuff
			self.copi = copi 
			self.cipo = cipo
			self.sclk = sclk
			if type(cs) != type(None):
				self.invert_csn = False
				self.cs = cs
			else:
				self.invert_csn = True
				self.cs = Signal()		

		# def get_sim_sync_processes(self):
		# 	for process, domain in self.interface_fifo.get_sim_sync_processes():
		# 		yield process, domain

			# test_id = self.utest.get_test_id()
			# if test_id == "fifoInterfaceTb_sim_thatWrittenFifos_canBeReadBack":
			# 	...

		def elaborate(self, platform = None):

			def handle_cs_or_csn():
				# to deal with the inverted cs pin on the ulx3s, but not in simulation
				if self.invert_csn:
					m.d.comb += self.cs.eq(~self.csn)
			
			def add_register_interface():
				# Create a set of registers...
				spi_registers = SPIRegisterInterface(
					address_size=fpga_mcu_interface.spi_register_interface.CMD_ADDR_BITS, # and first bit for write or not
					register_size=fpga_mcu_interface.spi_register_interface.REG_DATA_BITS, # to match the desired fifo width for later on
				)
				m.submodules += spi_registers
			
				addrs = fpga_mcu_interface.register_addresses

				trigger_rdy = Signal.like(self.ui.tb_fanin_flags.trigger_rdy)
				m.submodules += FFSynchronizer(o=trigger_rdy, i=self.ui.tb_fanin_flags.trigger_rdy)
				spi_registers.add_read_only_register(address=addrs.REG_FIFO_TRIGRDY_R, 	read=trigger_rdy)

				m.d.sync += self.ui.tb_fanout_flags.trigger.eq(spi_registers.add_register(address=addrs.REG_FIFO_TRIG_W))

				fifo_read_rdy = Signal.like(self.ui.tb_fanin_flags.fifo_read_rdy)
				m.submodules += FFSynchronizer(o=fifo_read_rdy, i=self.ui.tb_fanin_flags.fifo_read_rdy)
				spi_registers.add_read_only_register(address=addrs.REG_FIFO_READRDY_R,	read=fifo_read_rdy)

				write_counter = Signal.like(self.ui.tb_fanin_flags.write_counter)
				m.submodules += FFSynchronizer(o=write_counter, i=self.ui.tb_fanin_flags.write_counter)
				spi_registers.add_read_only_register(address=addrs.REG_FIFO_WCTR_R,	read=write_counter)

				return spi_registers
			
			def route_spi_signals(spi_registers, fifo_test):
				# inspired by the ilaSharedBusExample from LUNA
				board_spi = SPIDeviceBus()
				# ila_spi = SPIDeviceBus()
				reg_spi = SPIDeviceBus()
				fifo_spi = SPIDeviceBus()

				# between fpga_pin --- FFsynchroniser --- spi_multiplexer
				m.submodules += FFSynchronizer(o=board_spi.sdi, i=self.copi)
				m.d.comb += self.cipo.eq(board_spi.sdo) # ah! no need for synchronisation for sdo
				m.submodules += FFSynchronizer(o=board_spi.sck, i=self.sclk)
				m.submodules += FFSynchronizer(o=board_spi.cs, i=self.cs)
				# Multiplex our ILA and register SPI busses.
				m.submodules.mux = SPIMultiplexer([reg_spi, fifo_spi])
				m.d.comb += m.submodules.mux.shared_lines.connect(board_spi)
				
				# For sharing, we'll connect the _inverse_ of the primary
				# chip select to our sensor_spi[n] bus. This will allow us to send
				# camera data when CS is un-asserted, and register data when
				# CS is asserted.

				# between spi_multiplexer --- fifo_spi
				m.d.comb += fifo_test.spi.connect(fifo_spi)
				m.d.comb += fifo_spi.cs.eq(~board_spi.cs)

				# between spi_multiplexer --- spi_register_interface
				# note that it seems we need to delay the sdo by one sclk cycle...
				# that's why we dont use .connect, and instead connect signals manually,
				# and delay .sdo like this
				m.d.comb += [
					# spi_registers.spi .connect(reg_spi),
					spi_registers.spi.sck.eq(reg_spi.sck),
					spi_registers.spi.cs.eq(reg_spi.cs),
					spi_registers.spi.sdi.eq(reg_spi.sdi),

					# use straight cs here
					reg_spi.cs        .eq(board_spi.cs)
				]
				last_sdo = Signal()
				with m.If(Rose(reg_spi.sck)): # then the value we read now, we set on the next falling edge
					m.d.sync += last_sdo.eq(spi_registers.spi.sdo)
				with m.Elif(Fell(reg_spi.sck)): # set it on the falling edge
					m.d.sync += reg_spi.sdo.eq(last_sdo)

			m = Module()

			m.submodules.fifo_test = self.fifo_test

			# is this the right way around?
			m.d.comb += self.ui.connect(self.fifo_test.ui)

			handle_cs_or_csn()
			spi_registers = add_register_interface()
			route_spi_signals(spi_registers, self.fifo_test)


			return m

	
	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing
		...
	
	if args.action in ["generate", "simulate"]:
		# now run each FHDLTestCase above 
		import unittest
		sys.argv[1:] = [] # so the args used for this file don't interfere with unittest
		unittest.main()
	
	else: # upload
		
		# One test idea:
		# Fill a fifo with sequential values from a counter
		# Store the data in sdram
		# Read it back using a spi fifo-reader thing 
		# Are the read back values correct? How robust is the performance?
		
		class Upload(UploadBase):
			def elaborate(self, platform = None):
				from amaram.parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				

				config_params = Params()
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params
				config_params.clk_freq = 143e6
				config_params.burstlen = 8
				config_params.latency = 3
				config_params.numbursts = 2 
				# config_params.num_fifos = 4
				config_params.fifo_read_domain = "sync"
				config_params.fifo_write_domain = config_params.fifo_read_domain
				config_params.fifo_width = 16
				config_params.fifo_depth = config_params.burstlen * config_params.numbursts * 2#4 # 64
				config_params.read_pipeline_clk_delay = 10 # ??
				config_params.sync_mode = "sync_and_135e6_sdram_from_pll"#"sync_and_143e6_sdram_from_pll" #"sync_and_135e6_sdram_from_pll" #
				config_params.spi_clock_polarity = 1
				config_params.spi_clock_phase = 1
				
				utest_params = Params()
				utest_params.timeout_runtime = 1e-3 # arbitarily chosen, so the simulation won't run forever if it breaks
				utest_params.use_sdram_model = False
				utest_params.debug_flags = Array(Signal(name=f"debug_flag_{i}") for i in range(6))
				utest_params.timeout_period = 20e-6 # seconds
				utest_params.read_clk_freq = 16e6 #[60e6] 
				utest_params.write_clk_freq = 40e6 #[40e6]
				utest_params.num_fifo_writes = config_params.burstlen * config_params.numbursts * 2 # =160 #30 # 50 # 200
				utest_params.enable_detailed_model_printing = True

				# pass config params to super() like this. note - could this be done better?
				self.config_params = config_params 
				m = super().elaborate(platform) 

				# m.submodules.tb = tb = DomainRenamer("sdram")(Testbench(config_params, utest_params))
				m.submodules.tb = tb = Testbench(config_params, utest_params,
					copi = self.esp32.gpio4_copi,
					cipo = self.esp32.gpio12_cipo,
					sclk = self.esp32.gpio16_sclk,
					cs = self.esp32.gpio5_cs)

				# ui = Record.like(tb.ui)
				# m.d.sync += ui.connect(tb.ui, include=["fsm_state"])

				# def start_on_left_button():
				# 	start = Signal.like(self.i_buttons.left)
				# 	m.d.sync += [
				# 		start.eq(self.i_buttons.left),
				# 		ui.tb_fanout_flags.trigger.eq(Rose(start))
				# 	]

				def reset_on_right_button():
					# don't manually route the reset - do this, 
					# otherwise, if Records are used, they will oscillate, as can't be reset_less
					for domain in ["sync", "sdram", config_params.fifo_write_domain, config_params.fifo_read_domain]:
						m.d.sync += ResetSignal(domain).eq(self.i_buttons.right)


				def display_on_leds():
					counter = Signal(30)
					m.d.sdram += counter.eq(counter - 1)

					m.d.comb += self.leds.eq(Cat(
						tb.ui.tb_fanin_flags.fsm_state,
						counter[25:27] # to show that the sdram clock is clocking
					))

				# start_on_left_button()
				reset_on_right_button()
				display_on_leds()

				return m

		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")