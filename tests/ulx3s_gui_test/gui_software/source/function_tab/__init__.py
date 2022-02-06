
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

	def get_test_size(self):
		return int(self.test_size.text())

	def get_fifo_id(self):
		return int(self.fifo_id.text())


class function_tab(QWidget):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		def init_members():
			# self.button_A = QPushButton("Send heartbeat")
			# self.button_B = QPushButton("Read button register")
			self.button_C = QPushButton("Flash LEDs")
			self.control_D = fifo_test_interface()

		def init_appearance():
			layout = QVBoxLayout()
			layout.addWidget(self.button_C)
			layout.addWidget(self.control_D)
			self.setLayout(layout)

		def init_behavior():
			pass

		init_members()
		init_appearance()
		init_behavior()
	
	def connect_to_connmanager(self, connmanager):
		# self.button_A.clicked.connect(lambda : connmanager.send_cmd(
		# 	{
		# 		"request" : "heartbeat"
		# 	}
		# ))

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

		self.control_D.trigger.clicked.connect(lambda : connmanager.send_cmd(
			{
				"request": "test_fifo",
				"fifo_id" : self.control_D.get_fifo_id(),
				"test_size" : self.control_D.get_test_size(),
			}
		))

