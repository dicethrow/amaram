import sys, os
from termcolor import cprint

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
from amaranth.hdl.mem import Memory
from amaranth.hdl.xfrm import DomainRenamer
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
	ui_layout = [
		("trigger",	1,	DIR_FANOUT),
		("done",	1,	DIR_FANIN)
	]

	debug_layout = [
		("count",	32,	DIR_FANIN)
	]

	def __init__(self, load):
		super().__init__()
		self.ui = Record(timerTest.ui_layout)
		self.debug = Record(timerTest.debug_layout)
		self.load = load

	def elaborate(self, platform: Platform) -> Module:
		m = Module()

		ui = Record.like(self.ui)
		debug = Record.like(self.debug)
		m.d.sync += [
			self.ui.connect(ui),
			self.debug.connect(debug)
		]

		m.submodules.delayer = delayer = Timer(load=int(self.load))

		m.d.sync += [
			delayer.start.eq(ui.trigger), # comb?
			ui.done.eq(delayer.done),
			debug.count.eq(delayer.counter_out)
		]

		return m

if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	class Testbench(Elaboratable):
		timerTest_test_interface_layout = [
			("trigger",			1, DIR_FANOUT),
			("done",			1, DIR_FANIN),
			("reset",			1, DIR_FANOUT), # note - this doesn't seem to show in traces, but still works?
			# ("leds", 			8, DIR_FANOUT) # can't do reset-less here, so using a separate signal
		]

		
		def __init__(self):
			super().__init__()
			self.ui = Record(Testbench.timerTest_test_interface_layout)
			self.leds = Signal(8, reset_less=True)

		def elaborate(self, platform = None):
			m = Module()

			m.submodules.dut = dut = DomainRenamer("sync_1e6")(timerTest(load = 1e2 if platform == None else 1e6))

			ui = Record.like(self.ui)
			debug = Record.like(dut.debug)
			m.d.sync_1e6 += [
				self.ui.connect(ui),
				ui.connect(dut.ui, exclude=["reset"]),
				debug.connect(dut.debug)
			]

			# change a led flag each time one of these rises, so we can see quick changes
			# for i, each in enumerate([ui.trigger, ui.done, ui.reset]):
			# 	with m.If(Rose(each)):
			# 		m.d.sync += self.leds[i].eq(~self.leds[i])
			m.d.sync_1e6 += self.leds.eq(debug.count[10:])

			def init_clocks():
				### add default clock
				m.domains.sync = cd_sync = ClockDomain("sync")
				m.d.sync += cd_sync.rst.eq(ui.reset) # or should this be comb?
				if platform != None:
					m.d.comb += cd_sync.clk.eq(platform.request("clk25"))
					platform.add_clock_constraint(cd_sync.clk,  platform.default_clk_frequency)
				else:
					... # note - the sim clock is added later

				### add slower clock for counters (i.e. so they don't limit speed)
				m.domains.sync_1e6 = cd_sync_1e6 = ClockDomain("sync_1e6")
				divisor = 25
				clk_counter = Signal(shape=range(int(divisor/2)+1)) # is this right?
				m.d.sync += [
					clk_counter.eq(Mux(clk_counter == (int(divisor/2)-1), 0, clk_counter+1)), # not quite accurate but close enough
					cd_sync_1e6.rst.eq(ui.reset), # or should this be comb?
					cd_sync_1e6.clk.eq(Mux(clk_counter==0,~cd_sync_1e6.clk,cd_sync_1e6.clk))
				]
			init_clocks()

			return m

	if args.action == "generate":
		pass

	elif args.action == "simulate":

		class Simulate(Elaboratable):
			def __init__(self):
				super().__init__()
				self.ui = Record(Testbench.timerTest_test_interface_layout)

			def timer_test(self):
				def strobe(signal):
					for _ in range(2):
						prev_value = yield signal
						yield signal.eq(~prev_value)
						yield
				yield Active()

				for repeat in range(3):

					yield Delay(1e-6) # delay at start

					yield from strobe(self.ui.trigger)

					while not (yield self.ui.done):
						yield

					# yield Delay(100e-6)

					yield Delay(1e-6) # delay at end

					# now do a reset
					
					yield from strobe(self.ui.reset)
					# yield from strobe(ClockSignal().rst)


			def elaborate(self, platform = None):
				m = Module()

				m.submodules.tb = tb = Testbench()
				ui = Record.like(self.ui)
				m.d.sync_1e6 += [
					self.ui.connect(ui),
					ui.connect(tb.ui, exclude=["reset"])
				]

				# m.domains.sync = cd_sync = ClockDomain("sync")
				# m.d.sync += cd_sync.rst.eq(ui.reset)
				
				return m

		# dut = Simulate_test()

		

		# m = Module()
		# m.submodules.dut = dut = Testbench()
		# dut_test_io = Record(Testbench.timerTest_test_interface_layout)
		# m.d.sync += dut_test_io.connect(dut.dut_test_io)
		# # dut.dut_test_io.connect(dut_test_io)

		top = Simulate()
		sim = Simulator(top)
		sim.add_clock(1/25e6, domain="sync")
		sim.add_sync_process(top.timer_test, domain="sync_1e6")

		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw",
			# traces=[
			# 	dut_test_io.trigger,
			# 	dut_test_io.done,
			# 	dut_test_io.reset,
			# 	cd_sync.rst,
			# 	dut_test_io.leds,
			# ] + dut.ports()	
			):
			sim.run()

	else: # upload - is there a test we could upload and do on the ulx3s?
		...
		from amaranth.build import Platform, Resource, Subsignal, Pins, PinsN, Attrs
		from amaranth_boards.ulx3s import ULX3S_85F_Platform

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


		class UploadBase(Elaboratable):
			def elaborate(self, platform = None):
				self.leds = Cat([platform.request("led", i) for i in range(8)])
				esp32 = platform.request("esp32_spi")
				io_uart = platform.request("uart")
				# clk25 = platform.request("clk25")

				i_unsync_buttons = Record([
					("pwr",			1, DIR_FANOUT),
					("fireA",		1, DIR_FANOUT),
					("fireB",		1, DIR_FANOUT),
					("up",			1, DIR_FANOUT),
					("down",		1, DIR_FANOUT),
					("left",		1, DIR_FANOUT),
					("right",		1, DIR_FANOUT),
				])

				m = Module()

				m.d.sync += [
					i_unsync_buttons.pwr.eq(platform.request("button_pwr", 0)),
					i_unsync_buttons.fireA.eq(platform.request("button_fire", 0)),
					i_unsync_buttons.fireB.eq(platform.request("button_fire", 1)),
					i_unsync_buttons.up.eq(platform.request("button_up", 0)),
					i_unsync_buttons.down.eq(platform.request("button_down", 0)),
					i_unsync_buttons.left.eq(platform.request("button_left", 0)),
					i_unsync_buttons.right.eq(platform.request("button_right", 0)),
				]
				self.i_buttons = Record.like(i_unsync_buttons)
				m.submodules.i_button_ffsync = FFSynchronizer(i_unsync_buttons, self.i_buttons) # useful?

				
				# cd_sync = ClockDomain("sync")
				# m.d.comb += cd_sync.clk.eq(clk25)
				# m.domains += cd_sync
				# platform.add_clock_constraint(cd_sync.clk,  platform.default_clk_frequency)

				# external logic analyser, if desired
				if False:
					o_digital_discovery = platform.request("digital_discovery")
					m.d.sync += [
						o_digital_discovery.bus[0].eq(esp32.gpio5_cs), 	# cs
						o_digital_discovery.bus[1].eq(esp32.gpio16_sclk),	# clk
						o_digital_discovery.bus[2].eq(esp32.gpio4_copi),	# mosi
						o_digital_discovery.bus[3].eq(esp32.gpio12_cipo)	# miso
					]

				######## setup esp32 interaction ######

				# route the esp32's uart
				m.d.sync += [
					esp32.tx.eq(io_uart.rx),
					io_uart.tx.eq(esp32.rx),
				]

				# implement the esp32's reset/boot requirements
				with m.If((io_uart.dtr.i == 1) & (io_uart.rts.i == 1)):
					m.d.sync += esp32.en.eq(1 & ~self.i_buttons.pwr) 
					m.d.sync += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 0)):
					m.d.sync += esp32.en.eq(1 & ~self.i_buttons.pwr)
					m.d.sync += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 1) & (io_uart.rts == 0)):
					m.d.sync += esp32.en.eq(0 & ~self.i_buttons.pwr)
					m.d.sync += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 1)):
					m.d.sync += esp32.en.eq(1 & ~self.i_buttons.pwr)
					m.d.sync += esp32.gpio0.o.eq(0)

				return m

		class Upload(UploadBase):
			def __init__(self):
				super().__init__()
				
			def elaborate(self, platform = None):
				m = super().elaborate(platform)

				m.submodules.tb = tb = Testbench()	

				ui = Record.like(tb.ui)
				m.d.sync_1e6 += ui.connect(tb.ui)

				trigger = Signal.like(ui.trigger)
				reset = Signal.like(ui.reset)
				m.d.sync_1e6 += [
					trigger.eq(self.i_buttons.left),
					ui.trigger.eq(Rose(trigger, domain="sync_1e6")),

					reset.eq(self.i_buttons.right),
					ui.reset.eq(Rose(reset, domain="sync_1e6")),

					self.leds.eq(tb.leds),
				]

				return m


		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")

