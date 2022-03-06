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


from ic_parameters import cmd_to_ic

class pin_controller(Elaboratable):
	ui = [
		("ios", [ # this is the interface for control signals recieved from other modules
			("o_cmd", cmd_to_ic, 	DIR_FANOUT), 

			("o_clk_en", 	1,	DIR_FANOUT),
			("o_dqm",		1, 	DIR_FANOUT),

			("o_dq", 		16,	DIR_FANOUT),
			("i_dq", 		16, DIR_FANIN),

			("o_a", 		13, DIR_FANOUT),
			("o_ba", 		2, 	DIR_FANOUT),
			# ("o_cs"),
			# ("o_we"),
			# ("o_ras"),
			# ("o_cas")
		])
		
	]