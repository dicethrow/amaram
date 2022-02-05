
from amaranth.hdl import (Memory, ClockDomain, ResetSignal,
	ClockSignal, Elaboratable, Module, Signal, Mux, Cat,
	Const, C, Shape, Array, Record, Value)
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
from amaranth.utils import bits_for

# build/upload
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
# for testing only?
# from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.sim import Simulator, Delay, Tick, Passive, Active

import struct, enum
import numpy as np

class rw_cmds(enum.Enum):
	RW_IDLE			= 0
	RW_READ_16W		= 1
	RW_WRITE_16W	= 2

class cmd_to_dram_ic(enum.Enum):
	# based on p.9 of datasheet
	CMDO_DESL 		= 0 # device deselect
	CMDO_NOP 		= 1 # no operation
	CMDO_BST 		= 2 # burst stop
	CMDO_READ  		= 3 # read
	CMDO_READ_AP		= 4 # read with auto precharge
	CMDO_WRITE 		= 5 # write
	CMDO_WRITE_AP	= 6 # write with auto precharge
	CMDO_ACT			= 7 # bank activate
	CMDO_PRE 		= 8 # precharge select bank, to deactivate the open row in the chosen bank
	CMDO_PALL 		= 9 # precharge all banks, to deactivate the open row in all banks
	CMDO_REF 		= 10 # CBR auto-refresh
	CMDO_SELF 		= 11 # self-refresh
	CMDO_MRS 		= 12 # mode register set

class dram_ic_timing(enum.Enum): # minimums
	T_STARTUP = 2e-6 # 100e-6 # for now, make it shorter, for simulation 
	T_RP	= 15e-9
	T_RC	= 60e-9
	T_RCD	= 15e-9
	T_MRD	= 14e-9 # this is very slightly over 2 clock cycles, so we use 3 clock cycles
	T_RAS	= 37e-9 # max is 100e-6
	T_XSR	= 70e-9
	# T_RAS 	= 0 # for precharge ?


class sdram_base(Elaboratable):
	sdram_freq = int(143e6) #100e6 # untill overwritten
	traces_to_plot = [] # not used

	def __init__(self):
		print("in sdram_base constructor")
		self.m = Module()

	def elaborate(self, platform = None):
		return self.m


class sdram_quad_timer(sdram_base):
	def __init__(self, timer_cd = "sdram"):
		super().__init__()
		print("in sdram_quad_timer constructor")
		# self.num_timers = num_timers # for now, hardcode... 3 timers? this structure is filthy
		self.cd = timer_cd

		longest_duration = 200e-3 # 200 ms
		print("Using freq of ", sdram_base.sdram_freq, ", should be 143MHz") 
		self.shared_timer = Signal(reset=0, shape=bits_for(int(sdram_base.sdram_freq * longest_duration)))
		self.shared_timer_1 = Signal(reset=0, shape=bits_for(int(sdram_base.sdram_freq * longest_duration)))
		self.shared_timer_2 = Signal(reset=0, shape=bits_for(int(sdram_base.sdram_freq * longest_duration)))
		self.shared_timer_3 = Signal(reset=0, shape=bits_for(int(sdram_base.sdram_freq * longest_duration)))
		self.shared_timer_done = Signal()
		self.shared_timer_done_1 = Signal()
		self.shared_timer_done_2 = Signal()
		self.shared_timer_done_3 = Signal()
		self.shared_timer_inactive = Signal()
		self.shared_timer_inactive_1 = Signal()
		self.shared_timer_inactive_2 = Signal()
		self.shared_timer_inactive_3 = Signal()
		# self.increment = Signal(shape=self.shared_timer.width, reset = 0xABCD)

	
	def elaborate(self, platform = None):
		super().elaborate(platform)

		with self.m.If(self.shared_timer > 0):
			self.m.d[self.cd] += [
				self.shared_timer.eq(self.shared_timer - 1),
			]
		with self.m.If(self.shared_timer_1 > 0):
			self.m.d[self.cd] += [
				self.shared_timer_1.eq(self.shared_timer_1 - 1),
			]
		with self.m.If(self.shared_timer_2 > 0):
			self.m.d[self.cd] += [
				self.shared_timer_2.eq(self.shared_timer_2 - 1),
			]
		with self.m.If(self.shared_timer_3 > 0):
			self.m.d[self.cd] += [
				self.shared_timer_3.eq(self.shared_timer_3 - 1),
			]
		

		self.m.d.comb += [
			self.shared_timer_done.eq((self.shared_timer == 1)), # so it will pulse the clock cycle of reaching 0
			self.shared_timer_done_1.eq((self.shared_timer_1 == 1)),
			self.shared_timer_done_2.eq((self.shared_timer_2 == 1)),
			self.shared_timer_done_3.eq((self.shared_timer_3 == 1)),
		]

		self.m.d.comb += [
			self.shared_timer_inactive.eq((self.shared_timer == 0) & (self.shared_timer_done == 0)),
			self.shared_timer_inactive_1.eq((self.shared_timer_1 == 0) & (self.shared_timer_done_1 == 0)),
			self.shared_timer_inactive_2.eq((self.shared_timer_2 == 0) & (self.shared_timer_done_2 == 0)),
			self.shared_timer_inactive_3.eq((self.shared_timer_3 == 0) & (self.shared_timer_done_3 == 0)),
		]

		# print("Warning - using the sdram_base elaborate, this should be defined elsewhere instead")
		return self.m

	# def set_timer_clockdomain(self, cd = "sdram"):
	# 	self.cd = cd # e.g. "sdram" or "clki"

	def add_to_timer(self, value, timer_id = 0): # not used?
		if timer_id == 0:
			self.m.d[self.cd] += [
					self.shared_timer.eq(self.shared_timer + value - 1), # still do the slow decrement
				]
		elif timer_id == 1:
			self.m.d[self.cd] += [
					self.shared_timer_1.eq(self.shared_timer_1 + value - 1), # still do the slow decrement
				]
		elif timer_id == 2:
			self.m.d[self.cd] += [
					self.shared_timer_2.eq(self.shared_timer_2 + value - 1), # still do the slow decrement
				]
		elif timer_id == 3:
			self.m.d[self.cd] += [
					self.shared_timer_3.eq(self.shared_timer_3 + value - 1), # still do the slow decrement
				]


	def set_timer_clocks(self, clocks, timer_id = 0):
		clocks -= 1 # because one cycle is used in setting up this timer
		# remember we time not to the end of this time, but to the next command

		# print(num_clk_cycles, sdram_base.sdram_freq)
		try:
			assert clocks > 0, "unable to implement a delay of 0 in this way"
		except TypeError:
			pass # raise TypeError("Attempted to convert amaranth value to Python boolean")
		
		if timer_id == 0:
			self.m.d[self.cd] += self.shared_timer.eq(clocks)
		elif timer_id == 1:
			self.m.d[self.cd] += self.shared_timer_1.eq(clocks)
		elif timer_id == 2:
			self.m.d[self.cd] += self.shared_timer_2.eq(clocks)
		elif timer_id == 3:
			self.m.d[self.cd] += self.shared_timer_3.eq(clocks)

	def set_timer_delay(self, delay, timer_id = 0):
		if isinstance(delay, enum.Enum):
			num_clk_cycles = int(np.ceil(sdram_base.sdram_freq * delay.value))
		else:
			num_clk_cycles = int(np.ceil(sdram_base.sdram_freq * delay))
		# ceil, so we provide enough time

		self.set_timer_clocks(num_clk_cycles, timer_id)

