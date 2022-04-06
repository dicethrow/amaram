import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past, Initial
from amaranth.hdl.rec import DIR_NONE, DIR_FANOUT, DIR_FANIN, Layout, Record
from amaranth.hdl.mem import Memory
from amaranth.hdl.xfrm import DomainRenamer
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active, Settle
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer
from amaranth.lib.cdc import FFSynchronizer
from amaranth.build import Platform
from amaranth.utils import bits_for

from amtest.boards.ulx3s.common.clks import add_clock
from amtest.utils import FHDLTestCase, Params

from parameters_standard_sdram import rw_cmds, sdram_cmds


class sdram_sim_utils:
	def __init__(self, config_params, utest_params):
		self.config_params = config_params
		self.utest_params = utest_params

		# to enable the readback mechanism
		self.reads_to_return = [
			# {"bank_src" : 3, "data" : 0},	# for example
			# {"bank_src" : None}
		]


	def num_clk_cycles(self, delay):
		if isinstance(delay, enum.Enum):
			num_clk_cycles = int(np.ceil(self.config_params.clk_freq * delay.value))
		else:
			num_clk_cycles = int(np.ceil(self.config_params.clk_freq * delay))
			print(f"Delay is {delay}, cycles is {num_clk_cycles}")
		# ceil, so we provide enough time
		# num_clk_cycles -= 1
		# print("Num clk cycles: ", num_clk_cycles)
		return num_clk_cycles

	def get_cmd(self, io):
		decoded_cmd = sdram_cmds((yield io.decoded_cmd))
		# print(f"cmd is {decoded_cmd}")
		return decoded_cmd
	
	def assert_cmd_is(self, io, expected_cmd):
		# print(f"Asserting {yield from self.get_cmd(io)} == {expected_cmd}")
		assert (yield from self.get_cmd(io)) == expected_cmd

	def assert_idle_cmd_for(self, io, min_duration, focus_bank = None):
		""" 
		This will assert that for at least <min_duration> [seconds], the cmdi is in <valid_idle_states>.
		After that period, this function returns when cmdi not in <valid_idle_states>, with the new cmd.

		This will do some yields, so when this function returns/finishes (?),
		yields have been done so the next step can be done without more yields.

		thoughts
		- must wait this much time... but how to catch for if invalid commands etc occur in this time? ...cover / bmc?
		"""
		valid_idle_states = [sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL]
		initial_state = yield from self.get_cmd(io)
		clks = 0
		while True:
			# # yield Settle() # trying using clki negedge clock # does this fix the simulations being a bit non-deterministic? (commant from pre-march 2022)
			cmd = yield from self.get_cmd(io)
			if (cmd not in valid_idle_states) and ((cmd != initial_state) | (clks > 0)) and (True if (focus_bank == None) else ((yield io.ba) == focus_bank)):# (cmd == end_state):
				if not (clks >= self.num_clk_cycles(min_duration)):
					print("Error: ", clks, self.num_clk_cycles(min_duration))
					assert 0
					# print("xx a")
			return cmd, clks # the actual command that caused this block to stop
		else:
			# print("xx c")
			# yield Settle() # trying using clki negedge clock
			yield
			# yield Settle() # trying using clki negedge clock
			clks += 1
			# print(clks)


