Goal: To be able to have a common python library that can be used for things that overlap in the FPGA, GUI, MCU etc.

Implementation: By making this module, which is made available to python by either:

1. For normal python code, by adding the import location to the import search path by

	```python
	sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
	from test_common import register_addresses
	```

2. For micropython code, by using `rsync` to copy the `test_common` folder to the board, along with the rest of the firmware.

