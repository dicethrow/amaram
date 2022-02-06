# MY_ID = '1'  # Client-unique string
# SERVER = '192.168.1.107'
# SSID = 'WiFi-41E4' # Put in your WiFi credentials
# PW = '04431961'
# PORT = 8123
# TIMEOUT = 4000 # 2000

# The following may be deleted
# if SSID == 'use_my_local':
    # from iot.examples.my_local import *

# import iot.examples.c_app


# Create message ID's. Initially 0 then 1 2 ... 254 255 1 2
def gmid():
	mid = 0
	while True:
		yield mid
		mid = (mid + 1) & 0xff
		mid = mid if mid else 1

# Return True if a message ID has not already been received
def isnew(mid, lst=bytearray(32)):
	if mid == -1:
		for idx in range(32):
			lst[idx] = 0
		return
	idx = mid >> 3
	bit = 1 << (mid & 7)
	res = not(lst[idx] & bit)
	lst[idx] |= bit
	lst[(idx + 16 & 0x1f)] = 0
	return res