class model_sdram(sdram_sim_utils):
	def __init__(self, config_params, utest_params):

		if not hasattr(utest_params, "enable_detailed_model_printing"): utest_params.enable_detailed_model_printing = True

		super().__init__(config_params, utest_params)
	
	""" 
	todo:
	- add startup monitor (i.e. the thing that monitors what the set burstlen is)
	"""

	def toggle_debug_flag(self, i):
		yield self.utest_params.debug_flags[i].eq(~(yield self.utest_params.debug_flags[i]))

	def get_refresh_monitor_process(self, io):
		"""
		todo - implement the self-refresh functionality, as on p.24 of the datasheet
		- To represent the refresh state of the chip
		- To identify if the refresh requirements are failed
		- refresh requirements:
			- datasheet says '8k per 32ms' 
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

		- note: the datasheet says that when refresh is done, one of either 'auto' or 'self', 
		an internal bank/row (?) counter is used to ensure that the chip internals refreshes
		the correct memory location in a rollover way? we don't need to worry about this
		"""
		def func():
			# this assumes that the counter will reduce by 1 each clock cycle,
			# representing the time passing as a measure of capacitor leakage, which is the
			# whole reason the refresh mechanism exists, to compensates for it
			period_s = self.config_params.ic_refresh_timing.T_REF.value
			refreshes_per_period = self.config_params.ic_refresh_timing.NUM_REF.value
			clks_per_period = period_s * self.config_params.clk_freq
			increment_per_refresh = (clks_per_period / refreshes_per_period)

			counter_max = clks_per_period # is this rignt?

			# initialise the counter with some small value
			# counter = increment_per_refresh 
			# no! initialise it to be 'full'... as there is no data to refresh yet
			counter = counter_max

			yield Passive()
			interval_between_updates = 100
			past_counter_values = []
			memory_lapsed = False
			while True:
				try:
					# ensure the banks are precharged...?
					yield from self.assert_cmd_is(io, sdram_cmds.CMD_REF)
					yield from self.assert_idle_cmd_for(io, min_duration = self.config_params.ic_timing.T_RC)
					
					# print("Only gets here if a refresh was done succesfully, ", counter)
					counter = (counter + increment_per_refresh) if ((counter + increment_per_refresh) < counter_max) else counter
					
					# yield from self.assert_cmd_then_delay(sdram_cmds.CMD_REF,		min_duration = self.config_params.ic_timing.T_RC)
				except AssertionError as e:
					# A refresh either was not attempted or failed
					pass
				
				counter = counter - 1 if counter > 0 else counter # decrement once per clock

				### monitoring
				past_counter_values.append(counter)
				if len(past_counter_values) == interval_between_updates:
					# is this maths right/useful? does the % really not matter, as long as it doesn't dip to 0?
					# and if it dips to zero, indicate that the data has been lost, which isn't all bad, especially
					# if it hadn't had new data loaded yet. So be able to recover from this situation
					def as_percentage(val):
						return f"{100*val/clks_per_period}%"
					print(f"Refresh counter: {as_percentage(counter)} (), min={as_percentage(min(past_counter_values))}, max={as_percentage(max(past_counter_values))}")
					past_counter_values = []
					if memory_lapsed:
						print("Warning! Memory lapsed, all data in ram is now lost.")
						memory_lapsed = False

				# assert counter > 0
				if counter == 0:
					memory_lapsed = True

				# yield Settle() # trying using clki negedge clock
				yield
				# yield Settle() # trying using clki negedge clock
				# print(counter)
		return func, "clki"

	def get_readwrite_process_for_bank(self, bank_id, io):
		""" 
		This should be called once for each bank, to make a separate bank monitor process
		"""
		def func():

			num_banks = 1<<self.config_params.rw_params.BANK_BITS.value
			# sdram_cmds
			# ##################3


			# sdram_cmds = dram_sim_model_IS42S16160G.sdram_cmds
			# self.config_params.ic_timing = dram_sim_model_IS42S16160G.self.config_params.ic_timing

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

			
			yield Passive()
			
			while True:
				def add_read_to_return(bank_id, activated_row = None, column = None):
					just_had_keyerror = False

					if (bank_id == None):
						self.reads_to_return.append({"bank_src" : bank_id})
					else:
						try:
							read_value = bank_memory[activated_row].pop(column) # as reads on the sdram chip are destructive I think? test this!
						except KeyError:
							just_had_keyerror = True
							# typical issue - fix better! 
							# for now, print info instead
							print("KeyError! Ignoring")

							print(hex(activated_row), hex(column)) 
							print(activated_row, column)
							print(bank_memory)
							print(bank_memory[activated_row])
							# print("Appending to reads_to_return: ", hex(bank_memory[activated_row][column])) # so the issue is before here
							
							# add fake data instead
							read_value = 0xFACE # this suggests that the error was in the writing stage
						
						self.reads_to_return.append({"bank_src" : bank_id, "data" : read_value}) 

					return just_had_keyerror

				

				def bprint(*args):
					# if print_bank_debug_statements:
					if self.utest_params.enable_detailed_model_printing:
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
							if (i+1)%self.config_params.burstlen==0:
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
				if bank_id == (yield io.ba):		# 13mar2022 ah! but isn't .cmd always going to be one clock behind the actual ras cas etc signals? Yes - fix later, don't half-fix now..
					cmd = sdram_cmds((yield io.decoded_cmd)) 
				else:
					cmd = sdram_cmds.CMD_NOP
				
				# self.dq_write_en = False # reset the default here

				# if bank_id == 1:
				if cmd not in [
						sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL,
						sdram_cmds.CMD_PRE, sdram_cmds.CMD_PALL,
						sdram_cmds.CMD_REF, sdram_cmds.CMD_SELF,
						sdram_cmds.CMD_MRS
					]:
					# in general, ignore these cmds for bank operation...? so no need to print them in general
					pass

					bprint("cmd: ", cmd, ", clks since active: ", clks_since_active)
				# --------------------------------------------------------
				if bank_state == bank_states.IDLE:
					if cmd == sdram_cmds.CMD_ACT:
						yield from self.assert_cmd_is(io, sdram_cmds.CMD_ACT)
						temp_row = (yield io.a)
						# yield from toggle_debug_flag(0)
						new_cmd, waited_for_clks = yield from self.assert_idle_cmd_for(io, min_duration = self.config_params.ic_timing.T_RCD, focus_bank = bank_id)
						# try:
						# except:
						# 	# yield self.flagC.eq(1)
						# 	return 

						activated_row = temp_row
						bprint("Activated row: ", hex(activated_row))
						clks_since_active = waited_for_clks
						

						if activated_row not in bank_memory:
							bank_memory[activated_row] = {} #[None] * 512 # is this right?

						bank_state = bank_states.ROW_ACTIVATED

						if new_cmd != cmd:
							bprint(f"New command recieved: {new_cmd}, (last command was {cmd}")
							# yield self.flagB.eq(1)
							continue
						# yield self.flagA.eq(0)
					else:
						# print("Error! cmd is ",cmd)
						# print(".", end="")
						# yield self.flagA.eq(1)
						# assert cmd in [
						if cmd not in [
							sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL,
							sdram_cmds.CMD_PRE, sdram_cmds.CMD_PALL,
							sdram_cmds.CMD_REF, sdram_cmds.CMD_SELF,
							sdram_cmds.CMD_MRS
						]:
							bprint("Error! cmd  is ",cmd)
							# yield self.flagA.eq(1)
							return

				elif bank_state == bank_states.ROW_ACTIVATED:
					# yield self.flagD.eq(1)
					# print("woohoo! ", bank_id, cmd)
					if cmd in [sdram_cmds.CMD_WRITE, sdram_cmds.CMD_WRITE_AP]:
						inspect_bank_memory()

						if cmd == sdram_cmds.CMD_WRITE_AP:
							auto_precharge = True
						else:
							auto_precharge = False
						column = (yield io.a) & 0x1FF
						# yield from toggle_debug_flag(1)
						bank_memory[activated_row][column] = (yield io.dq_copi)
						writes_remaining = self.config_params.burstlen - 1
						if writes_remaining > 0: # this deals with the case of a burst length of 1
							bank_state = bank_states.WRITE
					
					elif cmd in [sdram_cmds.CMD_READ, sdram_cmds.CMD_READ_AP]:
						inspect_bank_memory()
						if cmd == sdram_cmds.CMD_READ_AP:
							auto_precharge = True
						else:
							auto_precharge = False
						column = (yield io.a) & 0x1FF
						# yield from toggle_debug_flag(2)
						reads_remaining = self.config_params.burstlen

						clks_until_latency_elapsed = self.config_params.latency - 1

						# this bank is now controlling reads in <latency> cycles,
						if len(self.reads_to_return) > clks_until_latency_elapsed:
							# so remove any reads other banks may have scheduled
							self.reads_to_return = self.reads_to_return[:clks_until_latency_elapsed]
						else:
							# or pad the duration before <latency> with blanks, if needed
							while len(self.reads_to_return) < clks_until_latency_elapsed:
								just_had_keyerror = add_read_to_return(bank_id=None)
								if just_had_keyerror:
									yield from self.toggle_debug_flag(0)

						# now schedule in writes from this bank, do one for
						# each clock after read, because that's when dqm is sampled
						# note: these writes will appear on the dqm bus <latency> clocks later
						if ~(yield io.dqm):
							just_had_keyerror = add_read_to_return(bank_id, activated_row, column)
							if just_had_keyerror:
									yield from self.toggle_debug_flag(1)

						else:
							just_had_keyerror = add_read_to_return(bank_id=None)
							if just_had_keyerror:
									yield from self.toggle_debug_flag(2)

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
							if ~(yield io.dqm):
								just_had_keyerror = add_read_to_return(bank_id, activated_row, column)
								if just_had_keyerror:
									yield from self.toggle_debug_flag(3)
							else:
								just_had_keyerror = add_read_to_return(bank_id=None)
								if just_had_keyerror:
									yield from self.toggle_debug_flag(4)

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
				elif bank_state == bank_states.WRITE: #[sdram_cmds.CMD_WRITE, sdram_cmds.CMD_WRITE_AP]:
					
					if cmd in [sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL]:
						# yield self.flagB.eq(~(yield self.flagB))
						# then continue an existing burst write
						# todo: exit early if another read/write command happens? p.50 of datasheet
						if writes_remaining != None:
							if writes_remaining > 0:
								writes_remaining -= 1
								column += 1
								bank_memory[activated_row][column] = (yield io.dq_copi)
							
							if writes_remaining == 0:
								writes_remaining = None
								clks_at_last_write = clks_since_active
								# how about timing?
								inspect_bank_memory()

						bprint(f"clks since active: {clks_since_active}")

					if writes_remaining == None:
						if auto_precharge:
							if cmd in [sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL, sdram_cmds.CMD_ACT]:
								if clks_at_last_write != None:
									# we need T_dpl + T_rp between the last write and the next active cmd
									timing_passed = True
									timing_passed = clks_since_active >= self.num_clk_cycles(self.config_params.ic_timing.T_RAS) if timing_passed else False
									
									if not auto_precharge:
										timing_passed = (clks_since_active-clks_at_last_write) >= self.num_clk_cycles(self.config_params.ic_timing.T_DPL.value + self.config_params.ic_timing.T_RP.value) if timing_passed else False
									else:
										timing_passed = (clks_since_active-clks_at_last_write) >= self.num_clk_cycles(self.config_params.ic_timing.T_DAL) if timing_passed else False
									
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

				# yield from self.assert_cmd_is(sdram_cmds.CMD_ACT)
				# print(bank_memory)
				
				clks_since_active = clks_since_active + 1 if (clks_since_active != None) else None
				# yield Settle() # trying using clki negedge clock # this should deal with not using a negedge sim clock
				yield
				# yield Settle() # trying using clki negedge clock # this should deal with not using a negedge sim clock
				# print(",")

			# assert the bank state is inactive

		return func, "clki"

	def propagate_i_dq_reads(self, io):
		def func():
			""" 
			Use with a negedge sim clock! otherwise instability possible
			This will only use the dq bus if a valid write occured <latency> clocks ago
			"""
			yield Passive()
			while True:
				# print(self.reads_to_return)
				if len(self.reads_to_return) > 0:
					next_write = self.reads_to_return.pop(0) # {"bank_src" : x, "data" : y}
					# print(f"next write: {next_write}")
					if next_write["bank_src"] != None:
						# print("next write is ", next_write["bank_src"], hex(next_write["data"]))
						yield io.dq_cipo.eq(next_write["data"])
						# yield self.nflagA.eq(1)
					else:
						yield io.dq_cipo.eq(0xBEAD) # this indicates that the error is with reading
						yield from self.toggle_debug_flag(5)
					# else:
						# yield self.nflagA.eq(0)

				# else:
					# yield self.nflagA.eq(0)
				# yield Settle() # trying using clki negedge clock # this should deal with not using a negedge sim clock
				yield
				# yield Settle() # trying using clki negedge clock # this should deal with not using a negedge sim clock
		return func, "clki"


	def get_sim_sync_processes(self, io):
		for bank_id in range(2**self.config_params.rw_params.BANK_BITS.value):
			yield self.get_readwrite_process_for_bank(bank_id, io)
		
		yield self.get_refresh_monitor_process(io)
		yield self.propagate_i_dq_reads(io)


