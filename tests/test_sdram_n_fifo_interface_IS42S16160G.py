


from amaranth.hdl import (Memory, ClockDomain, ResetSignal,
	ClockSignal, Elaboratable, Module, Signal, Mux, Cat,
	Const, C, Shape)
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.lib.fifo import AsyncFIFOBuffered


# build/upload
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
# for testing only?
# from amaranth.asserts import Assert, Assume, Cover, Past,
from amaranth.sim import Simulator, Delay, Tick, Passive, Active, Settle

import struct, enum
import numpy as np

import os, sys
print(os.getcwd())

# from sdram16_chip_model_IS42S16160G import dram_chip_model_IS42S16160G
# from sdram16_testdriver import sdram_controller #dram_testdriver
# sys.path.append(os.path.join(os.getcwd(), "amaram"))

import amaram
# from . import amaram
print(dir(amaram))

from amaram.sdram_n_fifo_interface_IS42S16160G import sdram_controller



import sys, os
from termcolor import cprint

class sdram_utils:
	"""
	For low-level functions or definitions that enable higher-level functions elsewhere. 
	"""
	class dram_ic_timing(enum.Enum): # minimums
		# assuming we have a -7 speed device? see p.18 of datasheet
		T_STARTUP = 2e-6 # 100e-6 # for now, make it shorter, for simulation 
		T_RP	= 15e-9
		T_RC	= 60e-9
		T_RCD	= 15e-9
		T_MRD	= 14e-9 # this is very slightly over 2 clock cycles, so we use 3 clock cycles
		T_RAS	= 37e-9 # max is 100e-6
		T_DAL	= 30e-9 # input data to active / refresh command delay time, during auto precharge
		T_XSR	= 70e-9
		T_DPL 	= 14e-9
		# T_RAS 	= 0 # for precharge ?

	class cmdi_states(enum.Enum):
		# based on p.9 of datasheet
		CMDI_DESL 		= 0 # device deselect
		CMDI_NOP 		= 1 # no operation
		CMDI_BST 		= 2 # burst stop
		CMDI_READ  		= 3 # read
		CMDI_READ_AP		= 4 # read with auto precharge
		CMDI_WRITE 		= 5 # write
		CMDI_WRITE_AP	= 6 # write with auto precharge
		CMDI_ACT			= 7 # bank activate
		CMDI_PRE 		= 8 # precharge select bank, to deactivate the open row in the chosen bank
		CMDI_PALL 		= 9 # precharge all banks, to deactivate the open row in all banks
		CMDI_REF 		= 10 # CBR auto-refresh
		CMDI_SELF 		= 11 # self-refresh
		CMDI_MRS 		= 12 # mode register set

		CMDI_ILLEGAL		= 13 # is this right?

	class mode_burst_length(enum.Enum):
		MODE_BURSTLEN_1		= 0b0
		MODE_BURSTLEN_2		= 0b1
		MODE_BURSTLEN_4		= 0b10
		MODE_BURSTLEN_8		= 0b11
		MODE_BURSTLEN_PAGE	= 0b111
	
	class mode_burst_type(enum.Enum):
		MODE_BSTTYPE_SEQ 	= 0b0
		MODE_BSTTYPE_INT	= 0b1
	
	class mode_latency(enum.Enum):
		# this is also the int representation
		MODE_CAS_2			= 0b10
		MODE_CAS_3			= 0b11

	class mode_operation(enum.Enum):
		MODE_STANDARD		= 0b00

	class mode_write_burst(enum.Enum):
		MODE_WBST_ENABLE	= 0b0
		MODE_WBST_SINGLE	= 0b1

	def __init__(self):
		super().__init__()
		pass

	def num_clk_cycles(self, delay):
		if isinstance(delay, enum.Enum):
			num_clk_cycles = int(np.ceil(self.clk_freq * delay.value))
		else:
			num_clk_cycles = int(np.ceil(self.clk_freq * delay))
			print(f"Delay is {delay}, cycles is {num_clk_cycles}")
		# ceil, so we provide enough time
		# num_clk_cycles -= 1
		# print("Num clk cycles: ", num_clk_cycles)
		return num_clk_cycles

	def assert_cmd_then_delay(self, cmd, min_duration):
		yield from self.assert_cmd_is(cmd)
		yield from self.assert_idle_cmd_for(min_duration)

	def assert_cmd_is(self, cmd):
		cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
		dram_ic_timing = dram_sim_model_IS42S16160G.dram_ic_timing

		assert cmdi_states((yield self.cmdi)) == cmd

	def assert_idle_cmd_for(self, min_duration, focus_bank = None, valid_idle_states = [cmdi_states.CMDI_NOP, cmdi_states.CMDI_DESL]):
		cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
		dram_ic_timing = dram_sim_model_IS42S16160G.dram_ic_timing

		""" 
		This will assert that for at least <min_duration> [seconds], the cmdi is in <valid_idle_states>.
		After that period, this function returns when cmdi not in <valid_idle_states>, with the new cmd.

		This will do some yields, so when this function returns/finishes (?),
		yields have been done so the next step can be done without more yields.
		"""

		# must wait this much time... but how to catch for if invalid
		# commands etc occur in this time? ...cover / bmc?
		initial_state = cmdi_states((yield self.cmdi))
		clks = 0
		while True:
			# yield Settle() # does this fix the simulations being a bit non-deterministic?
			cmd = cmdi_states((yield self.cmdi))
			bank = (yield self.dut.o_ba)
			# row = 
			# print("xx ", cmd)
			if (cmd not in valid_idle_states) and ((cmd != initial_state) | (clks > 0)) and (True if (focus_bank == None) else (bank == focus_bank)):# (cmd == end_state):
				if not (clks >= self.num_clk_cycles(min_duration)):
					print("Error: ", clks, self.num_clk_cycles(min_duration))
					assert 0
				# print("xx a")
				return cmd, clks # the actual command that caused this block to stop
			else:
				# print("xx c")
				yield
				clks += 1
				# print(clks)

