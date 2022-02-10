from . import fpga_mcu_interface
from . import mcu_gui_interface

# so micropython doesn't import this part
import sys
upython = sys.implementation.name == 'micropython'
if not upython:
	from . import fpga_gui_interface