def get_model_sdram_as_module_io_layout(config_params):
	io_layout = [
		("clk_en",		1,		DIR_FANOUT),
		("clk",			1,		DIR_FANOUT),
		("dqm",			1,		DIR_FANOUT),
		
		("cs",			1,		DIR_FANOUT),
		("we",			1,		DIR_FANOUT),
		("ras",			1,		DIR_FANOUT),
		("cas",			1,		DIR_FANOUT),


		("a",			config_params.rw_params.A_BITS.value,		DIR_FANOUT),
		("ba",			config_params.rw_params.BANK_BITS.value,	DIR_FANOUT),
		("dq_copi",		config_params.rw_params.DATA_BITS.value,	DIR_FANOUT),
		("dq_cipo",		config_params.rw_params.DATA_BITS.value,	DIR_FANIN),
		("dq_copi_en",	1,		DIR_FANOUT)
	]
	return io_layout


class model_sdram_as_module(Elaboratable):
	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()
		self.io = Record([
			("decoded_cmd",	sdram_cmds, 	DIR_FANOUT)
		] + get_model_sdram_as_module_io_layout(config_params))

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest


		self.model = model_sdram(self.config_params, self.utest_params)

	def get_sim_sync_processes(self):
		for process, domain in self.model.get_sim_sync_processes(io = self.io):
			yield process, domain

	def elaborate(self, platform = None):
		m = Module()

		assert platform == None, f"This is a time simulation, requiring a platform of None. Unexpected platform status of {platform}"

		# clarify the assumptions made in the implementation below
		assert self.io.ba.width == 2
		# assert self.config_params.rw_params.A_BITS.value == 13 # is this assert needed?

		# add command decoding functionality
		# decoded_cmd = Signal(shape=sdram_cmds, reset=sdram_cmds.CMD_NOP)
		encoded_cmd = Signal(shape=9)

		m.d.comb += encoded_cmd.eq(Cat(reversed(
			[Past(self.io.clk_en), 
			self.io.clk_en, 
			~self.io.cs, 	# using ~ as these are inverted by the use of PinsN in the Platform() upload stuff
			~self.io.ras,
			~self.io.cas, 
			~self.io.we, 
			self.io.ba[1], 
			self.io.ba[0], 
			self.io.a[10]],
		)))

		def set_state(new_state):
			m.d.comb += self.io.decoded_cmd.eq(new_state) # or clki?
		
		# I'm trying out a few ways to approach how to represent this, this is closet
		# to what is specified on p.9 of the datasheet. The meaning of the matches() string is:
		# past(clk_en) | clk_en | n_cs | n_ras | n_cas | n_we | ba[1] | ba[0] | a[10] 
		with m.If(	encoded_cmd.matches("1-1------")): set_state(sdram_cmds.CMD_DESL)
		with m.Elif(encoded_cmd.matches("1-0111---", "0--------", "--1------")): set_state(sdram_cmds.CMD_NOP)
		with m.Elif(encoded_cmd.matches("1-0110---")): set_state(sdram_cmds.CMD_BST)
		with m.Elif(encoded_cmd.matches("1-0101--0")): set_state(sdram_cmds.CMD_READ)
		with m.Elif(encoded_cmd.matches("1-0101--1")): set_state(sdram_cmds.CMD_READ_AP)
		with m.Elif(encoded_cmd.matches("1-0100--0")): set_state(sdram_cmds.CMD_WRITE)
		with m.Elif(encoded_cmd.matches("1-0100--1")): set_state(sdram_cmds.CMD_WRITE_AP)
		with m.Elif(encoded_cmd.matches("1-0011---")): set_state(sdram_cmds.CMD_ACT)
		with m.Elif(encoded_cmd.matches("1-0010--0")): set_state(sdram_cmds.CMD_PRE)
		with m.Elif(encoded_cmd.matches("1-0010--1")): set_state(sdram_cmds.CMD_PALL)
		with m.Elif(encoded_cmd.matches("110001---")): set_state(sdram_cmds.CMD_REF)
		with m.Elif(encoded_cmd.matches("100001---")): set_state(sdram_cmds.CMD_SELF)
		with m.Elif(encoded_cmd.matches("1-0000000")): set_state(sdram_cmds.CMD_MRS)
		with m.Else(): set_state(sdram_cmds.CMD_ILLEGAL)


		# now sort out the clock
		# we want this model to be clocked by the clock input pin
		m.domains.clki = clki = ClockDomain("clki", clk_edge="pos")# local=True)
		m.d.comb += clki.clk.eq(self.io.clk & self.io.clk_en)

		# m.domains.sync = sync = ClockDomain("sync", clk_edge="pos")# local=True)
		# m.d.comb += sync.clk.eq(self.io.clk & self.io.clk_en)


		test_counter = Signal(8)
		m.d.sync += test_counter.eq(test_counter + 1)

		# now make all references to 'sync' actually refer to "clki"
		return DomainRenamer("clki")(m)


