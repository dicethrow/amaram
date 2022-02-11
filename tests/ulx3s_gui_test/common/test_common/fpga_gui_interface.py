# fpga_gui_interface.py

# This defines the ila signals,
# so they can be both sampled on the FPGA and plotted correctly in a gtkwave gui

from amaranth import Signal, Const, Record

counter = Signal(16) #(31) # so with toggle, 32bit width, instead of 28
toggle  = Signal()
const_0xAB = Signal(8, reset=0xAB) # so 8

# fifo_monitor0 = Record([
# 	("READEN", 1),
# 	("READRDY", 1),
# 	("READLVL", 8), 	# let's just get the lowest 8bits?

# 	("WRITERDY")


# ])

# spi_monitor0  = Record([
# 	('sck', 1),
# 	('sdi', 1),
# 	('sdo', 1),
# 	('cs',  1)
# ])


def get_ila_signals_dict():
	# so we can more easily refer to each signal,
	# mainly useful in the fpga code
	ila_signals_dict = {
		"counter" : counter,
		# "spi_monitor0" : spi_monitor0,
		# "spi_monitor1" : spi_monitor1,
		"const_0xAB" : const_0xAB,
		# "toggle" : toggle
	}
	return ila_signals_dict

def get_ila_constructor_kwargs():
	kwargs = {
		"signals" : [v for k,v in get_ila_signals_dict().items()],
		"sample_depth" : 100, # number of words to buffer
		"domain" : "sync",
		"sample_rate" : 24e6, # assumed for sync clock? is that how it works?
		"samples_pretrigger" : 1 # not sure what this is
	}
	return kwargs

