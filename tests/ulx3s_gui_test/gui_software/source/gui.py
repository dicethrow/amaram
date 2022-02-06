# January 2022

import sys, os

from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
	QWidget, QFrame, QTextEdit, QPushButton, QPlainTextEdit, QSplitter, QLabel,
	QTreeView, QTabWidget, QFrame, QFileSystemModel,
	QFileDialog, QStyle, QStatusBar, QStackedWidget, QComboBox,
	QSlider,
	QVBoxLayout, QHBoxLayout,
	QSizePolicy,
	QApplication, QMainWindow)
from PyQt5.QtGui import QPalette, QColor

from function_tab import function_tab
from register_tab import register_tab

import asyncio, json
from threading import Thread
from termcolor import cprint

sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common import register_addresses
from test_common.mcu_gui_interface import PORT, TIMEOUT

# from iot import server
import server

class connectionManager(QWidget):
	text_to_send = pyqtSignal(list)

	class connectionWorker(QWidget):
		def __init__(self, client_id, text_to_send):
			super().__init__()
			self.text_to_send = text_to_send
			self.unsent_queue = asyncio.Queue()
			self.cipo_queue = asyncio.Queue()
			self.unresponded_queue = asyncio.Queue()
			self.client_id = "1" # only using one device
			self.conn = None  # Connection instance
			self.data = [0, 0, 0]  # Exchange a 3-li
			asyncio.create_task(self.start())

			self.cmd_list = []

		async def start(self):
			print('Client {} Awaiting connection.'.format(self.client_id))
			self.conn = await server.client_conn(self.client_id)
			asyncio.create_task(self.reader())
			asyncio.create_task(self.writer())
			asyncio.create_task(self.response_handler())

		async def reader(self):
			cprint('Started reader', "yellow")
			while True:
				line = await self.conn.readline()  # Pause in event of outage
				cipo_data = json.loads(line)
				await self.cipo_queue.put(cipo_data)

		async def writer(self):
			cprint('Started writer', "yellow")
			while True:		
				next_to_send = await self.unsent_queue.get()

				text = json.dumps(next_to_send["copi_data"])
				await self.conn.write(text)
				self.text_to_send.emit(["sent", text])

				await self.unresponded_queue.put(next_to_send)
		
		async def response_handler(self):

			def is_subdict(small, big):
	  			return dict(big, **small) == big

			cprint("Started response handler", "yellow")
			while True:
				print()
				cipo_data = await self.cipo_queue.get()

				text = f"Got {cipo_data} from remote {self.client_id}"
				# print(text)
				self.text_to_send.emit(["recieved", text])

				cprint(cipo_data, "magenta")

				found = False
				# for i in range(self.unresponded_queue.qsize()):
				while True: # is this while-true bad? I think it just makes clear it could get stuck
					sent_record = await self.unresponded_queue.get()
					cprint(sent_record["copi_data"], "yellow")
					if is_subdict(sent_record["copi_data"], cipo_data):
						found = True
						break
					else:
						await self.unresponded_queue.put(sent_record) # put back to check next time

				if found:
					if hasattr(sent_record["sender"], "recieve_result"):
						# sent_record["sender"].recieve_result(cipo_data)
						sent_record["sender"].response_signal.emit(cipo_data)
						# print(f"Would send result of {cipo_data}")
				else:
					cprint("Unable to find match for copi_data:", "red")
					cprint(cipo_data, "red")

	
	def __init__(self):
		super().__init__()
		self.restart_background_loop()
		self.start()

	# @pyqtSlot()
	def send_cmd(self, cmd):
		# self.apps[0].cmd_list.append({
		# 	"copi_data" : cmd,
		# 	"sender" : self.sender(),
		# 	"status" : "queued_for_sending"
		# })
		cprint(cmd, "green")
		self.apps[0].unsent_queue.put_nowait({ # is this the right func to call?
			"copi_data" : cmd,
			"sender" : self.sender(),
		})

				
	def restart_background_loop(self):
		def start_background_loop(loop: asyncio.AbstractEventLoop) -> None:
			asyncio.set_event_loop(loop)
			loop.run_forever()

		self.loop = asyncio.new_event_loop()
		self.t = Thread(target=start_background_loop, args=(self.loop,), daemon=True)
		self.t.start()
		# from https://gist.github.com/dmfigol/3e7d5b84a16d076df02baa9f53271058

	def start(self):
		async def to_do():
			print("Awaiting connection...")
			clients = {"1"}
			self.apps = []
			for n in clients:
				app = connectionManager.connectionWorker(client_id=n, text_to_send=self.text_to_send)
				self.apps.append(app)
			await server.run(clients, verbose=True, port=PORT, timeout=TIMEOUT)
		asyncio.run_coroutine_threadsafe(to_do(), self.loop)

	


