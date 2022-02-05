
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

from .base import cmd_to_dram_ic, sdram_base

class pin_controller(sdram_base):

	class demux_enum(enum.Enum):
		DEMUX_REFRESH_CTRL		= 0
		DEMUX_READWRITE_CTRL	= 1

	def __init__(self, core):
		super().__init__()
		self.core = core

		def add_pins():
			self.o_clk = Signal() # "sdram_clk" ?
			self.o_clk_en = Signal(reset = 0)
			self.o_dqm = Signal(reset = 1)

			self.o_dq = Signal(16)
			self.i_dq = Signal(16)
			
			self.o_a = Signal(13)
			self.o_ba = Signal(2)
			self.o_cs = Signal(reset = 1)		# inverted
			self.o_we = Signal(reset = 1)		# inverted
			self.o_ras = Signal(reset = 1)	# inverted
			self.o_cas = Signal(reset = 1)	# inverted

		add_pins()

		# this .o_cmd is set to be one of the other modules .cmd,
		# and this .o_cmd controls a switch/case which sets the corresponding pins to implement it
		self.o_cmd = Signal(shape=cmd_to_dram_ic)	

		# self.aaa_test = sdram_controller.pin_controller.get_control_record()
		# self.m.d.sdram += self.aaa_test.o_ba.eq(self.aaa_test.o_ba + 1)

		self.ios_layout = [
			("o_cmd", cmd_to_dram_ic),

			("o_clk_en", 1),
			("o_dqm", 1),

			("o_dq", 16),
			("i_dq", 16),

			("o_a", 13),
			("o_ba", 2),
			# ("o_cs"),
			# ("o_we"),
			# ("o_ras"),
			# ("o_cas")
		]
		

	def elaborate(self, platform = None):
		super().elaborate(platform)	

		# m.d.comb += self.o_clk.eq(ClockSignal("sdram_clk")) # so the rising edge is in the right spot etc
		self.core.m.d.comb += self.core.o_clk.eq(~ClockSignal("sdram")) # for inverted clock? as above?

		self.connect_command_to_pins()

		return self.m

	def implement_cmd_demultiplexer(self):
		"""
		This determines which submodule's .cmd is propagated to self.o_cmd and hence to the pins
		"""

		# self.ios = Array(Record(self.ios_layout) for e in sdram_controller.pin_controller.demux_enum)
		self.ios = Array([
			self.core.refresh_controller.ios,
			self.core.read_write_controller.ios
		])

		# using an index, rather than a single ios layout, means that we can set i and o pins
		self.selected_index = Signal(shape=pin_controller.demux_enum)

		demux_enum = pin_controller.demux_enum

		with self.m.FSM(domain="sdram", name="pin_controller_fsm"):
			with self.m.State("CMD_FROM_REFRESH_CTRL"):
				self.m.d.comb += self.selected_index.eq(demux_enum.DEMUX_REFRESH_CTRL)

				with self.m.If(self.core.refresh_controller.initialised):
					with self.m.If(~(self.core.refresh_controller.refresh_in_progress | self.core.refresh_controller.trigger_refresh)):
						self.m.next = "CMD_FROM_READWRITE_CTRL"

			with self.m.State("CMD_FROM_READWRITE_CTRL"):
				self.m.d.comb += self.selected_index.eq(demux_enum.DEMUX_READWRITE_CTRL)

				with self.m.If(self.core.refresh_controller.refresh_in_progress | self.core.refresh_controller.trigger_refresh):
					self.m.next = "CMD_FROM_REFRESH_CTRL"
			

	def connect_command_to_pins(self):
		# note, this doesn't take into account Past(CKE), that should be managed elsewhere
		# all of these require that past(self.o_cs) == 0
		# this implements the truth table on p.9 of the datasheet
		# self.cmd = Signal(shape=cmd_to_dram_ic)

		self.m.d.comb += [
			self.o_a.eq(self.ios[self.selected_index].o_a),
			self.o_ba.eq(self.ios[self.selected_index].o_ba),
			self.o_clk_en.eq(self.ios[self.selected_index].o_clk_en),
			self.o_dqm.eq(self.ios[self.selected_index].o_dqm),
			self.o_dq.eq(self.ios[self.selected_index].o_dq),
			self.ios[self.selected_index].i_dq.eq(self.i_dq)
		]

		# with self.m.Switch(self.o_cmd):
		with self.m.Switch(self.ios[self.selected_index].o_cmd):
			with self.m.Case(cmd_to_dram_ic.CMDO_DESL):
				self.m.d.comb += [
					self.o_cs.eq(0)
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_NOP):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(0),
					self.o_we.eq(0)
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_BST):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(0),
					self.o_we.eq(1)
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_READ):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(1),
					self.o_we.eq(0),
					self.o_a[10].eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_READ_AP):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(1),
					self.o_we.eq(0),
					self.o_a[10].eq(1)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_WRITE):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(1),
					self.o_we.eq(1),
					self.o_a[10].eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_WRITE_AP):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(0),
					self.o_cas.eq(1),
					self.o_we.eq(1),
					self.o_a[10].eq(1)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_ACT):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(0),
					self.o_we.eq(0)
					# self.ba and self.a needs to be set too, at the same time as this command
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_PRE):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(0),
					self.o_we.eq(1),
					self.o_a[10].eq(0)
					# self.ba needs to be set too
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_PALL):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(0),
					self.o_we.eq(1),
					self.o_a[10].eq(1)
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_REF):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(1),
					self.o_we.eq(0)
					# clk_en needs to be 1, rather than just on the previous cycle
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_SELF):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(1),
					self.o_we.eq(0)
					# clk_en needs to be 0, and 1 on the previous cycle
				]
				
			with self.m.Case(cmd_to_dram_ic.CMDO_MRS):
				self.m.d.comb += [
					self.o_cs.eq(1),
					self.o_ras.eq(1),
					self.o_cas.eq(1),
					self.o_we.eq(1),
					self.o_ba.eq(0b00),
					self.o_a[10].eq(0)
					# and self.a[:10] needs to be valid with the desired register bits
				]

