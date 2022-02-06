# 30jan2022
# trying to use amaranth libraries from python imports
# this will be a first! hopefully it works

# it works!!! woo hoo!
#	when I press the A and B buttons, a value between 0 and 3 is read back, as expected
# 	the value that is written is represented on the LEDs

# from machine import Pin, SoftSPI, SPI
# import struct
# spi = SPI(1, polarity=0, phase=0, bits=8, firstbit=SPI.MSB, baudrate = int(1e6), sck=Pin(16), mosi=Pin(4), miso=Pin(12))
# b = bytearray(b'\xde\xad')
# spi.write_readinto(b,b), print(b)

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past

from amlib.io import SPIDeviceInterface


class Top(Elaboratable):
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
		# clk_in = platform.request(platform.default_clk, dir='-')[0]
		# note - I got an error by 'requesting the clock' twice? perhaps this is done elsewhere?

		m = Module()

		# the amlib spi interface
		word_in = Signal(8)
		word_out = Signal(8)
		# word_accepted = Signal()
		word_complete = Signal()
		m.submodules.spi_device = spi_device = SPIDeviceInterface(
				word_size=8,
				clock_polarity=0,
				clock_phase=0,
				msb_first=True,
				cs_idles_high=True#False
			)
		m.d.comb += [
			# wires
			spi_device.spi.sdi.eq(esp32.gpio4_copi),
			esp32.gpio12_cipo.eq(spi_device.spi.sdo),
			spi_device.spi.sck.eq(esp32.gpio16_sclk),
			spi_device.spi.cs.eq(esp32.gpio5_csn),

			# interface
			word_in.eq(spi_device.word_in),  
			spi_device.word_out.eq(word_out), # so buttons changes the read value
			# word_accepted.eq(spi_device.word_accepted), # an internal thing? ignore for now
			word_complete.eq(spi_device.word_complete)
		]
		 # so the recieved word is shown on the leds
		with m.If(Rose(word_complete)):
			m.d.sync += leds.eq(word_in)
		# so the state of the buttons is read
		m.d.comb += word_out.eq(Cat(i_buttons["fireA"], i_buttons["fireB"]))


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
	
if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	if args.action == "generate":
		pass # do later

	elif args.action == "simulate":
		pass # do later

	else: # upload
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
				Subsignal("gpio5_csn",  PinsN("N4", dir="i"),  Attrs(PULLMODE="UP")),
				Subsignal("gpio12_cipo", Pins("K1", dir="o"),  Attrs(PULLMODE="UP")), # SDD2? GPIO12?
				Subsignal("gpio16_sclk", Pins("L1", dir="i"),  Attrs(PULLMODE="UP")),
				Attrs(IO_TYPE="LVCMOS33", DRIVE="4")
			),
		]

		platform = ULX3S_85F_Platform()
		platform.add_resources(esp32_spi)
		platform.build(Top(), do_program=False, build_dir=f"{current_filename}_build")