class MainWindow(QMainWindow):
	def __init__(self):
		super().__init__()

		def init_members():
			self.connmanager = connectionManager()
			
			self.console_remote_to_gui = QPlainTextEdit()

			self.ui_tabs = QTabWidget()
			self.register_tab = register_tab()
			self.function_tab = function_tab()

		def init_appearance():
			self.console_remote_to_gui.setReadOnly(True)

			self.ui_tabs.setDocumentMode(True) # cleaner appearance
			self.ui_tabs.setTabPosition(QTabWidget.North)
			self.ui_tabs.setMovable(False)
			self.ui_tabs.addTab(self.register_tab, "Register interface")
			self.ui_tabs.addTab(self.function_tab, "Function interface")

			# be able to rescale/adjust sizes
			splitter = QSplitter(Qt.Horizontal)
			splitter.addWidget(self.ui_tabs)
			# splitter.addWidget(self.console_remote_to_gui)

			# set initial sizes, from https://stackoverflow.com/questions/47788675/setting-initial-size-of-qtabwidget-in-qsplitter-pyqt-application
			# splitter.setSizes(
			# 	[int(splitter.size().height() * 0.6), 
			# 	int(splitter.size().height() * 0.4)])

			self.setCentralWidget(splitter)

		def init_behavior():
			self.connmanager.text_to_send.connect(lambda obj : self.add_to_console(obj))

			self.register_tab.connect_to_connmanager(self.connmanager)
			self.function_tab.connect_to_connmanager(self.connmanager)

		init_members()
		init_appearance()
		init_behavior()

	def closeEvent(self, event):
		""" Using parent class overriding,
		this function will attempt to close the BLE connection
		before the GUI is closed"""
		# from https://stackoverflow.com/questions/9249500/pyside-pyqt-detect-if-user-trying-to-close-window
		# do stuff

		# do something
		event.accept() # then let the window close
	
	@pyqtSlot(list)
	def add_to_console(self, data):
		category, text = data

		### sending

		# if category == "connect":
		# 	colour_str = "orange"

		if category == "sent":
			colour_str = "orange2"

		# elif category == "disconnect":
		# 	colour_str = "orange"

		# elif category == "control":
		# 	colour_str = "blue2"
		
		# elif category == "rx":
		# 	colour_str = "blue2"
		
		# elif category == "exception":
		# 	colour_str = "red"

		# ### recieving

		elif category == "recieved":
			colour_str = "blue"
		
		# elif category == "recieved error":
		# 	colour_str = "red"
		
		# elif category == "recieved info":
		# 	colour_str = "orange"
		
		html_text = self.get_colored_html_text(text, colour_str)

		self.console_remote_to_gui.appendHtml(html_text)
		# if category in ["connect", "listen", "disconnect", "control", "exception", "recieved", "recieved info", "recieved error"]:
			# self.console_output.appendHtml(html_text)
		
		# elif category in ["tx"]:
			# self.console_input.appendHtml(html_text)

	def get_colored_html_text(self, text, colour_str):
		colours = {
			"blue" : "2594cf",
			"blue2" : "15648f",
			"orange" : "f5d90d",
			"orange2" : "b29d0b",
			"red" : "cc4008"
		}

		# implement line breaks in html
		text = text.replace("\n", "<br>")
		text = text.replace("\t", 4*" &nbsp; ")
		
		html_text = f"<span style=\" color:#{colours[colour_str]};\" >{text}</span>"
		return html_text

def set_theme(app, theme_selection):
	# from https://stackoverflow.com/questions/48256772/dark-theme-for-qt-widgets
	if theme_selection == 'Dark':
		app.setStyle("Fusion")
		#
		# # Now use a palette to switch to dark colors:
		dark_palette = QPalette()
		dark_palette.setColor(QPalette.Window, QColor(53, 53, 53))
		dark_palette.setColor(QPalette.WindowText, Qt.white)
		dark_palette.setColor(QPalette.Base, QColor(35, 35, 35))
		dark_palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
		dark_palette.setColor(QPalette.ToolTipBase, QColor(25, 25, 25))
		dark_palette.setColor(QPalette.ToolTipText, Qt.white)
		dark_palette.setColor(QPalette.Text, Qt.white)
		dark_palette.setColor(QPalette.Button, QColor(53, 53, 53))
		dark_palette.setColor(QPalette.ButtonText, Qt.white)
		dark_palette.setColor(QPalette.BrightText, Qt.red)
		dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
		dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
		dark_palette.setColor(QPalette.HighlightedText, QColor(35, 35, 35))
		dark_palette.setColor(QPalette.Active, QPalette.Button, QColor(53, 53, 53))
		dark_palette.setColor(QPalette.Disabled, QPalette.ButtonText, Qt.darkGray)
		dark_palette.setColor(QPalette.Disabled, QPalette.WindowText, Qt.darkGray)
		dark_palette.setColor(QPalette.Disabled, QPalette.Text, Qt.darkGray)
		dark_palette.setColor(QPalette.Disabled, QPalette.Light, QColor(53, 53, 53))
		app.setPalette(dark_palette)
	elif theme_selection == 'Light':
		app.setStyle("")

def start_gui():
	app = QApplication(sys.argv)
	app.setStyle("Fusion")
	window = MainWindow()
	window.setWindowTitle('remote control')
	window.show()
	# set_theme(app, "Dark")
	app.exec_()
