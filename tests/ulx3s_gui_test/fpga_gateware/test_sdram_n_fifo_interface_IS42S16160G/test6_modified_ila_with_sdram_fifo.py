import sys, os
from termcolor import cprint

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ClockDomain, ResetSignal, Cat, Const
from amaranth.hdl.ast import Rose, Stable, Fell, Past
from amaranth.hdl.mem import Memory
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay, Tick, Passive, Active
from amaranth.asserts import Assert, Assume, Cover, Past
from amaranth.lib.fifo import AsyncFIFOBuffered
#from amaranth.lib.cdc import AsyncFFSynchronizer

from amlib.io import SPIRegisterInterface, SPIDeviceBus, SPIMultiplexer
from amlib.debug.ila import SyncSerialILA
# from amlib.utils.cdc import synchronize
from amaranth.lib.cdc import FFSynchronizer

import amaram
from amaram.sdram_n_fifo_interface_IS42S16160G import sdram_controller

sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
from test_common import fpga_gui_interface, fpga_mcu_interface
addrs = fpga_mcu_interface.register_addresses


# now make the async fifo interface for the sdram... here's the real testing



class IntegratedLogicAnalyzer_with_FIFO(Elaboratable):
	""" Super-simple integrated-logic-analyzer generator class for LUNA.

	Attributes
	----------
	enable: Signal(), input
		This input is only available if `with_enable` is True.
		Only samples with enable high will be captured.
	trigger: Signal(), input
		A strobe that determines when we should start sampling.
		Note that the sample at the same cycle as the trigger will
		be the first sample to be captured.
	capturing: Signal(), output
		Indicates that the trigger has occurred and sample memory
		is not yet full
	sampling: Signal(), output
		Indicates when data is being written into ILA memory

	complete: Signal(), output
		Indicates when sampling is complete and ready to be read.

	captured_sample_number: Signal(), input
		Selects which sample the ILA will output. Effectively the address for the ILA's
		sample buffer.
	captured_sample: Signal(), output
		The sample corresponding to the relevant sample number.
		Can be broken apart by using Cat(*signals).

	Parameters
	----------
	signals: iterable of Signals
		An iterable of signals that should be captured by the ILA.
	sample_depth: int
		The depth of the desired buffer, in samples.

	domain: string
		The clock domain in which the ILA should operate.
	sample_rate: float
		Cosmetic indication of the sample rate. Used to format output.
	samples_pretrigger: int
		The number of our samples which should be captured _before_ the trigger.
		This also can act like an implicit synchronizer; so asynchronous inputs
		are allowed if this number is >= 1. Note that the trigger strobe is read
		on the rising edge of the clock.
	with_enable: bool
		This provides an 'enable' signal.
		Only samples with enable high will be captured.
	"""

	def __init__(self, *, signals, sample_depth, domain="sync", sample_rate=60e6, samples_pretrigger=1, with_enable=False):
		self.domain             = domain
		self.signals            = signals
		self.inputs             = Cat(*signals)
		self.sample_width       = len(self.inputs)
		self.sample_depth       = sample_depth
		self.samples_pretrigger = samples_pretrigger
		self.sample_rate        = sample_rate
		self.sample_period      = 1 / sample_rate

		#
		# Create a backing store for our samples.
		#
		self.mem = Memory(width=self.sample_width, depth=sample_depth, name="ila_buffer")


		#
		# I/O port
		#
		self.with_enable = with_enable
		if with_enable:
			self.enable = Signal()

		self.trigger   = Signal()
		self.capturing = Signal()
		self.sampling  = Signal()
		self.complete  = Signal()

		self.captured_sample_number = Signal(range(0, self.sample_depth))
		self.captured_sample        = Signal(self.sample_width)


	def elaborate(self, platform):
		m  = Module()
		with_enable = self.with_enable

		# Memory ports.
		write_port = self.mem.write_port()
		read_port  = self.mem.read_port(domain='comb')
		m.submodules += [write_port, read_port]

		# If necessary, create synchronized versions of the relevant signals.
		if self.samples_pretrigger >= 1:
			synced_inputs  = Signal.like(self.inputs)
			delayed_inputs = Signal.like(self.inputs)

			# the first stage captures the trigger
			# the second stage the first pretrigger sample
			m.submodules.pretrigger_samples = \
				FFSynchronizer(self.inputs,  synced_inputs)
			if with_enable:
				synced_enable  = Signal()
				m.submodules.pretrigger_enable = \
					FFSynchronizer(self.enable, synced_enable)

			if self.samples_pretrigger == 1:
				m.d.comb += delayed_inputs.eq(synced_inputs)
				if with_enable:
					delayed_enable = Signal()
					m.d.comb += delayed_enable.eq(synced_enable)
			else: # samples_pretrigger >= 2
				capture_fifo_width = self.sample_width
				if with_enable:
					capture_fifo_width += 1

				pretrigger_fill_counter = Signal(range(self.samples_pretrigger * 2))
				pretrigger_filled       = Signal()
				m.d.comb += pretrigger_filled.eq(pretrigger_fill_counter >= (self.samples_pretrigger - 1))

				# fill up pretrigger FIFO with the number of pretrigger samples
				if (not with_enable):
					synced_enable = 1
				with m.If(synced_enable & ~pretrigger_filled):
					m.d.sync += pretrigger_fill_counter.eq(pretrigger_fill_counter + 1)

				m.submodules.pretrigger_fifo = pretrigger_fifo =  \
					DomainRenamer(self.domain)(SyncFIFOBuffered(width=capture_fifo_width, depth=self.samples_pretrigger + 1))

				m.d.comb += [
					pretrigger_fifo.w_data.eq(synced_inputs),
					# We only want to capture enabled samples
					# in the pretrigger period.
					# Since we also capture the enable signal,
					# we capture unconditionally after the pretrigger FIFO
					# has been filled
					pretrigger_fifo.w_en.eq(Mux(pretrigger_filled, 1, synced_enable)),
					# buffer the specified number of pretrigger samples
					pretrigger_fifo.r_en.eq(pretrigger_filled),

					delayed_inputs.eq(pretrigger_fifo.r_data),
				]

				if with_enable:
					delayed_enable = Signal()
					m.d.comb += [
						pretrigger_fifo.w_data[-1].eq(synced_enable),
						delayed_enable.eq(pretrigger_fifo.r_data[-1]),
					]

		else:
			delayed_inputs = Signal.like(self.inputs)
			m.d.sync += delayed_inputs.eq(self.inputs)
			if with_enable:
				delayed_enable = Signal()
				m.d.sync += delayed_enable.eq(self.enable)

		# Counter that keeps track of our write position.
		write_position = Signal(range(0, self.sample_depth))

		# Set up our write port to capture the input signals,
		# and our read port to provide the output.

		m.submodules.fifo = fifo = AsyncFIFOBuffered(
			width=self.sample_width,
			depth=self.sample_depth,
			r_domain="sync", 
			w_domain="sync")

		# Don't sample unless our FSM asserts our sample signal explicitly.
		sampling = Signal()
		m.d.comb += [
			write_port.en.eq(sampling),
			self.sampling.eq(sampling),
		]

		with m.FSM(name="ila_fifo_fsm") as fsm:
			m.d.comb += self.capturing.eq(fsm.ongoing("CAPTURE"))

			# IDLE: wait for the trigger strobe
			with m.State('IDLE'):
				m.d.comb += sampling.eq(0)

				# flush out the fifo if it's not empty
				with m.If(fifo.r_rdy):
					m.d.sync += fifo.r_en.eq(1)

				with m.If(self.trigger):
					with m.If(fifo.w_rdy):
						m.next = 'CAPTURE'

					# Prepare to capture the first sample
					m.d.sync += [
						write_position .eq(0),
						self.complete  .eq(0),
					]

			with m.State('CAPTURE'):
				enabled = delayed_enable if with_enable else 1
				m.d.comb += sampling.eq(enabled)

				with m.If(sampling):

					# How many clocks since the first sampled period does the level increase?
					# this may change based on what type of fifo we're using, obtained from simulation traces
					fifo_level_delay = 2
					
					# keep external interfaces/applications simple by not writing more samples than we have requested
					with m.If( fifo.w_rdy & ((fifo.w_level + fifo_level_delay) <= self.sample_depth)):
						m.d.sync += [
							fifo.w_en.eq(1),
							fifo.w_data.eq(delayed_inputs)
						]

					with m.Elif(fifo.r_rdy):
						m.d.sync += [
							fifo.w_en.eq(0),
							self.captured_sample.eq(fifo.r_data), # to propagate the first sample to the output
							self.complete.eq(1)
						]
						m.next = "READABLE"

					with m.Else():  # something has gone wrong - todo: handle this better, e.g. empty the fifo
						m.next = "IDLE"
						m.d.sync += [
							self.captured_sample.eq(0xBEEEBEEE) # to indicate it's invalid data
						]
						
			
			with m.State('READABLE'):
			
				with m.If(fifo.r_rdy):

					with m.If((Past(self.captured_sample_number) + 1) == self.captured_sample_number):
						m.d.sync += [
							fifo.r_en.eq(1),
							self.captured_sample.eq(fifo.r_data)
						]
					with m.Else():
						m.d.sync += [
							fifo.r_en.eq(0),
						]

				with m.Else():
					m.next = "IDLE"
					m.d.sync += [
						self.captured_sample.eq(0xDEADBEEF) # to indicate it's invalid data
					]

		# Convert our sync domain to the domain requested by the user, if necessary.
		if self.domain != "sync":
			m = DomainRenamer({"sync": self.domain})(m)

		return m

