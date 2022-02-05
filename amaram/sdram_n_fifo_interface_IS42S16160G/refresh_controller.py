
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

from .base import dram_ic_timing, cmd_to_dram_ic, sdram_base, sdram_quad_timer


class refresh_controller(sdram_quad_timer):
	""" 
	ah! make this controller do
	- initial power up, mode register set
	- refreshes, incl. self- and auto- (which may involve power saving?)

	todo 15nov2021 - make this handle delayed refreshes, 
	so the refresh requirements are never exceeded
	"""
	def __init__(self, core):
		super().__init__()
		self.core = core
		self.initialised = Signal(reset=0)

		self.request_to_refresh_soon = Signal()
		self.trigger_refresh = Signal()
		self.refresh_in_progress = Signal()

		self.refreshes_to_do = Signal(shape=bits_for(5-1), reset=5) #Const(5)
		self.refreshes_done_so_far = Signal(shape=self.refreshes_to_do.shape())

		period_s = 32e-3 # todo - make these defined elsewhere, not magic numbers
		refreshes_per_period = 8192
		self.clks_per_period = int((period_s * self.core.clk_freq) + 0.5)
		self.increment_per_refresh = int(self.clks_per_period / refreshes_per_period)
		self.refresh_level = Signal(shape=bits_for(self.clks_per_period), reset=self.clks_per_period) # is it valid to assume it starts 'full'..??

		# io signals that may or may not be propaated to the real pins,
		# at the control of the pin controller
		# self.cmd = Signal(shape=cmd_to_dram_ic)
		# self.o_clk_en = Signal(reset=0)
		# self.o_a = Signal(13)
		# self.o_ba = Signal(2)
		self.ios = Record(core.pin_controller.ios_layout)

	def elaborate(self, platform = None):

		self.m.d.sdram += [
			self.refresh_level.eq(Mux(self.refresh_level > 0, self.refresh_level - 1, 0))
		]

		# default to this always being true....?
		self.m.d.comb += self.ios.o_clk_en.eq(1)

		super().elaborate(platform)
		with self.m.FSM(domain="sdram", name="refresh_controller_fsm"):
			with self.m.State("AFTER_RESET"):
				with self.m.If(self.initialise_and_load_mode_register(trigger = self.initialised == 0)):
					self.m.next = "INITIAL_SETUP"

			with self.m.State("INITIAL_SETUP"):
				with self.m.If(self.shared_timer_inactive_1):
					# self.set_timer_delay(1e-4, timer_id=1) # 100 us?
					self.set_timer_delay(4e-6, timer_id=1) # 100 us?
				with self.m.Else():
					pass
				with self.m.If(self.shared_timer_done_1):
					self.m.next = "REQUEST_REFRESH_SOON"
			
			with self.m.State("READY_FOR_NORMAL_OPERATION"):
				with self.m.If(self.shared_timer_inactive_1):
					# self.set_timer_delay(1e-4, timer_id=1) # 100 us?
					# self.set_timer_delay(4e-6, timer_id=1) # 100 us?
					self.set_timer_clocks(self.increment_per_refresh - (self.clks_per_period-self.refresh_level), timer_id=1)
				with self.m.Else():
					pass
				with self.m.If(self.shared_timer_done_1):
					self.m.d.sdram += self.refreshes_to_do.eq(1)
					self.m.next = "REQUEST_REFRESH_SOON"

			with self.m.State("REQUEST_REFRESH_SOON"):
				self.m.d.sdram += self.request_to_refresh_soon.eq(1)
				with self.m.If(self.trigger_refresh):
					self.m.d.sdram += self.refresh_in_progress.eq(1)
					self.m.next = "DO_ANOTHER_REFRESH?"

			with self.m.State("DO_ANOTHER_REFRESH?"):
				self.m.d.sdram += self.request_to_refresh_soon.eq(0)
				with self.m.If(self.refreshes_to_do > 0):
					self.m.d.sdram += self.refreshes_to_do.eq(self.refreshes_to_do - 1) # so we only do one refresh normally
					self.m.next = "AUTO_REFRESH"

				with self.m.Else():
					# finish up here
					self.m.d.sdram += [
						self.refresh_in_progress.eq(0),
					]
					self.m.next = "READY_FOR_NORMAL_OPERATION"
			
			with self.m.State("AUTO_REFRESH"):
				with self.m.If(self.shared_timer_inactive):
					self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
					self.set_timer_delay(dram_ic_timing.T_RC)

					self.m.d.sdram += [
						self.refresh_level.eq(Mux(self.refresh_level < (self.clks_per_period - self.increment_per_refresh), self.refresh_level + self.increment_per_refresh, self.clks_per_period))
					]
					
				with self.m.Else():
					self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
				with self.m.If(self.shared_timer_done):
					self.m.next = "DO_ANOTHER_REFRESH?"
		
		return self.m
	
	def initialise_and_load_mode_register(self, trigger):
		# replicating p. 22 of datasheet
		complete = Signal()
		with self.m.If(trigger):
			with self.m.FSM(domain="sdram", name="initialise_and_load_mode_register_fsm"):

				with self.m.State("POWERUP"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += [
							self.ios.o_clk_en.eq(1),
							self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
						]
						self.set_timer_delay(dram_ic_timing.T_STARTUP)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "PRECH_BANKS"

				with self.m.State("PRECH_BANKS"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_PALL)
						self.set_timer_delay(dram_ic_timing.T_RP)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "AUTO_REFRESH_1"

				with self.m.State("AUTO_REFRESH_1"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
						self.set_timer_delay(dram_ic_timing.T_RC)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "AUTO_REFRESH_2"

				with self.m.State("AUTO_REFRESH_2"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_REF)
						self.set_timer_delay(dram_ic_timing.T_RC)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.next = "LOAD_MODE_REG"

				with self.m.State("LOAD_MODE_REG"):
					with self.m.If(self.shared_timer_inactive):
						self.m.d.comb += [
							self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_MRS),
							self.ios.o_a[:10].eq(0b0000110011) # burst=8, sequential; latency=3
							# self.ios.o_a[:10].eq(0b0000110010) # burst=4, sequential; latency=3
							# self.ios.o_a[:10].eq(0b0000110001) # burst=2, sequential; latency=3
						]
						self.set_timer_delay(dram_ic_timing.T_MRD)
					with self.m.Else():
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
					with self.m.If(self.shared_timer_done):
						self.m.d.comb += self.ios.o_cmd.eq(cmd_to_dram_ic.CMDO_NOP)
						self.m.next = "DONE"

				with self.m.State("DONE"):
					self.m.d.comb += complete.eq(1)
					self.m.d.sdram += self.initialised.eq(1)
		
		return complete

