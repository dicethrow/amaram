# parameters from the datasheet of the IS42S16160G sdram chip
# todo - implement a mechanism to select between different settings
# or different chips?

import enum

class rw_cmds(enum.Enum):
	RW_IDLE			= 0
	RW_READ_16W		= 1
	RW_WRITE_16W	= 2

class cmd_to_ic(enum.Enum):
	# based on p.9 of datasheet
	CMDO_DESL 		= 0 # device deselect
	CMDO_NOP 		= 1 # no operation
	CMDO_BST 		= 2 # burst stop
	CMDO_READ  		= 3 # read
	CMDO_READ_AP		= 4 # read with auto precharge
	CMDO_WRITE 		= 5 # write
	CMDO_WRITE_AP	= 6 # write with auto precharge
	CMDO_ACT			= 7 # bank activate
	CMDO_PRE 		= 8 # precharge select bank, to deactivate the open row in the chosen bank
	CMDO_PALL 		= 9 # precharge all banks, to deactivate the open row in all banks
	CMDO_REF 		= 10 # CBR auto-refresh
	CMDO_SELF 		= 11 # self-refresh
	CMDO_MRS 		= 12 # mode register set

class ic_timing(enum.Enum): # minimums
	T_STARTUP = 2e-6 # 100e-6 # for now, make it shorter, for simulation 
	# T_STARTUP = 100e-6 # for now, make it shorter, for simulation 
	T_RP	= 15e-9
	T_RC	= 60e-9
	T_RCD	= 15e-9
	T_MRD	= 14e-9 # this is very slightly over 2 clock cycles, so we use 3 clock cycles
	T_RAS	= 37e-9 # max is 100e-6
	T_XSR	= 70e-9
	# T_RAS 	= 0 # for precharge ?