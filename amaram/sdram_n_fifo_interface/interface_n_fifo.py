import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past, Initial, Array
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

from amaranth_boards.ulx3s import ULX3S_85F_Platform

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amlib.utils import Timer

from amtest.boards.ulx3s.common.upload import platform, UploadBase
from amtest.boards.ulx3s.common.clks import add_clock
from amtest.utils import FHDLTestCase, Params

from parameters_standard_sdram import sdram_cmds

from controller_pin import controller_pin
from controller_readwrite import controller_readwrite


""" 
This file is intended as the user interface, that will allow access to 
persistent data storage in sdram through a fifo interface.


Areas for improvement:
- Don't assume the width here is 16
- make some of the layout stuff dynamic?

"""

class sdram_n_fifo(Elaboratable):
	ui_layout = [
		("contains_data",	1,					DIR_FANIN) # not used yet
	]

	fifo_layout = [
		("w_data", 	16,							DIR_FANOUT),
		("w_rdy", 	1,							DIR_FANOUT),
		("w_en", 	1,							DIR_FANOUT),
		("w_level",	bits_for(50 + 1),			DIR_FANIN), 


		("r_data",	16,							DIR_FANIN),
		("r_rdy",	1,							DIR_FANIN),
		("r_en",	1,							DIR_FANOUT),
		("r_level",	bits_for(50 + 1),			DIR_FANIN),
	]

	
	def __init__(self, config_params, utest_params = None, utest: FHDLTestCase = None):
		super().__init__()

		self.config_params = config_params
		self.utest_params = utest_params
		self.utest = utest

		self.ui = Record(sdram_n_fifo.ui_layout)
		self.fifos = Array(Record(sdram_n_fifo.fifo_layout) for _ in range(self.config_params.num_fifos))

		# calculate some relations
		rw_params = self.config_params.rw_params
		self.config_params.global_word_addr_bits = rw_params.BANK_BITS.value + rw_params.ROW_BITS.value + rw_params.COL_BITS.value
		self.config_params.fifo_buf_id_bits = bits_for(self.config_params.num_fifos-1)
		self.config_params.fifo_buf_word_addr_bits = self.config_params.global_word_addr_bits - self.config_params.fifo_buf_id_bits

	def elaborate(self, platform = None):
		def get_and_set_up_buffer_fifos():
			# src_fifos[i] is a temporary buffer between data for data going from fpga->sdram
			src_fifos = Array(AsyncFIFOBuffered(
				width=self.config_params.fifo_width, 
				depth=self.config_params.fifo_depth, 
				r_domain="sync", # this is the clock domain used by the sdram 
				w_domain=self.config_params.fifo_write_domains[i] # this is the clock domain used by the user fpga 
				) for i in range(self.config_params.num_fifos))

			# dst_fifos[i] is a temporary buffer between data for data going from sdram->fpga
			dst_fifos = Array(AsyncFIFOBuffered(
				width=self.config_params.fifo_width, 
				depth=self.config_params.fifo_depth, 
				r_domain=self.config_params.fifo_read_domains[i],  # this is the clock domain used by the user fpga 
				w_domain="sync" # this is the clock domain used by the sdram 
				) for i in range(self.config_params.num_fifos))

			# add them as submodules
			for i, (src_fifo, dst_fifo) in enumerate(zip(src_fifos, dst_fifos)):
				m.submodules[f"fifo_{i}_src"] = src_fifo
				m.submodules[f"fifo_{i}_dst"] = dst_fifo

			# and make some control signals 
			fifo_controls = Array(Record([
				("words_stored_in_ram", 			self.config_params.fifo_buf_word_addr_bits),
				("fully_read",						1),
				("request_to_store_data_in_ram", 	1),
				("w_next_addr", 					self.config_params.fifo_buf_word_addr_bits),
				("r_next_addr", 					self.config_params.fifo_buf_word_addr_bits)
			]) for _ in range(self.config_params.num_fifos))

			# now make a 'virtual' fifo for each pair, made by tying the inputs and outputs together
			""" ____________________________________________________
				|self.fifos[<i>]                                       |
				|                                                   |
			-->-|-->-[src_fifo[<i>]]-->- ... ->--[dst_fifo[<i>]]->--|--->--
				|                                                   |
				|___________________________________________________|

			clock domains:

			|_________|        |_______________________|        |___________|
			 <write_i>                sync                         <read_i>
			"""

			for i in range(self.config_params.num_fifos):
				src_fifo = src_fifos[i]
				dst_fifo = dst_fifos[i]
				ui_fifo = self.fifos[i]

				m.d.comb += [ # should it be comb? I think yes, so the rw strobes line up 17mar2022
					src_fifo.w_data.eq(ui_fifo.w_data),
					ui_fifo.w_rdy.eq(src_fifo.w_rdy),
					src_fifo.w_en.eq(ui_fifo.w_en),
					ui_fifo.w_level.eq(src_fifo.w_level),

					ui_fifo.r_data.eq(dst_fifo.r_data),
					ui_fifo.r_rdy.eq(dst_fifo.r_rdy),
					dst_fifo.r_en.eq(ui_fifo.r_en),
					ui_fifo.r_level.eq(dst_fifo.r_level),
				]
			
			return src_fifos, dst_fifos, fifo_controls
		
		def route_data_through_sdram_or_bypass():
			# route src_fifo to dst_fifo, until it's full, and if the sdram is not storing fifo data
			# to start with, stream data straight from src_fifo to dst_fifo
			for i, (src_fifo, dst_fifo) in enumerate(zip(src_fifos, dst_fifos)):

				m.d.comb += fifo_controls[i].fully_read.eq(src_fifo.w_rdy & ~dst_fifo.r_rdy)

				with m.FSM(name=f"fifo_{i}_router_fsm"):

					with m.State("BYPASS_SDRAM"):
						with m.If(fifo_controls[i].words_stored_in_ram != 0):
							m.next = "USE_SDRAM"

						with m.Elif(src_fifo.r_rdy):
							with m.If(dst_fifo.w_rdy):
								# route it from src_fifo
								m.d.comb += [
									src_fifo.r_en.eq(dst_fifo.w_rdy),
									dst_fifo.w_data.eq(src_fifo.r_data),
									dst_fifo.w_en.eq(src_fifo.r_rdy)
								]
							with m.Else():
								# then we can't store the data in src_fifo, so we try to store it in ram.
								m.d.comb += [
									fifo_controls[i].request_to_store_data_in_ram.eq(1),

									src_fifo.r_en.eq(0),
									dst_fifo.w_data.eq(0), # so the traces look cleaner
									dst_fifo.w_en.eq(0)
								]						
					
					with m.State("USE_SDRAM"):
						with m.If(fifo_controls[i].words_stored_in_ram == 0):
							m.next = "BYPASS_SDRAM"
						
						# todo: should there be a check done around here that sdram contains at least a burstlen of space?

		def get_interface_and_set_up_readwrite_module():
			m.submodules.rw_ctrl = rw_ctrl = controller_readwrite(config_params)
			rw_ui = Record.like(rw_ctrl.ui)
			rw_pin_ui = Record.like(rw_ctrl.pin_ui)

			m.d.sync += [
				rw_ctrl.ui.connect(rw_ui),
				rw_pin_ui.connect(rw_ctrl.pin_ui)
			]

	


			
		
		def route_readback_pipeline_to_dstfifos():
			readback_fifo_id = Signal(shape=bits_for(self.config_params.num_fifos-1))
			readback_buf_addr = Signal(shape=self.config_params.fifo_buf_word_addr_bits)
			readback_global_addr = Signal(shape=self.config_params.global_word_addr_bits)
			# assuming the phase thing is accomplished by checking the low bits of the readback addr are zero

			# todo - finish and test! and add the readwrite controller too

		m = Module()

		ic_timing = self.config_params.ic_timing
		ic_refresh_timing = self.config_params.ic_refresh_timing

		src_fifos, dst_fifos, fifo_controls = get_and_set_up_buffer_fifos()
		route_data_through_sdram_or_bypass()

		rw_ui, rw_pin_ui = get_interface_and_set_up_readwrite_module()
		route_readback_pipeline_to_dstfifos()

		return m
		

