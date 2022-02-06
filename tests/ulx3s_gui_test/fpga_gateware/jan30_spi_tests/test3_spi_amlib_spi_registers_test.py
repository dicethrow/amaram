# Can I work out how to use a third party spi class that provides a register-level spi interface?
# works! Used a fpga_io.py file as shown here, running the test on the last line makes the leds flash as a counter:

"""
# fpga_io.py
# tests on 31jan2022

from machine import Pin, SoftSPI, SPI
import struct

spi = SPI(1, polarity=0, phase=1, bits=8, firstbit=SPI.MSB, baudrate = int(1e6), sck=Pin(16), mosi=Pin(4), miso=Pin(12))
csn = Pin(5, Pin.OUT)
csn.on()

def reg_io(addr = 0x00, write=False, write_value=0xDE): # initial value
	# 31jan2022
	# works!
	if addr >= 0x80:
		print("addr too large!")
		return 

	if write: # hence, is a write command
		addr |= 0x80

	csn.off()
	buf = bytearray(struct.pack(">BB", addr, write_value))
	# spi.write(struct.pack(">II", addr, result))
	spi.write_readinto(buf, buf)
	result_a, result_b = struct.unpack(">BB", buf)
	csn.on()
	return hex(result_a), hex(result_b)
	
# for i in range(255): _ = reg_io(4, True, i)
"""

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past

from amlib.io import SPIRegisterInterface

import sys, os
sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
import test_common 

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
		
		m = Module()


		m.submodules.reg_if = reg_if = SPIRegisterInterface(
			address_size=7,#15, # and first bit for write or not
			register_size=8,#16, # to match the desired fifo width for later on
			default_read_value=0xCA, 
			support_size_autonegotiation=True # see the source docs of this class
		)

		addrs = test_common.register_addresses
		reg_if.add_read_only_register(address=addrs.REG_CONST_0xAF_R, read=0xAF) # const
		reg_if.add_read_only_register(address=addrs.REG_BUTTONS_R, read=Cat(i_buttons["fireA"], i_buttons["fireB"])) # buttons
		reg_if.add_register(address=addrs.REG_GENPURP_0_RW) # general purpose read-write register
		reg_if.add_register(address=addrs.REG_LEDS_RW, value_signal=leds)
		
		m.d.comb += [ 
			# wires
			reg_if.spi.sdi.eq(esp32.gpio4_copi),
			esp32.gpio12_cipo.eq(reg_if.spi.sdo),
			reg_if.spi.sck.eq(esp32.gpio16_sclk),
			reg_if.spi.cs.eq(esp32.gpio5_csn)
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

