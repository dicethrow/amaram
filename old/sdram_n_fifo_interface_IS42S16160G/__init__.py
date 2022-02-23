# Goal: To be able to use a DRAM chip (or a simulation)
# 3oct2021


# for the chip on the ulx3s PCB that I have:
# 	IS42S16160G
# 	datasheet: https://www.issi.com/WW/pdf/42-45S83200G-16160G.pdf


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

from .pin_controller import pin_controller
from .fifo_controller import fifo_controller
from .read_write_controller import read_write_controller
from .refresh_controller import refresh_controller
from .base import sdram_base

class sdram_controller(sdram_base):

	def __init__(self):
		"""
		6nov2021 ways
		- informed by the structure of whitequark's boneless cpu  
		"""
		super().__init__()

		def add_pins():
			"""
			self <--> sdram chip interface pins
			"""
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

		def add_ic_properties():
			self.col_bits = 9
			self.bank_bits = 2
			self.row_bits = 11
		
		def add_fifo_signals(num_fifos):
			# note - I've copy-pasted this from somethwere else; this should be done better?
			
			self.depth = 50 # the pre/post-sdram fifos

			# todo - indicate start/end of data?
			fifo_layout = [
				("w_data", 16),
				("w_rdy", 1),
				("w_en", 1),
				("w_level", bits_for(self.depth + 1)),


				("r_data", 16),
				("r_rdy", 1),
				("r_en", 1),
				("r_level", bits_for(self.depth + 1)),
				("r_rst", 1), # new 15feb2022 - not used yet but fixes AttributeError()

				("fully_read", 1) # redundant?

				# ("r_next_addr_16W", 15) # can we assume this resets to zero?
			]

			self.fifos = Array(Record(fifo_layout) for _ in range(num_fifos))

		num_fifos = 4 # assume. should be up to the user

		add_pins()
		add_ic_properties()
		add_fifo_signals(num_fifos)

		# # PLL - 143MHz for sdram 
		self.clk_freq = int(143e6)

		self.pin_controller = pin_controller(self)
		self.refresh_controller = refresh_controller(self)
		self.fifo_controller = fifo_controller(self, num_fifos)
		self.read_write_controller = read_write_controller(self)
		
		# who gets control of the pins?
		# self.pin_controller.add_pins(self)
		# self.pin_controller.share_command(self.refresh_controller, self.fifo_controller)

	def elaborate(self, platform = None):
		super().elaborate(platform)

		self.m.submodules.pin_controller = self.pin_controller
		self.m.submodules.refresh_controller = self.refresh_controller
		self.m.submodules.fifo_controller = self.fifo_controller
		self.m.submodules.read_write_controller = self.read_write_controller

		# so we can be sure tha this code runs after all the __init__s have run
		self.pin_controller.implement_cmd_demultiplexer()
		self.fifo_controller.link_fifo_ui(self.m, self.fifos)

		def connect_interface_pins():
			self.m.d.comb += [
				self.o_clk.eq(self.pin_controller.o_clk),
				self.o_clk_en.eq(self.pin_controller.o_clk_en),
				self.o_dqm.eq(self.pin_controller.o_dqm),

				self.o_dq.eq(self.pin_controller.o_dq),
				self.pin_controller.i_dq.eq(self.i_dq), # self.i_dq = Signal(16)
				
				self.o_a.eq(self.pin_controller.o_a),
				self.o_ba.eq(self.pin_controller.o_ba),
				self.o_cs.eq(self.pin_controller.o_cs),		# inverted
				self.o_we.eq(self.pin_controller.o_we),		# inverted
				self.o_ras.eq(self.pin_controller.o_ras),	# inverted
				self.o_cas.eq(self.pin_controller.o_cas),	# inverted
			]

		connect_interface_pins()

		return self.m
		

	# def setup_for_simulation(self, sim : Simulator):
	# 	def initial_delay():
	# 		yield Active()
	# 		period = 10e-6 #5e-6 #20e-6
	# 		sections = 100
	# 		for t in range(sections):
	# 			print("Delaying, at t = ", (t/sections)*period)
	# 			yield Delay((1/sections) * period)
	# 		# yield Delay(5e-6)
	# 	sim.add_process(initial_delay)

