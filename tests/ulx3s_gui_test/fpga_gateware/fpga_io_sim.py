

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered

from amlib.io import SPIRegisterInterface

import os, sys
import struct
import time
# import amaram
# from amaram.sdram_n_fifo_interface_IS42S16160G import sdram_controller

import sys, os
from termcolor import cprint

sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common.fpga_mcu_interface import register_addresses, spi_register_interface, get_member

spi_freq = 1e6

def spi_write_readinto(dut, srcbuf, dstbuf):
	assert len(srcbuf) == len(dstbuf)
	assert type(dstbuf) == bytearray # so we can edit it
	# is there a problem if srcbuf = dstbuf?
	for byte_index in range(len(srcbuf)):
		for bit_index in range(7, -1, -1):
			send_bit = (int(srcbuf[byte_index]) >> bit_index) & 0b1

			yield dut.copi.eq(send_bit)

			yield Delay(0.5/spi_freq)

			yield dut.sclk.eq(0)#(1)
			rx_bit = (yield dut.cipo)
			dstbuf[byte_index] |= (rx_bit << bit_index)

			yield Delay(0.5/spi_freq)
			yield dut.sclk.eq(1)#(0)



def reg_io(dut, addr, write=False, write_value=0x0000, active_cs_level=0): # to be similar to the fpga_io.py micropython file
	write_mask = (2<<(spi_register_interface.CMD_ADDR_BITS-1))
	if addr >= write_mask: # then only 7 bits available for address, the other bit is read/write flag
		print("addr too large!")
		return

	initial_addr = addr
	if write: # hence, is a write command
		addr |= write_mask

	# yield dut.csn.eq(~active_cs_level)
	yield Tick()
	yield dut.csn.eq(active_cs_level)

	buf = bytearray(struct.pack(spi_register_interface.BYTE_PACK_FORMAT, addr, write_value))
	yield from spi_write_readinto(dut, buf, buf)
	_, result = struct.unpack(spi_register_interface.BYTE_PACK_FORMAT, buf)
	
	yield dut.csn.eq(~active_cs_level)
	
	# new
	yield Delay(0.5/spi_freq)


	print(f'reg io of {hex(initial_addr)} {"write" if write else "read"} result is {hex(result)}')
	return result

def alt_fifo_io(dut, read_num=1, active_cs_level = 1):
	# cs toggle to reset - needed?
	# yield dut.csn.eq(~active_cs_level)
	yield Tick()
	yield dut.csn.eq(active_cs_level)

	# for 16bit reads
	# numbytes = 2
	# buf = bytearray(numbytes)
	# for i in range(read_num):
	# 	yield from spi_write_readinto(dut, buf, buf)
	# 	print(f"fifo read of {[hex(x) for x in buf]}")

	pack_format = ">I" # unsigned int, 4byte=32bit
	buf = bytearray(struct.pack(pack_format, 0))
	for i in range(read_num):
		buf = bytearray(struct.pack(pack_format, 0))
		yield from spi_write_readinto(dut, buf, buf)
		result = struct.unpack(pack_format, buf)[0]
		# print(f"fifo read of {[hex(x) for x in buf]}")
		print(f"fifo read of {bin(result)}")
	

	yield dut.csn.eq(~active_cs_level)

			
def test_fifo(dut, fifo_id, test_size, timeout=0.1):
	def get_fifo_regs(fifo_id):
		i = str(fifo_id)
		fifo_regs = {
			"READ" : get_member(register_addresses, "REG_FIFO"+i+"_READ_R"),
			# "READEN" : get_member(register_addresses, "REG_FIFO"+i+"_READEN_W"),
			"READRDY" : get_member(register_addresses, "REG_FIFO"+i+"_READRDY_R"),
			"READLVL" : get_member(register_addresses, "REG_FIFO"+i+"_READLVL_R"),
			"WRITE" : get_member(register_addresses, "REG_FIFO"+i+"_WRITE_W"),
			# "WRITEEN" : get_member(register_addresses, "REG_FIFO"+i+"_WRITEEN_W"),
			"WRITERDY" : get_member(register_addresses, "REG_FIFO"+i+"_WRITERDY_R"),
			"WRITELVL" : get_member(register_addresses, "REG_FIFO"+i+"_WRITELVL_R"),
		}
		return fifo_regs

	start_timestamp_ns = time.time_ns()
	def get_elapsed_secs():
		return (time.time_ns() - start_timestamp_ns)/10e8

	def get_next_test_word(i):
		next_test_word = i & data_mask
		next_test_word &= 0xFFF
		next_test_word |= 0xA000+0x1000*(fifo_id) # so 0 is now 0xA000, easier to see in traces
		print("Next test word: ", hex(next_test_word))
		return next_test_word

	success = False
	fifo_regs = get_fifo_regs(fifo_id)
	data_mask = (1<<spi_register_interface.REG_DATA_BITS)-1
	print("Testing for fifo ", fifo_id)

	for i in range(test_size):
		while (yield from reg_io(dut, fifo_regs["WRITERDY"])) != 0x1:
			if get_elapsed_secs() > timeout:
				print("Aborting")
				return success
			yield Delay(1/1e6)
			cprint("writerdy: "+hex((yield from reg_io(dut, fifo_regs["WRITERDY"]))), "yellow")
		next_test_word = get_next_test_word(i)
		yield from reg_io(dut, fifo_regs["WRITE"], True, next_test_word)
		# reg_io(fifo_regs["WRITEEN"], True)
		cprint("Wrote "+hex(next_test_word), "green")

	i = 0
	while True:
	# for i in range(test_size+1):
		# reg_io(fifo_regs["READEN"], write=True)
		while (yield from reg_io(dut, fifo_regs["READRDY"])) != 0x1:
			# if i >= test_size:
				# if reg_io(fifo_regs["READLVL"]) == 0:
				# 	success = True
				# 	return success

			if get_elapsed_secs() > timeout:
			# yield Delay(1/1e6)
				print("Aborting")
				return success

			if (yield from reg_io(dut, fifo_regs["READLVL"])) == 0:
				print("Readlevel==0 abort")
				return success

			cprint("readrdy: "+hex((yield from (reg_io(dut, fifo_regs["READRDY"])))), "yellow")
			cprint("readlvl: "+hex((yield from reg_io(dut, fifo_regs["READLVL"]))), "yellow")

		next_test_word = yield from reg_io(dut, fifo_regs["READ"])
		cprint("Read "+hex(next_test_word), "blue")
		i += 1

	return success
