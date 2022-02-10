from .fpga_mcu_interface import *
from .mcu_gui_interface import *

# so micropython doesn't import this part
import sys
upython = sys.implementation.name == 'micropython'
if not upython:
	from .fpga_gui_interface import *
