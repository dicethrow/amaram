import sys, os
from termcolor import cprint

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
from amaranth.lib.cdc import AsyncFFSynchronizer

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amaranth.lib.cdc import FFSynchronizer

import amaram
from amaram.sdram_n_fifo_interface_IS42S16160G import sdram_controller

sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
import test_common

# inspired by the ilaSharedBusExample from luna

class dram_ulx3s_upload_test_IS42S16160G(Elaboratable):
	def __init__(self, copi, cipo, sclk, csn, i_buttons, leds):
		# external spi interface
		self.copi = copi 
		self.cipo = cipo
		self.sclk = sclk
		self.csn = csn

		self.i_buttons = i_buttons
		self.leds = leds

		# self.ila_input = Signal(16)
		self.counter = Signal(28)
		self.toggle  = Signal()
		self.ila = SyncSerialILA(signals=[self.counter, self.toggle], sample_depth=32)


	def elaborate(self, platform = None):
		m = Module()

		m.submodules += self.ila

		board_spi = SPIDeviceBus()
		if True:
			m.d.comb += [ 
				# wires
				board_spi.sdi.eq(self.copi),
				self.cipo.eq(board_spi.sdo),
				board_spi.sck.eq(self.sclk),
				board_spi.cs.eq(self.csn)
			]
		else:
			# board_spi = SPIDeviceBus()
			m.submodules += FFSynchronizer(o=board_spi.sdi, i=self.copi)
			m.submodules += FFSynchronizer(o=self.cipo, i=board_spi.sdo)
			m.submodules += FFSynchronizer(o=board_spi.sck, i=self.sclk)
			m.submodules += FFSynchronizer(o=board_spi.cs, i=self.csn) # note! cs is inverted?

		# Create an SPI bus for our ILA.
		ila_spi = SPIDeviceBus()
		m.d.comb += [
			self.ila.spi .connect(ila_spi),

			# For sharing, we'll connect the _inverse_ of the primary
			# chip select to our ILA bus. This will allow us to send
			# ILA data when CS is un-asserted, and register data when
			# CS is asserted.
			ila_spi.cs  .eq(~board_spi.cs)
		]


		# Clock divider / counter.
		with m.If(self.ila.complete):
			m.d.sync += self.counter.eq(0)
		# with m.Else():
		m.d.sync += self.counter.eq(self.counter + 1)

		# Another example signal, for variety.
		m.d.sync += self.toggle.eq(~self.toggle)


		# Create a set of registers...
		spi_registers = SPIRegisterInterface()
		m.submodules.spi_registers = spi_registers

		# ... and an SPI bus for them.
		reg_spi = SPIDeviceBus()
		m.d.comb += [
			spi_registers.spi .connect(reg_spi),
			reg_spi.cs        .eq(board_spi.cs)
		]

		# Multiplex our ILA and register SPI busses.
		m.submodules.mux = SPIMultiplexer([ila_spi, reg_spi])
		m.d.comb += m.submodules.mux.shared_lines.connect(board_spi)

		# Add a simple ID register to demonstrate our registers.
		# spi_registers.add_read_only_register(REGISTER_ID, read=0xDEADBEEF)
		addrs = test_common.register_addresses
		spi_registers.add_read_only_register(address=addrs.REG_BUTTONS_R, read=Cat(self.i_buttons["fireA"], self.i_buttons["fireB"])) # buttons
		# spi_registers.add_register(address=addrs.REG_LEDS_RW, value_signal=self.leds)

		# Create a simple SFR that will trigger an ILA capture when written,
		# and which will display our sample status read.
		spi_registers.add_sfr(addrs.REG_ILA_TRIG_RW,
			read=self.ila.complete,
			write_strobe=self.ila.trigger
		)

		# Attach the LEDs and User I/O to the MSBs of our counter.
		# leds    = [platform.request("led", i, dir="o") for i in range(0, 6)]
		# m.d.comb += Cat(leds).eq(self.counter[-7:-1])
		m.d.comb += self.leds.eq(self.counter[-8:])

		# Return our elaborated module.
		return m



