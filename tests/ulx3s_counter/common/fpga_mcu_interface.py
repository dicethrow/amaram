""" 
13apr2022
This file is intended to provide some definitions and classes
so the fpga<-->mcu interface is easier to manage.
"""

def get_member_name(which_class, which_class_member):
	""" 
	e.g. 
	"REG_LEDS" = get_member_name(register_addresses, register_addresses.REG_LEDS)
	"""
	for k,v in which_class.__dict__.items():
		if getattr(which_class, k) == which_class_member:
			return k
	print("Unable to find match, for ", which_class.__dict__)

def get_member(which_class, which_member_name):
	""" 
	e.g. 
	register_addresses.REG_LEDS = get_member(register_addresses, "REG_LEDS")
	"""
	assert which_member_name in which_class.__dict__, which_member_name+" not in "+str(which_class.__dict__)
	return which_class.__dict__[which_member_name]

class spi_register_interface():
	CMD_ADDR_BITS		= 7 # 1 bit for read or write, 7bits for address
	REG_DATA_BITS		= 16 # same width as the fifo, which is one 16-bit pixel wide
	BYTE_PACK_FORMAT	= ">BH"


class register_base():
	reg_count = 1 # start at 1? or 0?

	@staticmethod
	def auto(doc = None):
		# In future, do something with doc strings.
		next_reg_addr = register_base.reg_count
		register_base.reg_count += 1
		return next_reg_addr

class register_addresses(register_base):
	REG_FIFO_TRIGRDY_R		= register_base.auto(doc = """ 
		On read: =1 if ready to trigger, =0 if not.
	""")

	REG_FIFO_TRIG_W		= register_base.auto(doc = """ 
		On write: trigger the fifo to start
	""")

	REG_FIFO_READRDY_R 		= register_base.auto(doc = """ 
		On read: 
			=1 if ready to read fifo data
			=0 if not. Assume that the 1->0 transition means the fifo has been fully read.
		
	""")

	REG_FIFO_WCTR_R = register_base.auto()

	# todo: read how many words in outbuffer, and do fiforeads of chunks of that size
