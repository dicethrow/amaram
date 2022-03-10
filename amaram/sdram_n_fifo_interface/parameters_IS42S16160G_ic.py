# parameters from the datasheet of the IS42S16160G sdram chip

from enum import Enum

class ic_timing(Enum): # minimums
	#T_STARTUP = 2e-6 # 100e-6 # for now, make it shorter, for simulation 
	T_STARTUP = 100e-6 # for now, make it shorter, for simulation 
	T_RP	= 15e-9
	T_RC	= 60e-9
	T_RCD	= 15e-9
	T_MRD	= 14e-9 # this is very slightly over 2 clock cycles, so we use 3 clock cycles
	T_RAS	= 37e-9 # max is 100e-6
	T_XSR	= 70e-9
	# T_RAS 	= 0 # for precharge ?

class ic_refresh_timing(Enum):
	T_REF	= 32e-3
	NUM_REF	= 8192

class rw_params(Enum):
	BANK_BITS 	= 2
	ROW_BITS 	= 11
	COL_BITS 	= 9
	DATA_BITS 	= 16