if __name__ == "__main__":
	""" 
	feb2022 - apr2022

	Goal of this file:
	- Confirm that the real-time-logic and simulation logic interacts as expected

	Could I setup tests etc to run on github each push? It'd be good to learn how to do that.

	"""
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	def get_tb_ui_layout(config_params):
		ui_layout = [
			# ("sdram_io", controller_pin.get_ui_layout(config_params))
			("finished",		1,		DIR_FANIN)
		]
		return ui_layout

	class Testbench(Elaboratable):
		def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
			super().__init__()
			self.ui = Record(get_tb_ui_layout(config_params))

			self.config_params = config_params
			self.utest_params = utest_params
			self.utest = utest

			self.sdram_model_io = Record(get_model_sdram_as_module_io_layout(config_params))

			# put in the constructor so we can access the simulation processes
			self.sdram_model = model_sdram_as_module(self.config_params, self.utest_params)
			...
			# have a .comb passthrough for the sdram io pins
			# have a series of fsm's that turn the cmd enum into the desired value
			# also - maybe we could actually use the formal verification thing here?
			# although I feel it would be faster for now to just test by inspection
	
		def get_sim_sync_processes(self):
			for process, domain in self.sdram_model.get_sim_sync_processes():
				yield process, domain
			
			test_id = self.utest.get_test_id()
			if test_id == "modelSdramAsModule_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync":
				# now add some more sim processes? fifo stuff etc?
				print("whoop!")
		
		def elaborate(self, platform = None):
			m = Module()

			m.submodules.sdram_model = sdram_model = self.sdram_model #model_sdram_as_module(self.config_params, self.utest_params)

			m.d.comb += self.sdram_model_io.connect(sdram_model.io) # right way around?

			m.d.comb += self.sdram_model_io.clk.eq(~ClockSignal("sync")) # assuming that 'sync' will be renamed to 'sdram' or something later
			
			assert isinstance(self.utest, FHDLTestCase)
			add_clock(m, "sync")
			# add_clock(m, "sync_1e6")
			test_id = self.utest.get_test_id()

			if test_id == "modelSdramAsModule_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync":

				with m.FSM(name="testbench_fsm") as fsm:

					m.d.comb += self.sdram_model_io.clk_en.eq(1)

					with m.State("INITIAL"):
						m.next = "CHECK_CMD_DESL"
					
					with m.State("CHECK_CMD_DESL"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(0)
						]
						m.next = "CHECK_CMD_NOP"
					
					with m.State("CHECK_CMD_NOP"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(0),
							self.sdram_model_io.we.eq(0)
						]
						m.next = "CHECK_CMD_BST"

					with m.State("CHECK_CMD_BST"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(0),
							self.sdram_model_io.we.eq(1)
						]
						m.next = "CHECK_CMD_READ"
						
					with m.State("CHECK_CMD_READ"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(0),
							self.sdram_model_io.a[10].eq(0)
							# self.ba and self.a needs to be set too, at the same time as this command
						]
						m.next = "CHECK_CMD_READ_AP"
						
					with m.State("CHECK_CMD_READ_AP"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(0),
							self.sdram_model_io.a[10].eq(1)
							# self.ba and self.a needs to be set too, at the same time as this command
						]
						m.next = "CHECK_CMD_WRITE"
						
					with m.State("CHECK_CMD_WRITE"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(1),
							self.sdram_model_io.a[10].eq(0)
							# self.ba and self.a needs to be set too, at the same time as this command
						]
						m.next = "CHECK_CMD_WRITE_AP"
						
					with m.State("CHECK_CMD_WRITE_AP"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(0),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(1),
							self.sdram_model_io.a[10].eq(1)
							# self.ba and self.a needs to be set too, at the same time as this command
						]
						m.next = "CHECK_CMD_ACT"
						
					with m.State("CHECK_CMD_ACT"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(0),
							self.sdram_model_io.we.eq(0)
							# self.ba and self.a needs to be set too, at the same time as this command
						]
						m.next = "CHECK_CMD_PRE"
						
					with m.State("CHECK_CMD_PRE"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(0),
							self.sdram_model_io.we.eq(1),
							self.sdram_model_io.a[10].eq(0)
							# self.ba needs to be set too
						]
						m.next = "CHECK_CMD_PALL"
						
					with m.State("CHECK_CMD_PALL"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(0),
							self.sdram_model_io.we.eq(1),
							self.sdram_model_io.a[10].eq(1)
						]
						m.next = "CHECK_CMD_REF"
						
					with m.State("CHECK_CMD_REF"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(0)
							# clk_en needs to be 1, rather than just on the previous cycle
						]
						m.next = "CHECK_CMD_SELF"
						
					with m.State("CHECK_CMD_SELF"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(0),

							self.sdram_model_io.clk_en.eq(0)
							# clk_en needs to be 0, and 1 on the previous cycle
						]
						m.next = "CHECK_CMD_MRS_CLKSETUP"
					
					with m.State("CHECK_CMD_MRS_CLKSETUP"):
						m.d.comb += self.sdram_model_io.clk_en.eq(1)
						m.next = "CHECK_CMD_MRS"
						
					with m.State("CHECK_CMD_MRS"):
						m.d.comb += [
							self.sdram_model_io.cs.eq(1),
							self.sdram_model_io.ras.eq(1),
							self.sdram_model_io.cas.eq(1),
							self.sdram_model_io.we.eq(1),
							self.sdram_model_io.ba.eq(0b00),
							self.sdram_model_io.a[10].eq(0),
							# and self.a[:10] needs to be valid with the desired register bits

							self.sdram_model_io.clk_en.eq(0)
						]
						m.next = "DONE"
					
					with m.State("DONE"):
						m.d.comb += self.ui.finished.eq(1)



			return m

	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing

		class modelSdramAsModule_sim_thatEachCommandAndSignal_IsDecodedCorrectlyAndInSync(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params

				utest_params = Params()

				tb = Testbench(config_params, utest_params, utest=self)

				sim = Simulator(tb)
				sim.add_clock(period=1/config_params.clk_freq, domain="sync")

				# for sync_process in tb.dut.model.get_sim_sync_processes():
				for process, domain in tb.get_sim_sync_processes():
					sim.add_sync_process(sync_process, domain=domain)

				def wait_until_finished():
					yield Active()
					timeout_count = 100
					while timeout_count>0:
						timeout_count -= 1
						yield
						if (yield tb.ui.finished):
							timeout_count = -1
				sim.add_sync_process(wait_until_finished)

				# def wait_for_200us():
				# 	yield Active()
				# 	yield Delay(200e-6)
				# sim.add_process(wait_for_200us)

				with sim.write_vcd(
					f"{current_filename}_{self.get_test_id()}.vcd"):
					sim.run()

	if args.action in ["generate", "simulate"]:
		# now run each FHDLTestCase above 
		import unittest
		sys.argv[1:] = [] # so the args used for this file don't interfere with unittest
		unittest.main()

	else: # upload
		...

