
import enum

class rw_cmds(enum.Enum):
	RW_IDLE			= 0
	RW_READ_16W		= 1
	RW_WRITE_16W	= 2

class sdram_cmds(enum.Enum):
	# based on p.9 of datasheet of IS42S16160G
	CMD_DESL 		= 0 # device deselect
	CMD_NOP 		= 1 # no operation
	CMD_BST 		= 2 # burst stop
	CMD_READ  		= 3 # read
	CMD_READ_AP		= 4 # read with auto precharge
	CMD_WRITE 		= 5 # write
	CMD_WRITE_AP	= 6 # write with auto precharge
	CMD_ACT			= 7 # bank activate
	CMD_PRE 		= 8 # precharge select bank, to deactivate the open row in the chosen bank
	CMD_PALL 		= 9 # precharge all banks, to deactivate the open row in all banks
	CMD_REF 		= 10 # CBR auto-refresh
	CMD_SELF 		= 11 # self-refresh
	CMD_MRS 		= 12 # mode register set

	CMD_ILLEGAL		= 13 # is this right?


class mode_burst_length(enum.Enum):
	MODE_BURSTLEN_1		= 0b0
	MODE_BURSTLEN_2		= 0b1
	MODE_BURSTLEN_4		= 0b10
	MODE_BURSTLEN_8		= 0b11
	MODE_BURSTLEN_PAGE	= 0b111

class mode_burst_type(enum.Enum):
	MODE_BSTTYPE_SEQ 	= 0b0
	MODE_BSTTYPE_INT	= 0b1

class mode_latency(enum.Enum):
	# this is also the int representation
	MODE_CAS_2			= 0b10
	MODE_CAS_3			= 0b11

class mode_operation(enum.Enum):
	MODE_STANDARD		= 0b00

class mode_write_burst(enum.Enum):
	MODE_WBST_ENABLE	= 0b0
	MODE_WBST_SINGLE	= 0b1