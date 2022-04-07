import sys, os
from termcolor import cprint
from typing import List
import textwrap
import numpy as np
import enum

from amaranth import (Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, 
	ResetSignal, Cat, Const, Array)
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

from amaranth_boards.ulx3s import ULX3S_85F_Platform

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amlib.utils import Timer

from amtest.boards.ulx3s.common.upload import platform, UploadBase
from amtest.boards.ulx3s.common.clks import add_clock
from amtest.utils import FHDLTestCase, Params

from parameters_standard_sdram import sdram_cmds, rw_cmds

class sdram_fifo_interfaces:
	@staticmethod 
	def get_ui_fifo_layout(config_params):
		# todo: add the ability to make the fifo io not always 16bits wide
		fifo_layout = [
			("w_data", 	16,							DIR_FANOUT),
			("w_rdy", 	1,							DIR_FANIN), # ugh, this was the wrong way around
			("w_en", 	1,							DIR_FANOUT),
			("w_level",	bits_for(50 + 1),			DIR_FANIN), 

			("r_data",	16,							DIR_FANIN),
			("r_rdy",	1,							DIR_FANIN),
			("r_en",	1,							DIR_FANOUT),
			("r_level",	bits_for(50 + 1),			DIR_FANIN),
		]
		return fifo_layout

class controller_readwrite_interfaces:
	@staticmethod 
	def get_ui_layout(config_params):
		ui_layout = [
			("rw_copi", [
				# This is to either do a write with this w_data, 
				# or to trigger a pipelined read on the address
				("task",	rw_cmds,	DIR_FANOUT),
				("addr",	config_params.rw_params.get_ADDR_BITS(),	DIR_FANOUT),
				("w_data",	config_params.rw_params.DATA_BITS.value,	DIR_FANOUT),
			]),
			("r_cipo", [
				# this is to recieve the pipelined read that is
				# scheduled using the above pipeline
				("read_active",	1,	DIR_FANIN),
				("addr",	config_params.rw_params.get_ADDR_BITS(),	DIR_FANIN),
				("r_data",	config_params.rw_params.DATA_BITS.value,	DIR_FANIN),
			]),
			("in_progress",		1,		DIR_FANIN)
		]

		return ui_layout

class controller_pin_interfaces:
	@staticmethod
	def _get_rw_pipeline_layout(config_params, _dir):
		# this is to enable the ability to read back pipelined data easily

		rw_pipeline_layout = [
			("dq",			config_params.rw_params.DATA_BITS.value,		_dir),
			("dq_oen",		1,			_dir),
			("read_active",	1,			_dir),		# whether or not a read will be active on the dq bus in read mode
			("a",			config_params.rw_params.A_BITS.value,			_dir),
			("ba",			config_params.rw_params.BANK_BITS.value,		_dir),
			("addr",		config_params.rw_params.get_ADDR_BITS(),		_dir),
		]

		return rw_pipeline_layout

	@staticmethod
	def _get_common_io_layout(config_params):
		io_layout_common = [
			("clk_en", 		1,		DIR_FANOUT),
			("dqm",			1, 		DIR_FANOUT), # todo: treat this as 2-bits

			("rw_copi", 	controller_pin_interfaces._get_rw_pipeline_layout(config_params, DIR_FANOUT)), 
			("rw_cipo", 	controller_pin_interfaces._get_rw_pipeline_layout(config_params, DIR_FANIN)),  
		]
		return io_layout_common

	@staticmethod
	def get_sub_ui_layout(config_params):
		sub_ui_layout = [
			("cmd", sdram_cmds, 	DIR_FANOUT), # a high-level representation of the desired cmd
		] + controller_pin_interfaces._get_common_io_layout(config_params)
		return sub_ui_layout

	@staticmethod
	def get_ui_layout(config_params):
		# this represents the inter-module user interface
		ui_layout = [
			("bus_is_refresh_not_readwrite",	1,		DIR_FANOUT), # 1 means the refresh controller has command, 0 means the rw controller has command
			("refresh",	controller_pin_interfaces.get_sub_ui_layout(config_params)),
			("readwrite",	controller_pin_interfaces.get_sub_ui_layout(config_params))
		]
		return ui_layout

	@staticmethod
	def get_io_layout(config_params):
		# this is between the inter-module ui and the pins of the sdram chip
		io_layout = [
			("cs",			1,		DIR_FANOUT),
			("we",			1,		DIR_FANOUT),
			("ras",			1,		DIR_FANOUT),
			("cas",			1,		DIR_FANOUT)
		] + controller_pin_interfaces._get_common_io_layout(config_params)

		return io_layout

class controller_refresh_interfaces:
	@staticmethod
	def get_ui_layout(config_params):
		ui_layout = [
				("initialised",	1,				DIR_FANIN),	# high until set low later on
				# ("startup_complete",	1,		DIR_FANIN),
				("request_to_refresh_soon",	1,	DIR_FANIN),	# 
				("enable_refresh",	1,			DIR_FANOUT),
				("refresh_in_progress",	1,		DIR_FANIN),
				("refresh_lapsed",	1,			DIR_FANIN) # to indicate whether data loss from a lack of refreshing has occurred
			]
		return ui_layout