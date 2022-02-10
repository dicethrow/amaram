
from PyQt5 import QtCore, QtGui, QtWidgets, QtOpenGL
from PyQt5.QtCore import (
	Qt, pyqtSlot, pyqtSignal, QThread, QPoint, QUrl, QSize, QModelIndex, 
	QBuffer,QIODevice,
	QDir)
from PyQt5.QtWidgets import (
	QWidget, QFrame, QTextEdit, QPushButton, QPlainTextEdit, QSplitter, QLabel,
	QTreeView, QTabWidget, QFrame, QFileSystemModel,
	QFileDialog, QStyle, QStatusBar,
	QSlider, QLineEdit,
	QVBoxLayout, QHBoxLayout,
	QSizePolicy,
	QApplication, QMainWindow)

import sys, os, json
sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
import test_common

from amlib.debug.ila import ILAFrontend
from . import ila_to_gtkwave 

class labelled_number_entry(QWidget):
	def __init__(self, label, default_content = None):
		super().__init__()
		self.default_value = default_content
		H_layout = QHBoxLayout()
		H_layout.addWidget(QLabel(label))
		self.textbox = QLineEdit(str(default_content))
		self.textbox.setValidator(QIntValidator())
		H_layout.addWidget(self.textbox)
		self.setLayout(H_layout)

class fifo_test_interface(QWidget):
	response_signal = pyqtSignal(dict)
	trigger_signal = pyqtSignal() # so this class is the 'sender' in .sender()
	def __init__(self):
		super().__init__()
		self.fifo_id = QLineEdit(str(0))
		self.fifo_id.setValidator(QtGui.QIntValidator())

		self.test_size = QLineEdit(str(10))
		self.test_size.setValidator(QtGui.QIntValidator())

		self.trigger = QPushButton("Start")

		layout = QHBoxLayout()
		for widget in [QLabel("Test fifo "), self.fifo_id, QLabel(", with test size of "), self.test_size, self.trigger]:
			layout.addWidget(widget)
		
		self.setLayout(layout)

		self.trigger.clicked.connect(self.trigger_signal.emit)
		self.trigger_signal.connect(self.send_request)

		self.response_signal.connect(self.recieve_result)

	def connect_to_connmanager(self, connmanager):
		self.connmanager = connmanager

	def get_test_size(self):
		return int(self.test_size.text())

	def get_fifo_id(self):
		return int(self.fifo_id.text())
	
	def send_request(self):
		self.connmanager.send_cmd({
			"request": "test_fifo",
			"fifo_id" : self.get_fifo_id(),
			"test_size" : self.get_test_size(),
		})

	def recieve_result(self, cipo_data):
		def get_binary_string_at_least_n_bits_long(int_value, n=32):
			format_str = '{:0' + str(n) + 'b}'
			return "0b" + format_str.format(int_value)

		# put the packets together
		complete_data = []
		next_packet_index = -1
		for each_packet in cipo_data["response"]:
			for packet_index, packet_data in each_packet.items():
				next_packet_index += 1
				assert (int(packet_index) == next_packet_index), f"Error: {packet_index} != {next_packet_index}, from {each_packet}"
				complete_data = complete_data + packet_data

		# print it so we can copy/paste then do offline tests with it
		print("Complete data is")
		print(complete_data)
	
		# now inspect it
		for i, word in enumerate(complete_data):
			print(i, get_binary_string_at_least_n_bits_long(word))

		# now try to view it in gtkwave
		ila = ila_to_gtkwave.myILAFrontend(complete_data)
		ila.interactive_display()

class function_tab(QWidget):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		def init_members():
			self.button_A = QPushButton("Test fake fifo to gtkwave")
			# self.button_B = QPushButton("Read button register")
			self.button_C = QPushButton("Flash LEDs")
			self.control_D = fifo_test_interface()

		def init_appearance():
			layout = QVBoxLayout()
			layout.addWidget(self.button_C)
			layout.addWidget(self.control_D)
			layout.addWidget(self.button_A)
			self.setLayout(layout)

		def init_behavior():
			self.button_A.clicked.connect(lambda s : self.test_gtkwave())


		init_members()
		init_appearance()
		init_behavior()

	def test_gtkwave(self):
		# test the gtkwave viewer thing
		fake_data = [855850596, 3003334245, 855850597, 3003334246, 855850598, 3003334247, 855850599, 3003334248, 855850600, 3003334249, 855850601, 3003334250, 855850602, 3003334251, 855850603, 3003334252, 855850604, 3003334253, 855850605, 3003334254, 855850606, 3003334255, 855850607, 3003334256, 855850608, 3003334257, 855850609, 3003334258, 855850610, 3003334259, 855850611, 3003334260, 855850596, 3003334245, 855850597, 3003334246, 855850598, 3003334247, 855850599, 3003334248, 855850600, 3003334249, 855850601, 3003334250, 855850602, 3003334251, 855850603, 3003334252, 855850604, 3003334253, 855850605, 3003334254, 855850606, 3003334255, 855850607, 3003334256, 855850608, 3003334257, 855850609, 3003334258, 855850610, 3003334259, 855850611, 3003334260, 855850596, 3003334245, 855850597, 3003334246, 855850598, 3003334247, 855850599, 3003334248, 855850600, 3003334249, 855850601, 3003334250, 855850602, 3003334251, 855850603, 3003334252, 855850604, 3003334253, 855850605, 3003334254, 855850606, 3003334255, 855850607, 3003334256, 855850608, 3003334257, 855850609, 3003334258, 855850610, 3003334259, 855850611, 3003334260, 855850596, 3003334245, 855850597, 3003334246]

		# now try to view it in gtkwave
		ila = ila_to_gtkwave.myILAFrontend(fake_data)
		ila.interactive_display()

	
	def connect_to_connmanager(self, connmanager):
		

		# self.button_B.clicked.connect(lambda : connmanager.send_cmd(
		# 	{
		# 		"request": "read_register",
		# 		"reg_address" : register_addresses.REG_BUTTONS
		# 	}
		# ))

		self.button_C.clicked.connect(lambda : connmanager.send_cmd(
			{
				"request": "flash_leds"
			}
		))

		self.control_D.connect_to_connmanager(connmanager)

