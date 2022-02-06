""" 
1feb2022
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
	reg_count = 0

	@staticmethod
	def auto():
		next_reg_addr = register_base.reg_count
		register_base.reg_count += 1
		return next_reg_addr

class register_addresses(register_base):
	REG_AUTONEG_R			= register_base.auto()
	# REG_CONST_0xAF_R		= register_base.auto()
	REG_BUTTONS_R			= register_base.auto()
	# REG_GENPURP_0_RW		= register_base.auto()
	REG_LEDS_RW				= register_base.auto()

	REG_ILA_TRIG_RW			= register_base.auto()

	REG_FIFO0_READ_R		= register_base.auto()
	# REG_FIFO0_READEN_W		= register_base.auto() # the write-strobe on this strobes the fifo out, presenting the next data
	REG_FIFO0_READRDY_R		= register_base.auto()
	REG_FIFO0_READLVL_R		= register_base.auto()
	REG_FIFO0_WRITE_W		= register_base.auto()
	# REG_FIFO0_WRITEEN_W		= register_base.auto() # same as above
	REG_FIFO0_WRITERDY_R	= register_base.auto()
	REG_FIFO0_WRITELVL_R	= register_base.auto()

	REG_FIFO1_READ_R		= register_base.auto()
	# REG_FIFO1_READEN_W		= register_base.auto() # the write-strobe on this strobes the fifo out, presenting the next data
	REG_FIFO1_READRDY_R		= register_base.auto()
	REG_FIFO1_READLVL_R		= register_base.auto()
	REG_FIFO1_WRITE_W		= register_base.auto()
	# REG_FIFO0_WRITEEN_W		= register_base.auto() # same as above
	REG_FIFO1_WRITERDY_R	= register_base.auto()
	REG_FIFO1_WRITELVL_R	= register_base.auto()
	