class sdram_tests:
	"""
	To implement the tests for the desired interface: 
	n-fifo, one sdram. 
	"""
	def __init__(self):
		super().__init__()
		pass

	def add_rtl(self):
		# self.m.submodules.fifo = fifo = self.fifo = AsyncFIFOBuffered(width=16, depth=10, r_domain="sclk", w_domain="pclk_a")

		def add_domains():
			# # make clki domain, 
			# # domain based on the state of the .clk pin
			# clki = ClockDomain("clki", clk_edge="pos")#, local=True)
			# self.m.domains += clki
			# self.m.d.comb += clki.clk.eq(self.dut.o_clk)

			# # and for negedge - because clk_edge=neg doesn't work? according to something I read? 
			# # I lost the link, but this workaround was recommended
			# clki_n = ClockDomain("clki_n", clk_edge="pos")#, local=True)
			# # clki_n = ClockDomain("clki_n", clk_edge="neg")
			# self.m.domains += clki_n
			# self.m.d.comb += clki_n.clk.eq(~self.dut.o_clk) #
			
			for rw in ["read", "write"]:
				for i in range(len(self.dut.fifos)):
					domain = f"{rw}_{i}"
					self.m.domains += ClockDomain(domain)
		
		add_domains()

	# def write_into_fifo(self, fifo, domain, initial_delay, fill_in):
	# 	tx_vals = [0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]
	# 	def func():
	# 		yield Passive()
	# 		yield Delay(initial_delay)
	# 		yield
	# 		for i in range(fill_in):
	# 			data = tx_vals[i % len(tx_vals)]
	# 			print(f"write {i}: {data}")
	# 			yield fifo.w_data.eq(data)
	# 			yield fifo.w_en.eq(1)
	# 			yield #Tick()
	# 		yield fifo.w_en.eq(0)
	# 		yield #Tick()
	# 		yield #Tick()
	# 	return func

	# def read_from_fifo(self, fifo, domain, num_inactive_clocks = 10):
	# 	def func():
	# 		inactive_clocks = num_inactive_clocks
	# 		# yield Passive()
	# 		# while True:
	# 			# print("YYY")
	# 			# yield #Tick(domain)
	# 		yield fifo.r_en.eq(1)
	# 		i = 0
	# 		while inactive_clocks > 0:
	# 			print("xx")
	# 			if (yield fifo.r_rdy):
	# 				data = (yield fifo.r_data)
	# 				print(f"read {i}: {data}")
	# 				i += 1
	# 				inactive_clocks = num_inactive_clocks
	# 			inactive_clocks -= 1
	# 			yield
	# 	return func

	def write_into_fifo(self, fifo_ui, domain, id = 0):
	
		def func():
			write_counter = -1
			i = 0
			yield Passive()
			# yield Delay(initial_delay)
			yield
			while (yield fifo_ui.w_rdy) & (write_counter <= 0x100):
				write_counter += 1
				data = ((id << 4*3)|(write_counter & 0xFFF))
				print(f"write {hex(i)}: {hex(data)}")
				yield fifo_ui.w_data.eq(data)
				yield fifo_ui.w_en.eq(1)
				i += 1

				yield 
	
			yield fifo_ui.w_en.eq(0)
					
			yield #Tick()
			yield #Tick()
		return func

		# def func():
		# 	write_counter = -1
		# 	i = 0
		# 	yield Passive()
		# 	# yield Delay(initial_delay)
		# 	yield
		# 	while True:
		# 	if True:
		# 		if (yield fifo_ui.w_rdy):
		# 			write_counter += 1
		# 			data = ((id << 4*3)|(write_counter & 0xFFF))
		# 			print(f"write {i}: {hex(data)}")
		# 			yield fifo_ui.w_data.eq(data)
		# 			yield fifo_ui.w_en.eq(1)
		# 			i += 1
		# 		# else:
		# 			yield self.flagC.eq(~(yield self.flagC))

		# 		yield 

		# 		if i > 100:
		# 			yield fifo_ui.w_en.eq(0)
					
		# 			break

		# 	yield #Tick()
		# 	yield #Tick()
		# return func

	def read_from_fifo(self, fifo_ui, domain, id):

		# def empty_fifo(fifo):
		# 	i = 0
		# 	yield fifo.r_en.eq(1)
		# 	if (yield fifo.r_rdy):
		# 		i += 1
		# 		data = (yield fifo.r_data)
		# 		print(f"read {i}: {data}")

		# 	# for a bit of chaos, change fifos after this many reads
		# 	if i == 13:
		# 		yield fifo.r_en.eq(0)
		# 		yield
		# 		return

		# 	yield 

		def func():
			last_read = 0
			i = 0
			past_r_rdy = False
			# yield Passive()
			# yield Active()
			yield Delay(13e-6)# + read_offset*1e-7)


			yield fifo_ui.r_en.eq(1)
			yield
			while True:
				if (yield fifo_ui.r_rdy):
					data = (yield fifo_ui.r_data)
					print(f"fifo {hex(id)}, read {hex(i)}: {hex(data)}, delta={data-last_read}")
					last_read = data
					i += 1

				if (yield fifo_ui.fully_read):
					print("Fully read")
					break

				yield
					
			yield
			yield
			yield
			

			# # while True
			# for j, fifo_ui in enumerate(fifo_uis):
			# 	print(f"Reading fifo {j}: ")
			# 	yield self.flagC.eq((yield self.flagC))
			# 	empty_fifo(fifo_ui)
			# 	yield

				# i += 1
				# if i > 50:
				# 	yield fifo_ui.r_en.eq(0)
				# 	break
		return func

	def add_simulations_to(self):
		# self.sim.add_clock(1/24e6, domain="pclk_a")#, phase=30) # phase doesnt work?
		# self.sim.add_clock(1/4e6, domain="spi_clk")#, phase=21) 

		for i in range(len(self.dut.fifos)):
			r_domain = f"read_{i}"
			w_domain = f"write_{i}"
			self.sim.add_clock(1/80e6, domain=r_domain)	# represents spi reads
			# self.sim.add_clock(1/24e6, domain=w_domain)	# represents pclk writes
			self.sim.add_clock(1/40e6, domain=w_domain)

			fifo_id = [0xA, 0xB, 0xC, 0xD][i]

			# to represent an image sensor filling a fifo
			self.sim.add_sync_process(self.write_into_fifo(self.dut.fifos[i], w_domain, id=fifo_id), domain=w_domain)

			# to represent reading back the fifos with spi
			self.sim.add_sync_process(self.read_from_fifo(self.dut.fifos[i], r_domain, id=fifo_id), domain=r_domain)


		def delay_more():
			yield Active()
			yield Delay(21e-6)
		self.sim.add_process(delay_more)


