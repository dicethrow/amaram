# fpga_gui_interface.py

# This defines the ila signals,
# so they can be both sampled on the FPGA and plotted correctly in a gtkwave gui

from amaranth import Signal, Const, Record

counter = Signal(20) #(31) # so with toggle, 32bit width, instead of 28
toggle  = Signal()
const_0xFF = Signal(8, reset=0xFF) # so 8

spi_monitor  = Record([
	('sck', 1),
	('sdi', 1),
	('sdo', 1),
	('cs',  1)
])
# spi_monitor = Signal(4)

def get_ila_signals_dict():
	# so we can more easily refer to each signal,
	# mainly useful in the fpga code
	ila_signals_dict = {
		"counter" : counter,
		"spi_monitor" : spi_monitor,
		"const_0xFF" : const_0xFF,
		# "toggle" : toggle
	}
	return ila_signals_dict

def get_ila_constructor_kwargs():
	kwargs = {
		"signals" : [v for k,v in get_ila_signals_dict().items()],
		"sample_depth" : 32, # number of words to buffer
		"domain" : "sync",
		"sample_rate" : 24e6, # assumed for sync clock? is that how it works?
		"samples_pretrigger" : 1 # not sure what this is
	}
	return kwargs