class SyncSerialILA_with_FIFO(SyncSerialILA):
	""" 
	make a subclass where we override its self.ila. 
	This seems to require pretty much require redoing
	the __init__ function, where we specify, and then refer to,
	out new self.ila.
	"""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		# signals, sample_depth, clock_polarity=clock_polarity, clock_phase=clock_phase, cs_idles_high=cs_idles_high, 

		#
		# I/O port
		#
		self.spi = SPIDeviceBus()

		#
		# Init
		#

		self.clock_phase = kwargs.pop("clock_phase") #clock_phase
		self.clock_polarity = kwargs.pop("clock_polarity") #clock_polarity
		# now replace the default ila class with out fifo class

		# Extract the domain from our keyword arguments, and then translate it to sync
		# before we pass it back below. We'll use a DomainRenamer at the boundary to
		# handle non-sync domains.
		self.domain = kwargs.get('domain', 'sync')
		kwargs['domain'] = 'sync'

		# Create our core integrated logic analyzer.
		self.ila = IntegratedLogicAnalyzer_with_FIFO(**kwargs)

		# Copy some core parameters from our inner ILA.
		self.signals       = kwargs.get("signals") #signals
		self.sample_width  = self.ila.sample_width
		self.sample_depth  = self.ila.sample_depth
		self.sample_rate   = self.ila.sample_rate
		self.sample_period = self.ila.sample_period

		if kwargs.get('with_enable'):
			self.enable = self.ila.enable

		# Figure out how many bytes we'll send per sample.
		# We'll always send things squished into 32-bit chunks, as this is what the SPI engine
		# on our Debug Controller likes most.
		words_per_sample = (self.ila.sample_width + 31) // 32

		# Bolster our bits_per_word up to a power of two...
		self.bits_per_sample = words_per_sample * 4 * 8
		self.bits_per_sample = 2 ** ((self.bits_per_sample - 1).bit_length())

		# ... and compute how many bits should be used.
		self.bytes_per_sample = self.bits_per_sample // 8

		# Expose our ILA's trigger and status ports directly.
		self.trigger   = self.ila.trigger
		self.capturing = self.ila.capturing
		self.sampling  = self.ila.sampling
		self.complete  = self.ila.complete