class dram_sim_model_IS42S16160G(Elaboratable, sdram_tests, sdram_utils):
	""" 
	Goal: To use the async test structures of amaranth to simulate sdram interface code. 

	This class should be able to run tests that can then be run on the real chip, with
	consistent results.

	sdram ports:
	self.sdram_ic.clk.eq(self.o_clk),
	self.sdram_ic.clk_en.eq(self.o_clk_en),
	self.sdram_ic.dqm.eq(self.o_dqm),
	self.sdram_ic.dq_i.eq(self.o_dq),
	self.i_dq.eq(self.sdram_ic.dq_o),
	self.sdram_ic.a.eq(self.o_a),
	self.sdram_ic.ba.eq(self.o_ba),
	self.sdram_ic.n_cs.eq(~self.o_cs),
	self.sdram_ic.n_we.eq(~self.o_we),
	self.sdram_ic.n_ras.eq(~self.o_ras),
	self.sdram_ic.n_cas.eq(~self.o_cas)
	"""

	def elaborate(self, platform = None):
		return self.m
		

	def __init__(self, testdriver : sdram_controller, clk_freq):
		# super(sdram_tests, self).__init__()
		# super(sdram_utils, self).__init__()
		super().__init__()

		self.m = Module()
		self.dut = testdriver
		self.clk_freq = clk_freq

		self.mode = {
			"burst_length" 	: None,
			"burst_length_int" : None,
			"burst_type" 	: None,
			"latency"		: None,
			"operation"		: None,
			"writeburst" 	: None
		}


		self.reads_to_return = [
			# {"bank_src" : 3, "data" : 0},	# for example
			# {"bank_src" : None}
		]

		self.add_rtl()
		# self.add_simulations_to(sim) # call externally


	def add_rtl(self):

		def add_domains():
			# make clki domain, 
			# domain based on the state of the .clk pin
			clki = ClockDomain("clki", clk_edge="pos")#, local=True)
			self.m.domains += clki
			self.m.d.comb += clki.clk.eq(self.dut.o_clk)

			# and for negedge - because clk_edge=neg doesn't work? according to something I read? 
			# I lost the link, but this workaround was recommended
			clki_n = ClockDomain("clki_n", clk_edge="pos")#, local=True)
			# clki_n = ClockDomain("clki_n", clk_edge="neg")
			self.m.domains += clki_n
			self.m.d.comb += clki_n.clk.eq(~self.dut.o_clk) # 

		def add_flag_pins():
			self.flagA = Signal()
			self.flagB = Signal()
			self.flagC = Signal()
			self.flagD = Signal()

			self.nflagA = Signal()
			self.nflagB = Signal()

			# so the simulator doesn't remove these as unused signals,
			# before we start using them as simulation inputs
			
			# To be used with .clki
			self.m.d.clki_n += [
				self.flagA.eq(self.flagA),
				self.flagB.eq(self.flagB),
				self.flagC.eq(self.flagC),
				self.flagD.eq(self.flagD),
				
			]

			# To be used on .clki_n
			self.m.d.clki += [
				self.nflagA.eq(self.nflagA),
				self.nflagB.eq(self.nflagB),
			]

		def add_command_decoding(): # or 'add input decoding'?
			""" 
			So we can use amaranth rtl to decode these,
			then do the bulk of the simulation logic using
			amaranth's async() structure
			"""

			cmdi_states = dram_sim_model_IS42S16160G.cmdi_states

			# add input decoding logic
			self.cmdi = Signal(shape=cmdi_states, reset=cmdi_states.CMDI_NOP)
			cmd_inputs = Signal(shape=9)

			
			# self.m.d.clki_n += cmd_inputs.eq(Cat(reversed(
			self.m.d.comb += cmd_inputs.eq(Cat(reversed(
					[Past(self.dut.o_clk_en, domain="clki"), 
					self.dut.o_clk_en, 
					~self.dut.o_cs, 
					~self.dut.o_ras,
					~self.dut.o_cas, 
					~self.dut.o_we, 
					self.dut.o_ba[1], 
					self.dut.o_ba[0], 
					self.dut.o_a[10]]),
				))
			
			def set_state(new_state):
				self.m.d.comb += self.cmdi.eq(new_state)
				# self.m.d.clki += self.cmdi.eq(new_state)
				# self.m.d.clki += self.cmdi.eq(new_state)

			# I'm trying out a few ways to approach how to represent this, this is closet
			# to what is specified on p.9 of the datasheet
			# past(clk_en) | clk_en | n_cs | n_ras | n_cas | n_we | ba[1] | ba[0] | a[10] 
			with self.m.If(	 cmd_inputs.matches("1-1------")): set_state(cmdi_states.CMDI_DESL)
			with self.m.Elif(cmd_inputs.matches("1-0111---", "0--------", "--1------")): set_state(cmdi_states.CMDI_NOP)
			with self.m.Elif(cmd_inputs.matches("1-0110---")): set_state(cmdi_states.CMDI_BST)
			with self.m.Elif(cmd_inputs.matches("1-0101--0")): set_state(cmdi_states.CMDI_READ)
			with self.m.Elif(cmd_inputs.matches("1-0101--1")): set_state(cmdi_states.CMDI_READ_AP)
			with self.m.Elif(cmd_inputs.matches("1-0100--0")): set_state(cmdi_states.CMDI_WRITE)
			with self.m.Elif(cmd_inputs.matches("1-0100--1")): set_state(cmdi_states.CMDI_WRITE_AP)
			with self.m.Elif(cmd_inputs.matches("1-0011---")): set_state(cmdi_states.CMDI_ACT)
			with self.m.Elif(cmd_inputs.matches("1-0010--0")): set_state(cmdi_states.CMDI_PRE)
			with self.m.Elif(cmd_inputs.matches("1-0010--1")): set_state(cmdi_states.CMDI_PALL)
			with self.m.Elif(cmd_inputs.matches("110001---")): set_state(cmdi_states.CMDI_REF)
			with self.m.Elif(cmd_inputs.matches("100001---")): set_state(cmdi_states.CMDI_SELF)
			with self.m.Elif(cmd_inputs.matches("1-0000000")): set_state(cmdi_states.CMDI_MRS)
			with self.m.Else(): set_state(cmdi_states.CMDI_ILLEGAL)

		def add_i_dq_fix():
			# To be used with .clki_n
			# placeholder_cmdi_user = Signal(shape=self.cmdi.shape())
			self.m.d.clki += [
				self.dut.i_dq.eq(self.dut.i_dq),
				# self.dut.o_dq.eq(self.dut.o_dq)
				# placeholder_cmdi_user.eq(self.cmdi)
			]

		add_domains()
		add_flag_pins()
		add_command_decoding()
		add_i_dq_fix()

		sdram_tests.add_rtl(self)

	
	def add_simulations_to(self, sim : Simulator):
		self.sim = sim

		# def second_func():
		# 	yield Passive()
		# 	while True:
		# 		a = (yield self.dut.i_dq.eq(~(yield self.dut.i_dq)))
		# self.sim.add_process(test_func)
		# self.sim.add_sync_process(test_func, domain="sdram")
		# self.sim.add_sync_process(self.test_func_b, domain="clki")

		def initial_delay():
			yield Active()
			period = 5e-6 #20e-6
			sections = 100
			for t in range(sections):
				# yield self.nflagA.eq(~self.nflagA)
				print("Delaying, at t = ", (t/sections)*period)
				yield Delay((1/sections) * period)
			# yield Delay(5e-6)

		sdram_tests.add_simulations_to(self)

		self.sim.add_process(initial_delay)		
		self.sim.add_sync_process(self.cmd_monitor, domain="clki")
		self.sim.add_sync_process(self.ram_initialisation_monitor, domain="clki")
		self.sim.add_sync_process(self.refresh_monitor, domain="clki")
		for i in range(4):
			self.sim.add_sync_process(self.bank_monitor(bank_id=i), domain="clki")
		self.sim.add_sync_process(self.propagate_i_dq_reads, domain="clki_n")

		

	def propagate_i_dq_reads(self):
		""" 
		This will only use the dq bus if a valid write occured <latency> clocks ago
		"""
		yield Passive()
		while True:
			if len(self.reads_to_return) > 0:
				next_write = self.reads_to_return.pop(0) # {"bank_src" : x, "data" : y}

				if next_write["bank_src"] != None:
					# print("next write is ", next_write["bank_src"], hex(next_write["data"]))
					yield self.dut.i_dq.eq(next_write["data"])
					yield self.nflagA.eq(1)
				else:
					yield self.nflagA.eq(0)

			else:
				yield self.nflagA.eq(0)
			yield

	

	def refresh_monitor(self):
		"""
		todo - implement the self-refresh functionality, as on p.24 of the datasheet

		- To represent the refresh state of the chip

		- To identify if the refresh requirements are failed
			# - below, this happens when self.refresh_valid != True

		- refresh requirements:
			- '8k per 32ms' 
			- so use 8192 per 32ms (or 64?) 
				- so, in no 32ms period should there be fewer than 8192 refreshes?
		- timing
			- num_clocks = 32e-3 * f_dram = 4576000
		- what to do about it?
			- maybe every refresh cycle adds x to a counter; max value = ???
			- and every clock cycle it decrements by one, if zero, sets an error flag
			- so x = 4576000 / 8192 = 558.6 -> 559 increments per refresh
		- how to handle complexities?
			- self-refresh: 
				- increment the counter on an internal timer

		- assumptions
			- that sdram_base.sdram_freq is accurate (ie has been set right)
			- that time_for_refreshes is below the max time available in the self.shared_timer


		- note: the datasheet says that when refresh is done, one of either 'auto' or 'self', 
		an internal bank/row (?) counter is used to ensure that the chip internals refreshes
		the correct memory location in a rollover way? we don't need to worry about this
		"""
		cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
		dram_ic_timing = dram_sim_model_IS42S16160G.dram_ic_timing

		# this assumes that the counter will reduce by 1 each clock cycle,
		# representing the time passing / capacitor leakage that this
		# functionality compensates for
		period_s = 32e-3
		refreshes_per_period = 8192
		clks_per_period = period_s * self.clk_freq
		increment_per_refresh = (clks_per_period / refreshes_per_period)

		counter_max = clks_per_period # is this rignt?

		# initialise the counter with some small value
		# counter = increment_per_refresh 
		# no! initialise it to be 'full'... as there is no data to refresh yet
		counter = counter_max

		yield Passive()
		interval_between_updates = 100
		past_counter_values = []
		while True:
			try:
				# ensure the banks are precharged...?
				yield from self.assert_cmd_is(cmdi_states.CMDI_REF)
				yield from self.assert_idle_cmd_for(min_duration = dram_ic_timing.T_RC)
				
				# print("Only gets here if a refresh was done succesfully, ", counter)
				counter = (counter + increment_per_refresh) if (counter < counter_max) else counter
				
				# yield from self.assert_cmd_then_delay(cmdi_states.CMDI_REF,		min_duration = dram_ic_timing.T_RC)
			except AssertionError as e:
				# A refresh either was not attempted or failed
				pass

			counter = counter - 1 # decrement once per clock

			### monitoring
			past_counter_values.append(counter)
			if len(past_counter_values) == interval_between_updates:
				# is this maths right/useful? does the % really not matter, as long as it doesn't dip to 0?
				def as_percentage(val):
					return f"{100*val/clks_per_period}%"
				print(f"Refresh counter: {as_percentage(counter)} (), min={as_percentage(min(past_counter_values))}, max={as_percentage(max(past_counter_values))}")
				past_counter_values = []

			assert counter > 0

			yield
			# print(counter)

	def ram_initialisation_monitor(self):
		cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
		dram_ic_timing = dram_sim_model_IS42S16160G.dram_ic_timing
		
		yield Passive()
		
		yield from self.assert_cmd_then_delay(cmdi_states.CMDI_NOP,		min_duration = dram_ic_timing.T_STARTUP)
		yield from self.assert_cmd_then_delay(cmdi_states.CMDI_PALL,	min_duration = dram_ic_timing.T_RP)
		yield from self.assert_cmd_then_delay(cmdi_states.CMDI_REF,		min_duration = dram_ic_timing.T_RC)
		yield from self.assert_cmd_then_delay(cmdi_states.CMDI_REF,		min_duration = dram_ic_timing.T_RC)

		### mode register update
		# todo: assert all banks are idle
		yield from self.assert_cmd_is(cmdi_states.CMDI_MRS)
		mode_temp = yield (self.dut.o_a)
		yield from self.assert_idle_cmd_for(min_duration = dram_ic_timing.T_MRD)
		self.mode["burst_length"] = dram_sim_model_IS42S16160G.mode_burst_length(( mode_temp >> 0 ) & 0b111)
		self.mode["burst_length_int"] = None if self.mode["burst_length"] == dram_sim_model_IS42S16160G.mode_burst_length.MODE_BURSTLEN_PAGE else 2**self.mode["burst_length"].value
		self.mode["burst_type"] = dram_sim_model_IS42S16160G.mode_burst_type(( mode_temp >> 3 ) & 0b1)
		self.mode["latency"] = dram_sim_model_IS42S16160G.mode_latency(( mode_temp >> 4 ) & 0b111) 
		self.mode["operation"] = dram_sim_model_IS42S16160G.mode_operation(( mode_temp >> 7 ) & 0b11)
		self.mode["writeburst"] = dram_sim_model_IS42S16160G.mode_write_burst(( mode_temp >> 9 ) & 0b1)

		yield

	
	def cmd_monitor(self):
		cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
		last_cmd = cmdi_states.CMDI_ILLEGAL
		yield Passive()
		while True:
			cmd = cmdi_states((yield self.cmdi))
			if cmd != last_cmd:
				last_cmd = cmd
				# print("cmd was: ", cmd, type(cmd))
			yield
		
	def bank_monitor(self, bank_id):
		def func():
			cmdi_states = dram_sim_model_IS42S16160G.cmdi_states
			dram_ic_timing = dram_sim_model_IS42S16160G.dram_ic_timing

			


			# woohoo! finally here
			class bank_states(enum.Enum):
				IDLE			= 0,
				ROW_ACTIVATED	= 1,
				READ 			= 2,
				WRITE 			= 3,
				PRECHARGE 		= 4,
				ERROR 			= 5

			bank_state = bank_states.IDLE

			bank_memory = {}
			activated_row = None
			column = None

			writes_remaining = None
			reads_remaining = None
			cas_latency_elapsed = False

			auto_precharge = False

			clks_since_active = None
			clks_at_read_cmd = None
			clks_at_last_write = None
			clks_at_last_read = None

			print_bank_debug_statements = True
			
			yield Passive()
			while True:

				def bprint(*args):
					if print_bank_debug_statements:
						colors = ["red", "green", "yellow", "blue"]
						outstr = f"Bank {bank_id}, {bank_state} : "
						for arg in args:
							outstr += str(arg)
						cprint(outstr, colors[bank_id])


				def inspect_bank_memory():
					for row_id, row_data in bank_memory.items():
						data_str = f"from bank {bank_id}, row {hex(row_id)}:"
						for i, (col_id, col_data) in enumerate(row_data.items()):
							if i == 0:
								data_str += f"col {hex(col_id)}:"
								bprint(data_str)
								data_str = ""
							data_str += f"[{hex(col_data)}]"
							if (i+1)%self.mode["burst_length_int"]==0:
								bprint(data_str)
								data_str = ""
						if data_str != "":
							bprint(data_str)
				
				def inspect_reads_to_return():
					data_str = ""
					for r in self.reads_to_return:
						if r["bank_src"] == None:
							# print(r["data"])
							# bprint(None)
							# data_str += 
							data_str += f"[]"
						else:
							value = r["data"]
							data_str += f"[{hex(value)}]"
					bprint("reads to return: ", data_str)
						

					# print(f"Bank {bank_id}, {bank_state} : {[a for a in args]}")
				if bank_id == (yield self.dut.o_ba):
					cmd = cmdi_states((yield self.cmdi))
				else:
					cmd = cmdi_states.CMDI_NOP
				
				# self.dq_write_en = False # reset the default here

				# if bank_id == 1:
				if cmd not in [
						cmdi_states.CMDI_NOP, cmdi_states.CMDI_DESL,
						cmdi_states.CMDI_PRE, cmdi_states.CMDI_PALL,
						cmdi_states.CMDI_REF, cmdi_states.CMDI_SELF,
						cmdi_states.CMDI_MRS
					]:
					# in general, ignore these cmds for bank operation...? so no need to print them in general
					pass

					bprint("cmd: ", cmd, ", clks since active: ", clks_since_active)
				# --------------------------------------------------------
				if bank_state == bank_states.IDLE:
					if cmd == cmdi_states.CMDI_ACT:
						yield from self.assert_cmd_is(cmdi_states.CMDI_ACT)
						temp_row = (yield self.dut.o_a)
						bprint("temp row is: ", temp_row)
						try:
							new_cmd, waited_for_clks = yield from self.assert_idle_cmd_for(min_duration = dram_ic_timing.T_RCD, focus_bank = bank_id)
						except:
							yield self.flagC.eq(1)
							return 

						activated_row = temp_row
						clks_since_active = waited_for_clks

						if activated_row not in bank_memory:
							bank_memory[activated_row] = {} #[None] * 512 # is this right?

						bank_state = bank_states.ROW_ACTIVATED

						if new_cmd != cmd:
							bprint(f"New command recieved: {new_cmd}, (last command was {cmd}")
							# yield self.flagB.eq(1)
							continue
						yield self.flagA.eq(0)
					else:
						# print("Error! cmd is ",cmd)
						# print(".", end="")
						# yield self.flagA.eq(1)
						# assert cmd in [
						if cmd not in [
							cmdi_states.CMDI_NOP, cmdi_states.CMDI_DESL,
							cmdi_states.CMDI_PRE, cmdi_states.CMDI_PALL,
							cmdi_states.CMDI_REF, cmdi_states.CMDI_SELF,
							cmdi_states.CMDI_MRS
						]:
							bprint("Error! cmd  is ",cmd)
							yield self.flagA.eq(1)
							return

				elif bank_state == bank_states.ROW_ACTIVATED:
					yield self.flagD.eq(1)
					# print("woohoo! ", bank_id, cmd)
					if cmd in [cmdi_states.CMDI_WRITE, cmdi_states.CMDI_WRITE_AP]:
						inspect_bank_memory()

						if cmd == cmdi_states.CMDI_WRITE_AP:
							auto_precharge = True
						else:
							auto_precharge = False
						column = (yield self.dut.o_a) & 0x1FF
						bank_memory[activated_row][column] = (yield self.dut.o_dq)
						writes_remaining = self.mode["burst_length_int"] - 1
						if writes_remaining > 0: # this deals with the case of a burst length of 1
							bank_state = bank_states.WRITE
					
					elif cmd in [cmdi_states.CMDI_READ, cmdi_states.CMDI_READ_AP]:
						inspect_bank_memory()
						if cmd == cmdi_states.CMDI_READ_AP:
							auto_precharge = True
						else:
							auto_precharge = False
						column = (yield self.dut.o_a) & 0x1FF
						reads_remaining = self.mode["burst_length_int"]

						clks_until_latency_elapsed = self.mode["latency"].value - 1

						# this bank is now controlling reads in <latency> cycles,
						if len(self.reads_to_return) > clks_until_latency_elapsed:
							# so remove any reads other banks may have scheduled
							self.reads_to_return = self.reads_to_return[:clks_until_latency_elapsed]
						else:
							# or pad the duration before <latency> with blanks, if needed
							while len(self.reads_to_return) < clks_until_latency_elapsed:
								self.reads_to_return.append({"bank_src" : None})

						# now schedule in writes from this bank, do one for
						# each clock after read, because that's when dqm is sampled
						# note: these writes will appear on the dqm bus <latency> clocks later
						if ~(yield self.dut.o_dqm):
							# print(activated_row, column)
							# print("Appending to reads_to_return: ", hex(bank_memory[activated_row][column])) # so the issue is before here
							self.reads_to_return.append({"bank_src" : bank_id, "data" : bank_memory[activated_row].pop(column)}) # as reads are destructive I think?

						else:
							self.reads_to_return.append({"bank_src" : None})

						# bprint("zzzz")
						# bprint(self.reads_to_return)
						column += 1
						reads_remaining -= 1

						# todo - do reads of length 1 exist? or need to be implemented?
						# clks_at_read_cmd = clks_since_active
						bank_state = bank_states.READ
						# inspect_bank_memory()

				# --------------------------------------------------------
				elif bank_state == bank_states.READ:
					# note! due to using an additional buf latch (so the output is stable on rising edge),
					# the delay is 1 there, so we reduce the delay here
					# latency_to_use = self.mode["latency"].value - 1

					# cas_latency_elapsed = True if (clks_since_active - clks_at_read_cmd) >= latency_to_use else False

					if reads_remaining != None:
						if reads_remaining > 0:
							if ~(yield self.dut.o_dqm):
								self.reads_to_return.append({"bank_src" : bank_id, "data" : bank_memory[activated_row].pop(column)})
							else:
								self.reads_to_return.append({"bank_src" : None})

							# bprint(self.reads_to_return)
							
							column += 1
							reads_remaining -= 1

						if reads_remaining == 0:
							reads_remaining = None
						
						inspect_reads_to_return()
					
					if (reads_remaining == None):
						if not auto_precharge:
							assert 0, "not implemented yet"
							bprint("-", end="")
							# timing...? or do that in row_activated?
							bank_state = bank_states.ROW_ACTIVATED
						else:
							bank_state = bank_states.IDLE # oh my fucking god
							



				# --------------------------------------------------------
				elif bank_state == bank_states.WRITE: #[cmdi_states.CMDI_WRITE, cmdi_states.CMDI_WRITE_AP]:
					
					if cmd in [cmdi_states.CMDI_NOP, cmdi_states.CMDI_DESL]:
						yield self.flagB.eq(~(yield self.flagB))
						# then continue an existing burst write
						# todo: exit early if another read/write command happens? p.50 of datasheet
						if writes_remaining != None:
							if writes_remaining > 0:
								writes_remaining -= 1
								column += 1
								bank_memory[activated_row][column] = (yield self.dut.o_dq)
							
							if writes_remaining == 0:
								writes_remaining = None
								clks_at_last_write = clks_since_active
								# how about timing?
								inspect_bank_memory()

						bprint(f"clks since active: {clks_since_active}")

					if writes_remaining == None:
						if auto_precharge:
							if cmd in [cmdi_states.CMDI_NOP, cmdi_states.CMDI_DESL, cmdi_states.CMDI_ACT]:
								if clks_at_last_write != None:
									# we need T_dpl + T_rp between the last write and the next active cmd
									timing_passed = True
									timing_passed = clks_since_active >= self.num_clk_cycles(dram_ic_timing.T_RAS) if timing_passed else False
									
									if not auto_precharge:
										timing_passed = (clks_since_active-clks_at_last_write) >= self.num_clk_cycles(dram_ic_timing.T_DPL.value + dram_ic_timing.T_RP.value) if timing_passed else False
									else:
										timing_passed = (clks_since_active-clks_at_last_write) >= self.num_clk_cycles(dram_ic_timing.T_DAL) if timing_passed else False
									
									if timing_passed:
										bank_state = bank_states.IDLE
										clks_since_active = None
										clks_at_last_write = None
										
										bprint("passed")
										continue
									else:
										bprint("Waiting")
						elif not auto_precharge:
							# then we need a discreet state for 'precharge', T_dpl after the last write
							# or just return to active?
							assert 0, "not implemented yet"
							bank_state = bank_states.ROW_ACTIVATED

				# --------------------------------------------------------
				elif bank_state == bank_states.PRECHARGE:
					pass

				# --------------------------------------------------------
				elif bank_state == bank_states.ERROR:
					pass

				# yield from self.assert_cmd_is(cmdi_states.CMDI_ACT)
				# print(bank_memory)
				
				clks_since_active = clks_since_active + 1 if (clks_since_active != None) else None
				yield
				# print(",")

			# assert the bank state is inactive

		return func


