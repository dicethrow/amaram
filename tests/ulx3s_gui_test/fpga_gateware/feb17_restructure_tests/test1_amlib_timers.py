import sys, os
from termcolor import cprint

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.hdl.mem import Memory
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amlib.utils import Timer
from amaranth.lib.cdc import FFSynchronizer
from amaranth.build import Platform


# sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
# from test_common import fpga_gui_interface, fpga_mcu_interface
# addrs = fpga_mcu_interface.register_addresses

class timerTest(Elaboratable):
	def __init__(self):
		super().__init__()
		self.trigger = Signal()
		self.done = Signal()

	def elaborate(self, platform: Platform) -> Module:
		m = Module()

		m.submodules.delayer = delayer = Timer(width=10, load=int(0xFF))

		with m.If(self.trigger):
			m.d.comb += delayer.start.eq(1)
		m.d.sync += self.done.eq(delayer.done)

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

		# sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/fpga_gateware"))
		# import fpga_io_sim

		tb_trigger = Signal()
		tb_done = Signal()
		tb_reset = Signal()
		m.submodules.dut = dut = timerTest()

		m.domains.sync = cd_sync = ClockDomain("sync")
		# m.d.comb += ResetSignal().eq(tb_reset)
		m.d.sync += cd_sync.rst.eq(tb_reset)

		m.d.sync += [
			dut.trigger.eq(tb_trigger),
			tb_done.eq(dut.done)
		]

		def strobe(signal):
			for _ in range(2):
				prev_value = yield signal
				yield signal.eq(~prev_value)
				yield


		def timer_test():
			yield Active()

			for repeat in range(3):

				yield Delay(1e-6) # delay at start

				yield from strobe(tb_trigger)

				while not (yield tb_done):
					yield

				# yield Delay(100e-6)

				yield Delay(1e-6) # delay at end

				# now do a reset
				
				yield from strobe(tb_reset)
				# yield from strobe(ClockSignal().rst)


		sim = Simulator(m)
		sim.add_clock(1/25e6, domain="sync")

		sim.add_sync_process(timer_test, domain="sync")

		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw"):

			sim.run()

	else: # upload - is there a test we could upload and do on the ulx3s?
		...
		# from amaranth.build import Platform, Resource, Subsignal, Pins, PinsN, Attrs
		# from amaranth_boards.ulx3s import ULX3S_85F_Platform

		# # from 

		# class top(Elaboratable):
		# 	def elaborate(self, platform):
		# 		leds = Cat([platform.request("led", i) for i in range(8)])
		# 		esp32 = platform.request("esp32_spi")
		# 		io_uart = platform.request("uart")
		# 		i_buttons = {
		# 			"pwr" : platform.request("button_pwr", 0),
		# 			"fireA" : platform.request("button_fire", 0),
		# 			"fireB" : platform.request("button_fire", 1),
		# 			"up" : platform.request("button_up", 0),
		# 			"down" : platform.request("button_down", 0),
		# 			"left" : platform.request("button_left", 0),
		# 			"right" : platform.request("button_right", 0)
		# 		}

		# 		m = Module()

		# 		# ### set up the SPI register test interface
		# 		m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
		# 			copi = esp32.gpio4_copi, 
		# 			cipo = esp32.gpio12_cipo, 
		# 			sclk = esp32.gpio16_sclk, 
		# 			cs = esp32.gpio5_cs,

		# 			i_buttons = i_buttons, 
		# 			leds = leds
		# 		)
			

		# 		# external logic analyser, if desired
		# 		if False:
		# 			o_digital_discovery = platform.request("digital_discovery")
		# 			m.d.comb += [
		# 				o_digital_discovery.bus[0].eq(esp32.gpio5_cs), 	# cs
		# 				o_digital_discovery.bus[1].eq(esp32.gpio16_sclk),	# clk
		# 				o_digital_discovery.bus[2].eq(esp32.gpio4_copi),	# mosi
		# 				o_digital_discovery.bus[3].eq(esp32.gpio12_cipo)	# miso
		# 			]

		# 		######## setup esp32 interaction ######

		# 		# route the esp32's uart
		# 		m.d.comb += [
		# 			esp32.tx.eq(io_uart.rx),
		# 			io_uart.tx.eq(esp32.rx),
		# 		]

		# 		# implement the esp32's reset/boot requirements
		# 		with m.If((io_uart.dtr.i == 1) & (io_uart.rts.i == 1)):
		# 			m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"]) 
		# 			m.d.comb += esp32.gpio0.o.eq(1)
		# 		with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 0)):
		# 			m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
		# 			m.d.comb += esp32.gpio0.o.eq(1)
		# 		with m.Elif((io_uart.dtr == 1) & (io_uart.rts == 0)):
		# 			m.d.comb += esp32.en.eq(0 & ~i_buttons["pwr"])
		# 			m.d.comb += esp32.gpio0.o.eq(1)
		# 		with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 1)):
		# 			m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
		# 			m.d.comb += esp32.gpio0.o.eq(0)

		# 		return m

		# # ESP-32 connections
		# esp32_spi = [
		# 	Resource("esp32_spi", 0,
		# 		Subsignal("en",     Pins("F1", dir="o"), Attrs(PULLMODE="UP")),
		# 		Subsignal("tx",     Pins("K3", dir="o"), Attrs(PULLMODE="UP")),
		# 		Subsignal("rx",     Pins("K4", dir="i"), Attrs(PULLMODE="UP")),
		# 		Subsignal("gpio0",  Pins("L2"),          Attrs(PULLMODE="UP")),
		# 		Subsignal("gpio4_copi", Pins("H1", dir="i"),  Attrs(PULLMODE="UP")), # SDD1? GPIO4? 
		# 		Subsignal("gpio5_cs",  PinsN("N4", dir="i"),  Attrs(PULLMODE="UP")),
		# 		Subsignal("gpio12_cipo", Pins("K1", dir="o"),  Attrs(PULLMODE="UP")), # SDD2? GPIO12?
		# 		Subsignal("gpio16_sclk", Pins("L1", dir="i"),  Attrs(PULLMODE="UP")),
		# 		Attrs(IO_TYPE="LVCMOS33", DRIVE="4")
		# 	),
		# ]

		# # digital discovery connection, for logic probing
		# digital_discovery = [
		# 	Resource("digital_discovery", 0,
		# 		Subsignal("bus", Pins("14- 14+ 15- 15+ 16- 16+ 17- 17+ 18- 18+", dir="o", conn=("gpio", 0)), Attrs(IO_TYPE="LVCMOS25"))
		# 	)
		# ]

		# platform = ULX3S_85F_Platform()
		# platform.add_resources(esp32_spi)
		# platform.add_resources(digital_discovery)
		# platform.build(top(), do_program=False, build_dir=f"{current_filename}_build")