if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	m = Module()

	# if args.action in ["generate", "simulate"]:
	# 	m.submodules.dram_testdriver = dram_testdriver = dram_testdriver()

	if args.action == "generate":
		pass

	elif args.action == "simulate":

		sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/fpga_gateware"))
		import fpga_io_sim

		# # PLL - 143MHz for sdram 
		# sdram_freq = int(143e6)

		# from simulation_test import dram_sim_model_IS42S16160G

		# #m.submodules.dram_testdriver = dram_testdriver = dram_testdriver()
		# m.submodules.m_sdram_controller = m_sdram_controller = sdram_controller()
		# m.submodules.m_dram_model = m_dram_model = dram_sim_model_IS42S16160G(m_sdram_controller, sdram_freq)		


		tb_copi = Signal()
		tb_cipo = Signal()
		tb_sclk = Signal()
		tb_csn = Signal()
		tb_buttons = {
			"fireA" : Signal(),
			"fireB" : Signal()
		}
		tb_leds = Signal(8)

		placeholder_signal = Signal()
		m.d.sync += [
			placeholder_signal.eq(~placeholder_signal)
		]


		m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
			copi = tb_copi, cipo = tb_cipo, sclk = tb_sclk, csn = tb_csn,
			i_buttons = tb_buttons, leds = tb_leds
		)

		addrs = test_common.register_addresses

		def spi_tests():
			yield Active()
			# yield tb_buttons["fireB"].eq(0b1) # lets see if we can read this
			
			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW)
			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW, True, 0xABCD)

			yield Delay(1/1e6)
			yield from fpga_io_sim.alt_fifo_io(dut, read_num=10)
			
			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_BUTTONS_R)
			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_BUTTONS_R)

			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW, True, 0xABCD)
			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW)

			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW, True, 0x1234)
			# yield Delay(1/1e6)
			# yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW)

			# now let's play with fifos
			# yield from fpga_io_sim.test_fifo(dut, 0, 10)
			

		# def some_ticks():
		# 	for i in range(20):
		# 		yield Tick()

		# def some_time():
		# 	yield Delay(2e-3)

		sim = Simulator(m)
		sim.add_clock(1/25e6, domain="sync")

		sim.add_process(spi_tests)
		# sim.add_process(some_time)
		# sim.add_sync_process(some_ticks)


		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw", 
			traces=[]): # todo - how to add clk, reset signals?

			sim.run()

	else: # upload - is there a test we could upload and do on the ulx3s?
		from amaranth.build import Platform, Resource, Subsignal, Pins, PinsN, Attrs
		from amaranth_boards.ulx3s import ULX3S_85F_Platform

		# from 

		class top(Elaboratable):
			def elaborate(self, platform):
				leds = Cat([platform.request("led", i) for i in range(8)])
				esp32 = platform.request("esp32_spi")
				io_uart = platform.request("uart")
				i_buttons = {
					"pwr" : platform.request("button_pwr", 0),
					"fireA" : platform.request("button_fire", 0),
					"fireB" : platform.request("button_fire", 1),
					"up" : platform.request("button_up", 0),
					"down" : platform.request("button_down", 0),
					"left" : platform.request("button_left", 0),
					"right" : platform.request("button_right", 0)
				}

				m = Module()

				# ### set up the SPI register test interface
				# m.submodules.reg_if = reg_if = SPIRegisterInterface(
				# 	address_size=test_common.spi_register_interface.CMD_ADDR_BITS, # and first bit for write or not
				# 	register_size=test_common.spi_register_interface.REG_DATA_BITS, # to match the desired fifo width for later on
				# 	support_size_autonegotiation=True # see the source docs of this class
				# )
				m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
					copi = esp32.gpio4_copi, cipo = esp32.gpio12_cipo, sclk = esp32.gpio16_sclk, csn = esp32.gpio5_csn,
					i_buttons = i_buttons, leds = leds
				)
				# m.d.comb += [ 
				# 	# wires
				# 	reg_if.spi.sdi.eq(esp32.gpio4_copi),
				# 	esp32.gpio12_cipo.eq(reg_if.spi.sdo),
				# 	reg_if.spi.sck.eq(esp32.gpio16_sclk),
				# 	reg_if.spi.cs.eq(esp32.gpio5_csn)
				# ]

				

				######## setup esp32 interaction ######

				# route the esp32's uart
				m.d.comb += [
					esp32.tx.eq(io_uart.rx),
					io_uart.tx.eq(esp32.rx),
				]

				# implement the esp32's reset/boot requirements
				with m.If((io_uart.dtr.i == 1) & (io_uart.rts.i == 1)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"]) 
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 0)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 1) & (io_uart.rts == 0)):
					m.d.comb += esp32.en.eq(0 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 1)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(0)

				return m

		# ESP-32 connections
		esp32_spi = [
			Resource("esp32_spi", 0,
				Subsignal("en",     Pins("F1", dir="o"), Attrs(PULLMODE="UP")),
				Subsignal("tx",     Pins("K3", dir="o"), Attrs(PULLMODE="UP")),
				Subsignal("rx",     Pins("K4", dir="i"), Attrs(PULLMODE="UP")),
				Subsignal("gpio0",  Pins("L2"),          Attrs(PULLMODE="UP")),
				Subsignal("gpio4_copi", Pins("H1", dir="i"),  Attrs(PULLMODE="UP")), # SDD1? GPIO4? 
				Subsignal("gpio5_csn",  PinsN("N4", dir="i"),  Attrs(PULLMODE="UP")),
				Subsignal("gpio12_cipo", Pins("K1", dir="o"),  Attrs(PULLMODE="UP")), # SDD2? GPIO12?
				Subsignal("gpio16_sclk", Pins("L1", dir="i"),  Attrs(PULLMODE="UP")),
				Attrs(IO_TYPE="LVCMOS33", DRIVE="4")
			),
		]

		platform = ULX3S_85F_Platform()
		platform.add_resources(esp32_spi)
		platform.build(top(), do_program=False, build_dir=f"{current_filename}_build")