if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	m = Module()

	# if args.action in ["generate", "simulate"]:
	# 	m.submodules.dram_testdriver = dram_testdriver = dram_testdriver()

	if args.action == "generate":
		pass

	elif args.action == "simulate":
		# PLL - 143MHz for sdram 
		sdram_freq = int(143e6)

		#m.submodules.dram_testdriver = dram_testdriver = dram_testdriver()
		m.submodules.m_sdram_controller = m_sdram_controller = sdram_controller()
		m.submodules.m_dram_model = m_dram_model = dram_sim_model_IS42S16160G(m_sdram_controller, sdram_freq)		

		sim = Simulator(m)

		sim.add_clock(1/sdram_freq, domain="sdram")
		# m_sdram_controller.setup_for_simulation(sim) # used?
		m_dram_model.add_simulations_to(sim)

		# dram_model = dram_sim_model_IS42S16160G(dram_testdriver, sim)

		

		# def initial_delay():
		# 	yield Active()
		# 	period = 5e-6 #20e-6
		# 	sections = 100
		# 	for t in range(sections):
		# 		print("Delaying, at t = ", (t/sections)*period)
		# 		yield Delay((1/sections) * period)
		# 	# yield Delay(5e-6)

		# sim.add_process(initial_delay)

		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw", 
			traces=[]): # todo - how to add clk, reset signals?

			sim.run()

	else: # upload - is there a test we could upload and do on the ulx3s?
		pass