if __name__ == "__main__":
	""" 
	feb2022 - mar2022

	Adding tests to each file, so I can more easily make 
	changes in order to improve timing performance.

	"""
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	if args.action == "generate": # formal testing
		...

	elif args.action == "simulate": # time-domain testing

		class fifoInterface_sim_thatWrittenFifos_canBeReadBack(FHDLTestCase):
			def test_sim(self):
				from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing, rw_params
				from model_sdram import model_sdram

				config_params = Params()
				config_params.clk_freq = 143e6
				config_params.ic_timing = ic_timing
				config_params.ic_refresh_timing = ic_refresh_timing
				config_params.rw_params = rw_params
				config_params.num_fifos = 4
				config_params.fifo_read_domains = [f"read_{i}" for i in range(config_params.num_fifos)]
				config_params.fifo_write_domains = [f"write_{i}" for i in range(config_params.num_fifos)]
				config_params.fifo_width = 16
				config_params.fifo_depth = 50

				utest_params = Params()
				utest_params.timeout_period = 20e-6 # seconds
				utest_params.read_clk_freqs = config_params.num_fifos * [4e6]#[60e6] 
				utest_params.write_clk_freqs = config_params.num_fifos * [40e6]#[40e6]
				utest_params.num_fifo_writes = 200 # 50

				dut = sdram_n_fifo(config_params, utest_params, utest=self)

				sim = Simulator(dut)

				sim.add_clock(period=1/config_params.clk_freq, domain="sync")
				# now add the read/write domains
				for i, (r_domain, w_domain) in enumerate(zip(config_params.fifo_read_domains, config_params.fifo_write_domains)):
					sim.add_clock(period=1/utest_params.read_clk_freqs[i], domain=r_domain) # represents faster reads
					sim.add_clock(period=1/utest_params.write_clk_freqs[i], domain=w_domain) # represents slower reads

				# sdram_model = model_sdram(config_params, utest_params)
				# for i in range(4): # num of banks
				# 	sim.add_sync_process(sdram_model.get_readwrite_process_for_bank(bank_id = i, dut_ios=dut.controller_pin_ui.ios))
				# sim.add_sync_process(sdram_model.propagate_i_dq_reads(dut_ios=dut.controller_pin_ui.ios))

				# all_writes_done = Signal(shape=range(config_params.num_fifos+1), reset=config_params.num_fifos)

				def write_counter_values_to_fifo(fifo, num_writes, fifo_id = 0, write_domain="sync"):
					# todo - add some random waits, to make this more realistic?
					def func():
						yield Active()
						# yield Passive()
						for i in range(num_writes):
							while (yield fifo.w_rdy) == 0:
								yield

							data = ((fifo_id << 4*3)|(i & 0xFFF))
							yield fifo.w_data.eq(data)
							yield fifo.w_en.eq(1)
							print(f"Wrote {hex(data)} to fifo={hex(fifo_id)}")

							if i == num_writes-1:
								yield fifo.w_en.eq(0)

							yield

						yield fifo.w_en.eq(0)
						# yield all_writes_done.eq((yield all_writes_done)-1)

						yield
						yield
					return func
				
				def read_counter_values_from_fifo(fifo, num_reads, fifo_id):
					# todo - add asserts that this reads the expected values (i.e. incrementing)
					def func():
						yield Active()
						last_read = None
						i = 0
						timeout_period = utest_params.timeout_period
						timeout_clks = int(timeout_period * utest_params.read_clk_freqs[fifo_id])
						stop = False
						while True:
							if timeout_clks == 0:
								print("Timeout!")
								break

							if (yield fifo.r_rdy):# and ((yield all_writes_done)==0):
								yield fifo.r_en.eq(1)

								yield
								timeout_clks -= 1
								
								if i == (num_reads-1): # right?
									stop = True
							
								# check if still ready? this fixed a bug where the same value was read twice
								if (yield fifo.r_rdy):
									data = (yield fifo.r_data)
									colors = ["red", "green", "yellow", "blue"]
									status = f"fifo={hex(fifo_id)}, read={hex(i)}: {hex(data)}"
									status += f" delta={data-last_read}" if (last_read != None) else ""
									cprint(status, colors[fifo_id])
									last_read = data
									i += 1
							
							else:
								yield fifo.r_en.eq(0)
								yield
								timeout_clks -= 1
							
							if stop:
								break
						
						# some end clocks
						for _ in range(10):
							yield

					return func
					

				for i in range(config_params.num_fifos):
					sim.add_sync_process(write_counter_values_to_fifo(
						dut.fifos[i], utest_params.num_fifo_writes, i), 
						domain=config_params.fifo_write_domains[i])
					
					sim.add_sync_process(read_counter_values_from_fifo(
						dut.fifos[i], utest_params.num_fifo_writes, i), 
						domain=config_params.fifo_read_domains[i])
					
				
				with sim.write_vcd(
					f"{current_filename}_{self.get_test_id()}.vcd"):
					sim.run()

	if args.action in ["generate", "simulate"]:
		# now run each FHDLTestCase above 
		import unittest
		sys.argv[1:] = [] # so the args used for this file don't interfere with unittest
		unittest.main()

	else: # upload
		class Upload(UploadBase):
			def __init__(self):
				super().__init__(sync_mode="sync_and_143e6_sdram_from_pll")
				
			def elaborate(self, platform = None):
				m, platform = super().elaborate(platform) 

				# from parameters_IS42S16160G_ic import ic_timing, ic_refresh_timing
				# from model_sdram import model_sdram

				# config_params = Params()
				# config_params.clk_freq = 143e6
				# config_params.ic_timing = ic_timing
				# config_params.ic_refresh_timing = ic_refresh_timing

				# m.submodules.tb = tb = DomainRenamer({"sync":"sdram"})(Testbench(config_params))

				# ui = Record.like(tb.ui)
				# m.d.sync += ui.connect(tb.ui)

				# def start_on_left_button():
				# 	start = Signal.like(self.i_buttons.left)
				# 	m.d.sync += [
				# 		start.eq(self.i_buttons.left),
				# 		ui.tb_fanout_flags.trigger.eq(Rose(start))
				# 	]

				# def reset_on_right_button():
				# 	# don't manually route the reset - do this, 
				# 	# otherwise, if Records are used, they will oscillate, as can't be reset_less
				# 	m.d.sync += ResetSignal("sync").eq(self.i_buttons.right) 

				# def display_on_leds():
				# 	m.d.comb += self.leds.eq(Cat([
				# 		ui.tb_fanin_flags.in_normal_operation,
				# 		ui.tb_fanin_flags.in_requesting_refresh,
				# 		ui.tb_fanin_flags.in_performing_refresh,
				# 		self.i_buttons.right,  		# led indicates that the start button was pressed
				# 		self.i_buttons.left			# led indicates that the reset button was pressed
				# 	]))

				# start_on_left_button()
				# reset_on_right_button()
				# display_on_leds()

				# return DomainRenamer("sdram")(m)
				return m
		
		platform.build(Upload(), do_program=False, build_dir=f"{current_filename}_build")