class dram_ulx3s_upload_test_IS42S16160G(Elaboratable):
	def __init__(self, copi, cipo, sclk, i_buttons, leds,  csn=None, cs=None):
		# external spi interface
		self.copi = copi 
		self.cipo = cipo
		self.sclk = sclk

		if type(cs) != type(None):
			self.invert_csn = False
			self.cs = cs
		else:
			self.invert_csn = True
			self.cs = Signal()			
		self.csn = csn
		
		self.i_buttons = i_buttons
		self.leds = leds

		
	def elaborate(self, platform = None):
		self.m = Module()

		def handle_cs_or_csn():
			# to deal with the inverted cs pin on the ulx3s, but not in simulation
			if self.invert_csn:
				self.m.d.comb += self.cs.eq(~self.csn)

		def add_register_interface():
			# Create a set of registers...
			self.spi_registers = SPIRegisterInterface(
				address_size=fpga_mcu_interface.spi_register_interface.CMD_ADDR_BITS, # and first bit for write or not
				register_size=fpga_mcu_interface.spi_register_interface.REG_DATA_BITS, # to match the desired fifo width for later on
			)
			self.m.submodules += self.spi_registers

			# Add a simple ID register to demonstrate our registers.
			# self.spi_registers.add_read_only_register(REGISTER_ID, read=0xDEADBEEF)
			addrs = fpga_mcu_interface.register_addresses
			self.spi_registers.add_read_only_register(address=addrs.REG_BUTTONS_R, read=Cat(self.i_buttons["fireA"], self.i_buttons["fireB"])) # buttons
		
			# add fifo to test, see if the difficulties from earlier were due to not being synchronised
			self.spi_registers.m.submodules.test_fifo0 = test_fifo0 = AsyncFIFOBuffered(width=16, depth=20, r_domain="sync", w_domain="sync")
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READ_R,		value_signal=test_fifo0.r_data,	read_strobe=test_fifo0.r_en)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READRDY_R,	value_signal=test_fifo0.r_rdy)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_READLVL_R,	value_signal=test_fifo0.r_level)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITE_W,		value_signal=test_fifo0.w_data,	write_strobe=test_fifo0.w_en)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITERDY_R,	value_signal=test_fifo0.w_rdy)
			self.spi_registers.add_register(address=addrs.REG_FIFO0_WRITELVL_R,	value_signal=test_fifo0.w_level)

		def add_ila():
			self.ila_signals = fpga_gui_interface.get_ila_signals_dict()
			self.ila = SyncSerialILA_with_FIFO(
				**fpga_gui_interface.get_ila_constructor_kwargs(),
				clock_polarity=1, clock_phase=1 
			)
			
			self.m.submodules += self.ila

			# connect leds to show some feedback about when the ila is triggered
			if False: # leds to test/show register io
				self.spi_registers.add_register(address=addrs.REG_LEDS_RW, value_signal=self.leds)
			else: # leds to count complete flag raises
				with self.m.If(Rose(self.ila.complete)):
					self.m.d.sync += self.leds.eq(self.leds + 1)

			
			# Create a simple SFR that will trigger an ILA capture when written,
			# and which will display our sample status read.
			self.spi_registers.add_sfr(addrs.REG_ILA_TRIG_RW,
				read=self.ila.complete,
				write_strobe=self.ila.trigger
			)
		
		def route_spi_signals():
			# inspired by the ilaSharedBusExample from LUNA
			self.board_spi = SPIDeviceBus()
			ila_spi = SPIDeviceBus()
			reg_spi = SPIDeviceBus()

			# between fpga_pin --- FFsynchroniser --- spi_multiplexer
			self.m.submodules += FFSynchronizer(o=self.board_spi.sdi, i=self.copi)
			self.m.d.comb += self.cipo.eq(self.board_spi.sdo) # ah! no need for synchronisation for sdo
			self.m.submodules += FFSynchronizer(o=self.board_spi.sck, i=self.sclk)
			self.m.submodules += FFSynchronizer(o=self.board_spi.cs, i= self.cs)
			# Multiplex our ILA and register SPI busses.
			self.m.submodules.mux = SPIMultiplexer([ila_spi, reg_spi])
			self.m.d.comb += self.m.submodules.mux.shared_lines.connect(self.board_spi)

			# between spi_multiplexer --- spi_ila
			self.m.d.comb += [
				self.ila.spi .connect(ila_spi),

				# For sharing, we'll connect the _inverse_ of the primary
				# chip select to our ILA bus. This will allow us to send
				# ILA data when CS is un-asserted, and register data when
				# CS is asserted.
				ila_spi.cs  .eq(~self.board_spi.cs)
			]

			# between spi_multiplexer --- spi_register_interface
			self.m.d.comb += [
				# self.spi_registers.spi .connect(reg_spi),
				self.spi_registers.spi.sck.eq(reg_spi.sck),
				self.spi_registers.spi.cs.eq(reg_spi.cs),
				self.spi_registers.spi.sdi.eq(reg_spi.sdi),

				# use straight cs here
				reg_spi.cs        .eq(self.board_spi.cs)
			]
			# note that it seems we need to delay the sdo by one sclk cycle...
			last_sdo = Signal()
			with self.m.If(Rose(reg_spi.sck)): # then the value we read now, we set on the next falling edge
				self.m.d.sync += last_sdo.eq(self.spi_registers.spi.sdo)
			with self.m.Elif(Fell(reg_spi.sck)): # set it on the falling edge
				self.m.d.sync += reg_spi.sdo.eq(last_sdo)

		def add_signals_to_ila():
			# watch spi signals?
			if True:
				# Clock divider / counter.
				with self.m.If(self.ila.complete):
					self.m.d.sync += self.ila_signals["counter"].eq(0)
				self.m.d.sync += self.ila_signals["counter"].eq(self.ila_signals["counter"] + 1)
			else:
				# test with a constant, known value
				self.m.d.sync += self.ila_signals["counter"].eq(0xF0FF0FFF)

			# Another example signal, for variety.
			if False: #not in use presently
				self.m.d.sync += self.ila_signals["toggle"].eq(~self.ila_signals["toggle"])


		
		handle_cs_or_csn()
		add_register_interface()
		add_ila()
		route_spi_signals()
		add_signals_to_ila()

		return self.m



