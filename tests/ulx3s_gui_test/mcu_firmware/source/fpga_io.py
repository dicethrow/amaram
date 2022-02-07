# tests on 31jan2022

from machine import Pin, SoftSPI, SPI
import struct, time
from termcolor import cprint

from test_common import register_addresses, spi_register_interface, get_member

spi = SPI(1, polarity=0, phase=1, bits=8, firstbit=SPI.MSB, baudrate = int(4e6), sck=Pin(16), mosi=Pin(4), miso=Pin(12))
csn = Pin(5, Pin.OUT)
csn.on()

def reg_io(addr = 0x00, write=False, write_value=0x0000, active_cs_level=0): # initial value
	# 31jan2022
	# works!
	write_mask = (2<<(spi_register_interface.CMD_ADDR_BITS-1))
	if addr >= write_mask: # then only 7 bits available for address, the other bit is read/write flag
		print("addr too large!")
		return 

	if write: # hence, is a write command
		addr |= write_mask

	# csn.value(1-active_cs_level)
	csn.value(active_cs_level)
	
	buf = bytearray(struct.pack(spi_register_interface.BYTE_PACK_FORMAT, addr, write_value))
	spi.write_readinto(buf, buf)
	_, result = struct.unpack(spi_register_interface.BYTE_PACK_FORMAT, buf)
	
	csn.value(1-active_cs_level)

	return result
	
# for i in range(255): _ = reg_io(4, True, i)

def alt_fifo_io(read_num=1, active_cs_level=1):
	# csn.value(1-active_cs_level)
	csn.value(active_cs_level)
	
	# numbytes = 4 # for 16bit reads
	# buf = bytearray(numbytes)

	pack_format = ">I" # unsigned int, 4byte=32bit
	
	for i in range(read_num):
		buf = bytearray(struct.pack(pack_format, 0))
		spi.write_readinto(buf, buf)
		result = struct.unpack(pack_format, buf)[0]
		# print(f"fifo read of {[hex(x) for x in buf]}")
		print(f"fifo read of {bin(result)}")

	csn.value(1-active_cs_level)

def flash_leds():
	for i in range(255):
		time.sleep(0.01)
		reg_io(register_addresses.REG_LEDS_RW, True, i)
		
def test_fifo(fifo_id, test_size, timeout=1):
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

	success = False
	fifo_regs = get_fifo_regs(fifo_id)
	data_mask = (1<<spi_register_interface.REG_DATA_BITS)-1
	print("Testing for fifo ", fifo_id)

	for i in range(test_size):
		while reg_io(fifo_regs["WRITERDY"]) != 0x1:
			if get_elapsed_secs() > timeout:
				print("Aborting")
				return success
			cprint("writerdy: "+hex(reg_io(fifo_regs["WRITERDY"])), "yellow")
		next_test_word = i & data_mask
		reg_io(fifo_regs["WRITE"], True, next_test_word)
		# reg_io(fifo_regs["WRITEEN"], True)
		cprint("Wrote "+hex(next_test_word), "green")

	i = 0
	while True:
	# for i in range(test_size+1):
		# reg_io(fifo_regs["READEN"], write=True)
		while reg_io(fifo_regs["READRDY"]) != 0x1:
			# if i >= test_size:
				# if reg_io(fifo_regs["READLVL"]) == 0:
				# 	success = True
				# 	return success

			if get_elapsed_secs() > timeout:
				print("Aborting")
				return success

			if reg_io(fifo_regs["READLVL"]) == 0:
				print("Readlevel==0 abort")
				return success

			cprint("readrdy: "+hex(reg_io(fifo_regs["READRDY"])), "yellow")
			cprint("readlvl: "+hex(reg_io(fifo_regs["READLVL"])), "yellow")

			

		next_test_word = reg_io(fifo_regs["READ"])
		cprint("Read "+hex(next_test_word), "blue")
		i += 1

	return success


		

