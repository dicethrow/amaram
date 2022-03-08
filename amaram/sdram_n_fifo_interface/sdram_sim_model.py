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
def assert_cmd_is(dut_o_cmd, expected_cmd):
	assert cmdi_states((yield dut_o_cmd)) == expected_cmd

def assert_idle_cmd_for():
	...
	# todo: migrate from the old cmd

def refresh_monitor_process(config_params, test_params, dut_pins):
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
	
	# this assumes that the counter will reduce by 1 each clock cycle,
	# representing the time passing as a measure of capacitor leakage, which is the
	# whole reason the refresh mechanism exists, to compensates for it
	period_s = config_params.ic_refresh_timing.T_REF.value
	refreshes_per_period = config_params.ic_refresh_timing.NUM_REF.value
	clks_per_period = period_s * config_params.clk_freq
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
			yield from assert_cmd_is(dut_pins.o_cmd, sdram_cmds.CMD_REF)
			yield from assert_idle_cmd_for(dut_pins.o_cmd, min_duration = dram_ic_timing.T_RC)
			
			# print("Only gets here if a refresh was done succesfully, ", counter)
			counter = (counter + increment_per_refresh) if ((counter + increment_per_refresh) < counter_max) else counter
			
			# yield from self.assert_cmd_then_delay(cmdi_states.CMD_REF,		min_duration = dram_ic_timing.T_RC)
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