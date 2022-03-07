
import enum

class rw_cmds(enum.Enum):
	RW_IDLE			= 0
	RW_READ_16W		= 1
	RW_WRITE_16W	= 2

class cmd_to_ic(enum.Enum):
	# based on p.9 of datasheet of IS42S16160G
	CMDO_DESL 		= 0 # device deselect
	CMDO_NOP 		= 1 # no operation
	CMDO_BST 		= 2 # burst stop
	CMDO_READ  		= 3 # read
	CMDO_READ_AP	= 4 # read with auto precharge
	CMDO_WRITE 		= 5 # write
	CMDO_WRITE_AP	= 6 # write with auto precharge
	CMDO_ACT		= 7 # bank activate
	CMDO_PRE 		= 8 # precharge select bank, to deactivate the open row in the chosen bank
	CMDO_PALL 		= 9 # precharge all banks, to deactivate the open row in all banks
	CMDO_REF 		= 10 # CBR auto-refresh
	CMDO_SELF 		= 11 # self-refresh
	CMDO_MRS 		= 12 # mode register set

