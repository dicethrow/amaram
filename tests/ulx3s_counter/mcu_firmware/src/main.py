# 13apr2022
# main.py
# to be run on the ulx3s's esp32, using micropython

import uasyncio, fpga_io
from common.fpga_mcu_interface import register_addresses as addrs
# bytes(next(fpga_io.faster_raw_read_fifo(6*2,2)))

async def trigger_then_readback_fifo():

	while True:
		print("Checking fifo is ready to be triggered...")
		if fpga_io.reg_io(addrs.REG_FIFO_TRIGRDY_R):
			break
		else:
			await uasyncio.sleep_ms(200)

	print("Triggering fifo to fill")
	fpga_io.reg_io(addrs.REG_FIFO_TRIG_W, True, write_value=1) 
	fpga_io.reg_io(addrs.REG_FIFO_TRIG_W, True, write_value=0) 

	while True:
		print("Checking fifo is ready to be read...")
		if fpga_io.reg_io(addrs.REG_FIFO_READRDY_R):
			break
		else:
			await uasyncio.sleep_ms(200)
			print(fpga_io.reg_io(addrs.REG_FIFO_WCTR_R))

	print("Reading back fifo")
	await uasyncio.sleep_ms(200)
	i = 0
	while True:
		if not fpga_io.reg_io(addrs.REG_FIFO_READRDY_R):
			# wait a bit more to check that it is done
			await uasyncio.sleep_ms(200)
			if not fpga_io.reg_io(addrs.REG_FIFO_READRDY_R):
				# break
				print(",", end="")
			else:
				print("-----") # continue looping
		else:
			num_words = fpga_io.reg_io(addrs.REG_FIFO_WCTR_R)
			for j, next_16b_word_mv in enumerate(fpga_io.faster_raw_read_fifo(num_words*2, 2)): # is await right?
				# print(".", end="")
				i += 1
				print(bytes(next_16b_word_mv), i, j)
				# print(f"fifo read of {fpga_io.get_binary_string_at_least_n_bits_long(next_16b_word, 16)}, read index={i}, j={j}")
			break
				
	print("done")
			
			

def run_test():
	uasyncio.run(trigger_then_readback_fifo())

run_test()

