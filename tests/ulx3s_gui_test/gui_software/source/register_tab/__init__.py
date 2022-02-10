
from PyQt5 import QtCore, QtGui, QtWidgets, QtOpenGL
from PyQt5.QtCore import (
	Qt, pyqtSlot, pyqtSignal, QThread, QPoint, QUrl, QSize, QModelIndex, 
	QBuffer,QIODevice, QRegExp,
	QDir)
from PyQt5.QtWidgets import (
	QWidget, QFrame, QTextEdit, QPushButton, QPlainTextEdit, QSplitter, QLabel, 
	QTreeView, QTabWidget, QFrame, QFileSystemModel, 
	QTableWidget, QAbstractScrollArea, QTableWidgetItem,
	QFileDialog, QStyle, QStatusBar,
	QSlider, QItemDelegate, QSpinBox,
	QVBoxLayout, QHBoxLayout,
	QSizePolicy,
	QApplication, QMainWindow)
from PyQt5.QtGui import (
	QValidator, QTextCursor, QRegExpValidator
)

import sys, os, json
sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common import fpga_mcu_interface
class register_table(QTableWidget):
	""" 
	to provide a table of registers, and provide a way to write to and read them

	todo: 
		- add a button to read all registers
	"""

	class register_button(QPushButton):
		response_signal = pyqtSignal(dict)
		def __init__(self, label, containing_table):
			super().__init__(label)
			self.containing_table = containing_table
			self.reg_addr = ""
			self.action_string = ""
			self.action_location = []

			self.response_signal.connect(self.recieve_result)

		def recieve_result(self, cipo_data):
			# print("reg button got this: ", cipo_data)
			self.setEnabled(True)
			# print("Thing is ", self.containing_table.itemAt(self.action_location[0], self.action_location[1]))
			
			if self.action_string == "read_register":
				read_value = (cipo_data["response"]["read_value"])
				item = self.containing_table.item(*self.action_location)
				item.setText(f"{hex(read_value)}")
				# item = QTableWidgetItem(f"{read_value}")
				# self.containing_table.setItem(self.action_location[0], self.action_location[1], item)
			
			elif self.action_string == "write_register":
				# item = hex()
				# print("Sender is ", self.sender())
				pass
			
	# too hard - do a try/except instead						
	# class registerWriteEditDelegate(QItemDelegate):
	# 	# this is the 'edit delegate' for editing cells in the table, 
	# 	# so only valid input is used for writing to registers
	# 	# Not perfect but easily good enough
	# 	# from https://stackoverflow.com/questions/37621753/how-validate-a-cell-in-qtablewidget

	# 	class PlainTextEdit(QPlainTextEdit):
	# 		# from https://stackoverflow.com/questions/45674354/alowing-only-ints-into-qplaintextedit
	# 		def __init__(self, parent=None):
	# 			QPlainTextEdit.__init__(self, parent)
	# 			# regexp = QRegExp('^([01]?[0-9]?[0-9]|2[0-4][0-9]|25[0-5])$')
	# 			regexp = QRegExp('^(0|[xX][0-9a-fA-F]+)$')
	# 			self.validator= QRegExpValidator(regexp)
	# 			# self.document().contentsChange.connect(self.onContentsChange)
			
	# 		def keyPressEvent(self, event):
	# 			state = self.validator.validate(event.text(), 0)
	# 			if state[0] == QValidator.Acceptable:
	# 				QtWidgets.QPlainTextEdit.keyPressEvent(self, event)
			
	# 	def createEditor(self, parent, option, index):
	# 		# return QSpinBox(parent)
	# 		return register_table.registerWriteEditDelegate.PlainTextEdit(parent)



	def __init__(self):
		super().__init__()
		self.connmanager = None # to start with 
		# self.setEditTriggers(QTableWidget.NoEditTriggers)  # so each cell defaults to being non editable
		
		def init_members():
			# add content to table
			reg_addresses_dict = {k:v for k,v in fpga_mcu_interface.register_addresses.__dict__.items() if type(v)==int}

			self.read_buttons = {addr: register_table.register_button("Read", self) for name,addr in reg_addresses_dict.items()}
			self.write_buttons = {addr: register_table.register_button("Write", self) for name,addr in reg_addresses_dict.items()}

			def make_table():
				column_labels = ["Address", "Name", "Last read value", "Read trigger", "Next write value", "Write trigger"]

				self.setColumnCount(len(column_labels))
				self.setHorizontalHeaderLabels(column_labels)	
				self.setRowCount(len(reg_addresses_dict))

				for m, (register_name, register_addr) in enumerate(reg_addresses_dict.items()):
					for n, column_label in enumerate(column_labels):
					
						if column_label == "Address":
							item = QTableWidgetItem(hex(register_addr))
							item.setFlags(item.flags() & ~Qt.ItemIsEditable) 
							self.setItem(m, n, item)
							
						elif column_label == "Name":
							item = QTableWidgetItem(register_name) 
							item.setFlags(item.flags() & ~Qt.ItemIsEditable) 
							self.setItem(m, n, item)

						elif column_label == "Last read value":
							item = QTableWidgetItem("")  # as none read yet
							item.setFlags(item.flags() & ~Qt.ItemIsEditable) 
							self.setItem(m, n, item)
						
						elif column_label == "Next write value":
							item = QTableWidgetItem()
							item.setFlags(item.flags() | Qt.ItemIsEditable) # make editable
							# self.setItemDelegateForColumn(n, register_table.registerWriteEditDelegate())
							self.setItem(m, n, item)

						elif column_label in ["Read trigger", "Write trigger"]:
							if column_label == "Read trigger":
								item = self.read_buttons[register_addr]
								readwrite_teststring = "R"
								item.action_string = "read_register"

							elif column_label == "Write trigger":
								item = self.write_buttons[register_addr]
								readwrite_teststring = "W"
								item.action_string = "write_register"
							
							item.reg_addr = register_addr
							item.action_location = [m,n-1] # as the write or read happens to the left cell

							if readwrite_teststring in register_name.split("_")[-1]:
								# then we can click and interact with it
								def handleButtonClicked():
									button = self.sender() # note! use 'button' for callback behaviour, not 'item'.
									if self.connmanager:
										copi_data = {
												"request": button.action_string,
												"reg_address" : button.reg_addr
											}
										if copi_data["request"] == "write_register":
											value_entry_widget = self.item(*button.action_location)
											try:
												value_int = int(value_entry_widget.text(),0) # handle e.g. 0xAB or 0b01
											except:
												print(f"Invalid entry of {value_entry_widget.text()}, skipping")
												return 
											copi_data["write_value"] = value_int #0x00 # read from other?
											
										self.connmanager.send_cmd(copi_data)
										button.setEnabled(False)
								
								item.clicked.connect(handleButtonClicked)
								self.setCellWidget(m, n, item)
							else:
								# then we can't do this action with this register, blank it out
								item = QTableWidgetItem("")
								item.setFlags(item.flags() & ~Qt.ItemIsEditable) 
								self.setItem(m, n, item)
								# item.setEnabled(False)
							continue
						
						else:
							assert 0, "invalid column"

						# make it match the colour used elsewhere in the gui
						# if each_key in color_codes:
						# 	self.item(m, n).setBackground(QtGui.QColor(*color_codes[each_key]))
					
				self.resizeColumnsToContents() # this deals with width of cells 
			
			make_table()		

		def init_appearance():
			self.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
			# layout = QHBoxLayout()
			# layout.addWidget(self.name)
			# self.setLayout(layout)

		def init_behavior():			
			pass

		init_members()
		init_appearance()
		init_behavior()

	def on_button_clicked():
		# make button go grey
		# make it go un-grey when the result is back
		pass
	
	def connect_to_connmanager(self, connmanager):
		self.connmanager = connmanager

	


class register_tab(QWidget):
	def __init__(self):
		super().__init__()

		def init_members():
			self.register_table = register_table()

		def init_appearance():
			layout = QVBoxLayout()
			layout.addWidget(self.register_table)
			self.setLayout(layout)

		def init_behavior():
			pass

		init_members()
		init_appearance()
		init_behavior()

	def connect_to_connmanager(self, connmanager):
		self.register_table.connect_to_connmanager(connmanager)
