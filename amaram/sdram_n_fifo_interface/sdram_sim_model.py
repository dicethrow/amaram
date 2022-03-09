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
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer
from amaranth.lib.cdc import FFSynchronizer
from amaranth.build import Platform
from amaranth.utils import bits_for

from parameters_standard_sdram import sdram_cmds

""" 
8mar2022
todo
- pass 'dram_ic_timing' from an external file, so we can work with faster/slower chips
- does using the negedge of the clock for flags etc slow things down?
	- no, because this model is not used in the upload mode
		- so don't worry about wide use of comb() too
- rather than use self.dut... to access ic pins, use a Record()

- think about how this file should look like (compared to the old file, amaram/old/sdram.../sdram_sim_model.py)
	- works with 'config_params' and 'test_params'
	- load externally define
	- allows testing smaller parts (e.g. just the refresh stuff)

"""

class sdram_sim_utils:
	def __init__(self, config_params, test_params):
		self.config_params = config_params
		self.test_params = test_params

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
	
	def assert_cmd_is(self, dut_ios, expected_cmd):
		assert sdram_cmds((yield dut_ios.cmd)) == expected_cmd

	def assert_idle_cmd_for(self, dut_ios, min_duration, focus_bank = None):
		""" 
		This will assert that for at least <min_duration> [seconds], the cmdi is in <valid_idle_states>.
		After that period, this function returns when cmdi not in <valid_idle_states>, with the new cmd.

		This will do some yields, so when this function returns/finishes (?),
		yields have been done so the next step can be done without more yields.

		thoughts
		- must wait this much time... but how to catch for if invalid commands etc occur in this time? ...cover / bmc?
		"""
		valid_idle_states = [sdram_cmds.CMD_NOP, sdram_cmds.CMD_DESL]
		initial_state = sdram_cmds((yield dut_ios.cmd))
		clks = 0
		while True:
			# yield Settle() # does this fix the simulations being a bit non-deterministic? (commant from pre-march 2022)
			cmd = sdram_cmds((yield dut_ios.cmd))
			if (cmd not in valid_idle_states) and ((cmd != initial_state) | (clks > 0)) and (True if (focus_bank == None) else ((yield dut_ios.ba) == focus_bank)):# (cmd == end_state):
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


class sdram_sim_model(sdram_sim_utils):
	def __init__(self, config_params, test_params):
		super().__init__(config_params, test_params)

	def get_refresh_monitor_process(self, dut_ios):
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
					yield from self.assert_cmd_is(dut_ios, sdram_cmds.CMD_REF)
					yield from self.assert_idle_cmd_for(dut_ios, min_duration = self.config_params.ic_timing.T_RC)
					
					# print("Only gets here if a refresh was done succesfully, ", counter)
					counter = (counter + increment_per_refresh) if ((counter + increment_per_refresh) < counter_max) else counter
					
					# yield from self.assert_cmd_then_delay(cmdi_states.CMD_REF,		min_duration = dram_ic_timing.T_RC)
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


				yield
				# print(counter)
		return func