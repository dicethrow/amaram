import sys, os
from termcolor import cprint

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amaranth.lib.cdc import FFSynchronizer

import amaram
from amaram.sdram_n_fifo_interface_IS42S16160G import sdram_controller

sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common import fpga_gui_interface, fpga_mcu_interface
addrs = fpga_mcu_interface.register_addresses

# inspired by the ilaSharedBusExample from luna


class dram_ulx3s_upload_test_IS42S16160G(Elaboratable):
	def __init__(self, copi, cipo, sclk, i_buttons, leds,  csn=None, cs=None):
		# external spi interface
		self.copi = copi 
		self.cipo = cipo
		self.sclk = sclk

		if type(cs) != type(None):
			self.invert_csn = False
			self.cs = cs
		else:
			self.invert_csn = True
			self.cs = Signal()			
		self.csn = csn
		
		self.i_buttons = i_buttons
		self.leds = leds

		
	def elaborate(self, platform = None):
		self.m = Module()

		def handle_cs_or_csn():
			# to deal with the inverted cs pin on the ulx3s, but not in simulation
			if self.invert_csn:
				self.m.d.comb += self.cs.eq(~self.csn)

		def add_register_interface():
			# Create a set of registers...
			self.spi_registers = SPIRegisterInterface(
				address_size=fpga_mcu_interface.spi_register_interface.CMD_ADDR_BITS, # and first bit for write or not
				register_size=fpga_mcu_interface.spi_register_interface.REG_DATA_BITS, # to match the desired fifo width for later on
			)
			self.m.submodules += self.spi_registers

			# Add a simple ID register to demonstrate our registers.
			# self.spi_registers.add_read_only_register(REGISTER_ID, read=0xDEADBEEF)
			addrs = fpga_mcu_interface.register_addresses
			self.spi_registers.add_read_only_register(address=addrs.REG_BUTTONS_R, read=Cat(self.i_buttons["fireA"], self.i_buttons["fireB"])) # buttons
		
			# add fifo to test, see if the difficulties from earlier were due to not being synchronised
			self.spi_registers.m.submodules.test_fifo0 = test_fifo0 = AsyncFIFOBuffered(width=16, depth=20, r_domain="sync", w_domain="sync")
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READ_R,		value_signal=test_fifo0.r_data,	read_strobe=test_fifo0.r_en)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READRDY_R,	value_signal=test_fifo0.r_rdy)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READLVL_R,	value_signal=test_fifo0.r_level)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITE_W,		value_signal=test_fifo0.w_data,	write_strobe=test_fifo0.w_en)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITERDY_R,	value_signal=test_fifo0.w_rdy)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITELVL_R,	value_signal=test_fifo0.w_level)

		def add_ila():
			self.ila_signals = fpga_gui_interface.get_ila_signals_dict()
			self.ila = SyncSerialILA(
				**fpga_gui_interface.get_ila_constructor_kwargs(),
				clock_polarity=1, clock_phase=1 
			)
			self.m.submodules += self.ila

			# connect leds to show some feedback about when the ila is triggered
			if False: # leds to test/show register io
				self.spi_registers.add_register(address=addrs.REG_LEDS_RW, value_signal=self.leds)
			else: # leds to count complete flag raises
				with self.m.If(Rose(self.ila.complete)):
					self.m.d.sync += self.leds.eq(self.leds + 1)

			
			# Create a simple SFR that will trigger an ILA capture when written,
			# and which will display our sample status read.
			self.spi_registers.add_sfr(addrs.REG_ILA_TRIG_RW,
				read=self.ila.complete,
				write_strobe=self.ila.trigger
			)
		
		def route_spi_signals():
			self.board_spi = SPIDeviceBus()
			ila_spi = SPIDeviceBus()
			reg_spi = SPIDeviceBus()

			# between fpga_pin --- FFsynchroniser --- spi_multiplexer
			self.m.submodules += FFSynchronizer(o=self.board_spi.sdi, i=self.copi)
			self.m.d.comb += self.cipo.eq(self.board_spi.sdo) # ah! no need for synchronisation for sdo
			self.m.submodules += FFSynchronizer(o=self.board_spi.sck, i=self.sclk)
			self.m.submodules += FFSynchronizer(o=self.board_spi.cs, i= self.cs)
			# Multiplex our ILA and register SPI busses.
			self.m.submodules.mux = SPIMultiplexer([ila_spi, reg_spi])
			self.m.d.comb += self.m.submodules.mux.shared_lines.connect(self.board_spi)

			# between spi_multiplexer --- spi_ila
			self.m.d.comb += [
				self.ila.spi .connect(ila_spi),

				# For sharing, we'll connect the _inverse_ of the primary
				# chip select to our ILA bus. This will allow us to send
				# ILA data when CS is un-asserted, and register data when
				# CS is asserted.
				ila_spi.cs  .eq(~self.board_spi.cs)
			]

			# between spi_multiplexer --- spi_register_interface
			self.m.d.comb += [
				# self.spi_registers.spi .connect(reg_spi),
				self.spi_registers.spi.sck.eq(reg_spi.sck),
				self.spi_registers.spi.cs.eq(reg_spi.cs),
				self.spi_registers.spi.sdi.eq(reg_spi.sdi),

				# use straight cs here
				reg_spi.cs        .eq(self.board_spi.cs)
			]
			# note that it seems we need to delay the sdo by one sclk cycle...
			last_sdo = Signal()
			with self.m.If(Rose(reg_spi.sck)): # then the value we read now, we set on the next falling edge
				self.m.d.sync += last_sdo.eq(self.spi_registers.spi.sdo)
			with self.m.Elif(Fell(reg_spi.sck)): # set it on the falling edge
				self.m.d.sync += reg_spi.sdo.eq(last_sdo)

		def add_signals_to_ila():
			# watch spi signals?
			if True:
				# Clock divider / counter.
				with self.m.If(self.ila.complete):
					self.m.d.sync += self.ila_signals["counter"].eq(0)
				self.m.d.sync += self.ila_signals["counter"].eq(self.ila_signals["counter"] + 1)
			else:
				# test with a constant, known value
				self.m.d.sync += self.ila_signals["counter"].eq(0xF0FF0FFF)

			# Another example signal, for variety.
			if False: #not in use presently
				self.m.d.sync += self.ila_signals["toggle"].eq(~self.ila_signals["toggle"])

				
			self.m.d.sync += [
				self.ila_signals["spi_monitor0"].sdi.eq(self.board_spi.sdi),
				self.ila_signals["spi_monitor0"].sdo.eq(self.board_spi.sdo),
				self.ila_signals["spi_monitor0"].sck.eq(self.board_spi.sck),
				self.ila_signals["spi_monitor0"].cs.eq(self.board_spi.cs),
			]

		
		handle_cs_or_csn()
		add_register_interface()
		add_ila()
		route_spi_signals()
		add_signals_to_ila()

		return self.m



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

		# #m.submodules.dram_testdriver = ram_testdriver = dram_testdriver()
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

		addrs = fpga_mcu_interface.register_addresses

		def spi_tests():
			yield Active()
			# yield tb_buttons["fireB"].eq(0b1) # lets see if we can read this

			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW, True, 0xABCD)
			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW)
			

			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW)
			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW, True, 0xABCD)
			yield Delay(1/1e6)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW)

			yield Delay(1/1e6)
			yield from fpga_io_sim.alt_fifo_io(dut, read_num=32)


		sim = Simulator(m)
		sim.add_clock(1/25e6, domain="sync")

		sim.add_process(spi_tests)

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
				m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
					copi = esp32.gpio4_copi, 
					cipo = esp32.gpio12_cipo, 
					sclk = esp32.gpio16_sclk, 
					cs = esp32.gpio5_cs,

					i_buttons = i_buttons, 
					leds = leds
				)
			

				# external logic analyser, if desired
				if False:
					o_digital_discovery = platform.request("digital_discovery")
					m.d.comb += [
						o_digital_discovery.bus[0].eq(esp32.gpio5_cs), 	# cs
						o_digital_discovery.bus[1].eq(esp32.gpio16_sclk),	# clk
						o_digital_discovery.bus[2].eq(esp32.gpio4_copi),	# mosi
						o_digital_discovery.bus[3].eq(esp32.gpio12_cipo)	# miso
					]

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
				Subsignal("gpio5_cs",  PinsN("N4", dir="i"),  Attrs(PULLMODE="UP")),
				Subsignal("gpio12_cipo", Pins("K1", dir="o"),  Attrs(PULLMODE="UP")), # SDD2? GPIO12?
				Subsignal("gpio16_sclk", Pins("L1", dir="i"),  Attrs(PULLMODE="UP")),
				Attrs(IO_TYPE="LVCMOS33", DRIVE="4")
			),
		]

		# digital discovery connection, for logic probing
		digital_discovery = [
			Resource("digital_discovery", 0,
				Subsignal("bus", Pins("14- 14+ 15- 15+ 16- 16+ 17- 17+ 18- 18+", dir="o", conn=("gpio", 0)), Attrs(IO_TYPE="LVCMOS25"))
			)
		]

		platform = ULX3S_85F_Platform()
		platform.add_resources(esp32_spi)
		platform.add_resources(digital_discovery)
		platform.build(top(), do_program=False, build_dir=f"{current_filename}_build")

