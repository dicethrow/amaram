# c_app.py Client-side application demo

# Released under the MIT licence. See LICENSE.
# Copyright (C) Peter Hinch 2018-2020

# Now uses and requires uasyncio V3. This is incorporated in daily builds
# and release builds later than V1.12

import gc, time
import uasyncio as asyncio
gc.collect()
from iot import client
gc.collect()
import ujson
import esp32
# Optional LED. led=None if not required
from sys import platform
# if platform == 'pyboard':  # D series
# 	from pyb import LED
# 	led = LED(1)
# else:
# 	from machine import Pin
# 	led = Pin(2, Pin.OUT, value=1)  # Optional LED
led = None
# End of optional LED
from iot.primitives import queue

from test_common.mcu_gui_interface import MY_ID, SERVER, PORT, SSID, PW, TIMEOUT
import fpga_io

from termcolor import cprint

gc.collect()

class spi_interface():
	""" 
	Does stuff with the SPI interface. 
	todo: 
		add a lock as class instance so only one instance can access the same hardware at a time?
		Could do it as a base hardware_access_manager class?
	"""
	def __init__(self, cipo_queue, copi_queue):
		asyncio.create_task(self.register_io(cipo_queue, copi_queue))

	async def register_io(self, cipo_queue, copi_queue):
		while True:
			# print("Waiting for register_io command")
			data = await(copi_queue.get())
			
			if data["request"] == "read_register":
				cprint("Reading register " + hex(data["reg_address"]), "green") # todo - can we decode this using the reg enum thing?
				data["response"] = {
					"read_value" : fpga_io.reg_io(data["reg_address"], False, 0x00)
				}
				await cipo_queue.put(data)

			elif data["request"] == "write_register":
				# print("Reg writes not implemented yet, skipping")
				cprint("Writing to register " + hex(data["reg_address"]), "green")
				fpga_io.reg_io(data["reg_address"], True, data["write_value"])
				data["response"] = {
					"write_success" : True
				}
				await cipo_queue.put(data)
			
			elif data["request"] == "flash_leds":
				cprint("Flashing LEDs", "green")
				fpga_io.flash_leds()
				data["response"] = "flash_leds_sucessful" 
				await cipo_queue.put(data)
				
			elif data["request"] == "test_fifo":
				cprint("Testing FIFO", "green")
				result = fpga_io.test_fifo(data["fifo_id"], data["test_size"])
				data["response"] = {
					"fifo_test" : result
				}
				await cipo_queue.put(data)

			else:
				cprint("Unrecognised in register_io: " + data, "red")

class App(client.Client):
	def __init__(self, verbose):
		self.verbose = verbose # what is 'verbose' and how does it work?
		self.cl = client.Client(MY_ID, SERVER, PORT, SSID, PW, TIMEOUT, 
			conn_cb=self.constate, verbose=verbose, led=led, wdog=False)
								
	
	async def start(self):
		print("Starting new mcu app")
		self.verbose and cprint('App awaiting connection.', "green")
		await self.cl
		cipo_queue = queue.Queue(maxsize=10) # arbitrary value?
		copi_queue = queue.Queue(maxsize=10) # arbitrary value?
		# spi_lock = asyncio.Lock() # so only one access to the fpga spi bus at a time

		asyncio.create_task(self.send_to_server_from(cipo_queue))
		asyncio.create_task(self.recv_from_server_to(copi_queue))

		self.spi_interface = spi_interface(cipo_queue, copi_queue)

		while True: # loiter?
			await asyncio.sleep(1) # needed?

	def constate(self, state):
		print("Connection state:", state)

	async def recv_from_server_to(self, copi_queue):
		print("Starting recv_from_server")
		while True:
			await asyncio.sleep_ms(0) # necessary?
			line = await self.cl.readline()
			print()
			data = ujson.loads(line)

			if "request" in data:
				await copi_queue.put(data)
	
			else:
				cprint("Unknown data recieved: " + line, "red")
	
	async def send_to_server_from(self, cipo_queue):
		print("Starting send_to_server")
		while True:
			await asyncio.sleep_ms(0) # necessary?
			data = await cipo_queue.get()
			line = ujson.dumps(data)
			await self.cl.write(line)
			# print("Got ", line, "from cipo_queue, sent to server")
			cprint(line, "blue")
			# await asyncio.sleep(0.1) # needed?

	def shutdown(self):
		self.cl.close()  # Shuts down WDT (but not on Pyboard D).

app = None
async def main():
	global app  # For finally clause
	app = App(verbose=True)
	await app.start()

try:
	asyncio.run(main())
finally:
	app.shutdown()
	asyncio.new_event_loop()
