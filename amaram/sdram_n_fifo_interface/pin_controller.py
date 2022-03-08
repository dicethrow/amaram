from amaranth.hdl import (Memory, ClockDomain, ResetSignal,
	ClockSignal, Elaboratable, Module, Signal, Mux, Cat,
	Const, C, Shape, Array, Record, Value)
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
from amaranth.lib.fifo import AsyncFIFOBuffered
from amaranth.utils import bits_for

# build/upload
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
# for testing only?
# from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.sim import Simulator, Delay, Tick, Passive, Active


from parameters_standard_sdram import sdram_cmds

class pin_controller(Elaboratable):
	ui = [
		("ios", [ # this is the interface for control signals recieved from other modules
			("cmd", sdram_cmds, 	DIR_FANOUT), 

			("clk_en", 		1,		DIR_FANOUT),
			("dqm",			1, 		DIR_FANOUT),

			("copi_dq", 	16,		DIR_FANOUT), #todo: make this width variable, ie 8/16/32
			("cipo_dq", 	16, 	DIR_FANIN),

			("a", 			13, 	DIR_FANOUT),
			("ba", 			2, 		DIR_FANOUT),
			# ("cs"),
			# ("we"),
			# ("ras"),
			# ("cas")
		])
		
	]