if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	m = Module()

	# if args.action in ["generate", "simulate"]:
	# 	m.submodules.dram_testdriver = dram_testdriver = dram_testdriver()

	if args.action == "generate":
		pass

	elif args.action == "simulate":

		sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/fpga_gateware"))
		import fpga_io_sim

		# # PLL - 143MHz for sdram 
		# sdram_freq = int(143e6)

		# from simulation_test import dram_sim_model_IS42S16160G

		# #m.submodules.dram_testdriver = ram_testdriver = dram_testdriver()
		# m.submodules.m_sdram_controller = m_sdram_controller = sdram_controller()
		# m.submodules.m_dram_model = m_dram_model = dram_sim_model_IS42S16160G(m_sdram_controller, sdram_freq)		


		tb_copi = Signal()
		tb_cipo = Signal()
		tb_sclk = Signal()
		tb_csn = Signal()

		tb_buttons = {
			"fireA" : Signal(),
			"fireB" : Signal()
		}
		tb_leds = Signal(8)

		placeholder_signal = Signal()
		m.d.sync += [
			placeholder_signal.eq(~placeholder_signal)
		]


		m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
			copi = tb_copi, cipo = tb_cipo, sclk = tb_sclk, csn = tb_csn,
			i_buttons = tb_buttons, leds = tb_leds
		)

		addrs = fpga_mcu_interface.register_addresses

		def spi_tests():
			yield Active()
			# yield tb_buttons["fireB"].eq(0b1) # lets see if we can read this

			spi_freq = 1e6
			spi_clk_period = 1/spi_freq

			yield Delay(spi_clk_period)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW, True, 0xABCD)
			yield Delay(spi_clk_period)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_LEDS_RW)
			

			yield Delay(spi_clk_period)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW)
			yield Delay(spi_clk_period)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW, True, 0xABCD)
			yield Delay(spi_clk_period)
			yield from fpga_io_sim.reg_io(dut, addrs.REG_ILA_TRIG_RW)

			yield Delay(spi_clk_period)
			yield from fpga_io_sim.alt_fifo_io(dut, read_num=fpga_gui_interface.get_ila_constructor_kwargs()["sample_depth"])

			# just to add a bit of time at the end
			yield Delay(20 * spi_clk_period)


		sim = Simulator(m)
		sim.add_clock(1/25e6, domain="sync")

		sim.add_process(spi_tests)

		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw", 
			traces=[]): # todo - how to add clk, reset signals?

			sim.run()

	else: # upload - is there a test we could upload and do on the ulx3s?
		from amaranth.build import Platform, Resource, Subsignal, Pins, PinsN, Attrs
		from amaranth_boards.ulx3s import ULX3S_85F_Platform

		# from 

		class top(Elaboratable):
			def elaborate(self, platform):
				leds = Cat([platform.request("led", i) for i in range(8)])
				esp32 = platform.request("esp32_spi")
				io_uart = platform.request("uart")
				i_buttons = {
					"pwr" : platform.request("button_pwr", 0),
					"fireA" : platform.request("button_fire", 0),
					"fireB" : platform.request("button_fire", 1),
					"up" : platform.request("button_up", 0),
					"down" : platform.request("button_down", 0),
					"left" : platform.request("button_left", 0),
					"right" : platform.request("button_right", 0)
				}

				m = Module()

				# ### set up the SPI register test interface
				m.submodules.dut = dut = dram_ulx3s_upload_test_IS42S16160G(
					copi = esp32.gpio4_copi, 
					cipo = esp32.gpio12_cipo, 
					sclk = esp32.gpio16_sclk, 
					cs = esp32.gpio5_cs,

					i_buttons = i_buttons, 
					leds = leds
				)
			

				# external logic analyser, if desired
				if False:
					o_digital_discovery = platform.request("digital_discovery")
					m.d.comb += [
						o_digital_discovery.bus[0].eq(esp32.gpio5_cs), 	# cs
						o_digital_discovery.bus[1].eq(esp32.gpio16_sclk),	# clk
						o_digital_discovery.bus[2].eq(esp32.gpio4_copi),	# mosi
						o_digital_discovery.bus[3].eq(esp32.gpio12_cipo)	# miso
					]

				######## setup esp32 interaction ######

				# route the esp32's uart
				m.d.comb += [
					esp32.tx.eq(io_uart.rx),
					io_uart.tx.eq(esp32.rx),
				]

				# implement the esp32's reset/boot requirements
				with m.If((io_uart.dtr.i == 1) & (io_uart.rts.i == 1)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"]) 
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 0)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 1) & (io_uart.rts == 0)):
					m.d.comb += esp32.en.eq(0 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(1)
				with m.Elif((io_uart.dtr == 0) & (io_uart.rts == 1)):
					m.d.comb += esp32.en.eq(1 & ~i_buttons["pwr"])
					m.d.comb += esp32.gpio0.o.eq(0)

				return m

		# ESP-32 connections
		esp32_spi = [
			Resource("esp32_spi", 0,
				Subsignal("en",     Pins("F1", dir="o"), Attrs(PULLMODE="UP")),
				Subsignal("tx",     Pins("K3", dir="o"), Attrs(PULLMODE="UP")),
				Subsignal("rx",     Pins("K4", dir="i"), Attrs(PULLMODE="UP")),
				Subsignal("gpio0",  Pins("L2"),          Attrs(PULLMODE="UP")),
				Subsignal("gpio4_copi", Pins("H1", dir="i"),  Attrs(PULLMODE="UP")), # SDD1? GPIO4? 
				Subsignal("gpio5_cs",  PinsN("N4", dir="i"),  Attrs(PULLMODE="UP")),
				Subsignal("gpio12_cipo", Pins("K1", dir="o"),  Attrs(PULLMODE="UP")), # SDD2? GPIO12?
				Subsignal("gpio16_sclk", Pins("L1", dir="i"),  Attrs(PULLMODE="UP")),
				Attrs(IO_TYPE="LVCMOS33", DRIVE="4")
			),
		]

		# digital discovery connection, for logic probing
		digital_discovery = [
			Resource("digital_discovery", 0,
				Subsignal("bus", Pins("14- 14+ 15- 15+ 16- 16+ 17- 17+ 18- 18+", dir="o", conn=("gpio", 0)), Attrs(IO_TYPE="LVCMOS25"))
			)
		]

		platform = ULX3S_85F_Platform()
		platform.add_resources(esp32_spi)
		platform.add_resources(digital_discovery)
		platform.build(top(), do_program=False, build_dir=f"{current_filename}_build")

