
from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
from amaranth.lib.cdc import FFSynchronizer


from amlib.io import SPIRegisterInterface

import sys, os
sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common import fpga_mcu_interface 

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
			address_size=test_common.spi_register_interface.CMD_ADDR_BITS, # and first bit for write or not
			register_size=test_common.spi_register_interface.REG_DATA_BITS, # to match the desired fifo width for later on
			default_read_value=0xCA, 
			support_size_autonegotiation=True # see the source docs of this class
		)

		addrs = test_common.register_addresses
		reg_if.add_read_only_register(address=addrs.REG_CONST_0xAF_R, read=0xAF) # const
		reg_if.add_read_only_register(address=addrs.REG_BUTTONS_R, read=Cat(i_buttons["fireA"], i_buttons["fireB"])) # buttons
		reg_if.add_register(address=addrs.REG_GENPURP_0_RW) # general purpose read-write register
		reg_if.add_register(address=addrs.REG_LEDS_RW, value_signal=leds)

		
		m.submodules.test_fifo = test_fifo = AsyncFIFOBuffered(width=16, depth=5, r_domain="sync", w_domain="sync")
		reg_if.add_register(address=addrs.REG_FIFO0_READ_R,		value_signal=test_fifo.r_data,	read_strobe=test_fifo.r_en)
		reg_if.add_register(address=addrs.REG_FIFO0_READRDY_R,	value_signal=test_fifo.r_rdy)
		reg_if.add_register(address=addrs.REG_FIFO0_READLVL_R,	value_signal=test_fifo.r_level)
		reg_if.add_register(address=addrs.REG_FIFO0_WRITE_W,		value_signal=test_fifo.w_data,	write_strobe=test_fifo.w_en)
		reg_if.add_register(address=addrs.REG_FIFO0_WRITERDY_R,	value_signal=test_fifo.w_rdy)
		reg_if.add_register(address=addrs.REG_FIFO0_WRITELVL_R,	value_signal=test_fifo.w_level)

		
		# m.d.comb += [ 
		# 	# wires
		# 	reg_if.spi.sdi.eq(esp32.gpio4_copi),
		# 	esp32.gpio12_cipo.eq(reg_if.spi.sdo),
		# 	reg_if.spi.sck.eq(esp32.gpio16_sclk),
		# 	reg_if.spi.cs.eq(esp32.gpio5_csn)
		# ]
		m.submodules += FFSynchronizer(o=reg_if.spi.sdi, i=esp32.gpio4_copi)
		m.submodules += FFSynchronizer(o=esp32.gpio12_cipo, i=reg_if.spi.sdo)
		m.submodules += FFSynchronizer(o=reg_if.spi.sck, i=esp32.gpio16_sclk)
		m.submodules += FFSynchronizer(o=reg_if.spi.cs, i= esp32.gpio5_csn)

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

		