# 29jan2022
# getting the workflows set back up
# Goal: A minimal example of how to go from an amaranth design to a gtkwave waveform inspection

from typing import List

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past

# following https://www.youtube.com/watch?v=AQOXoKQhG3I

class Clocky(Elaboratable):
	def __init__(self):
		self.x = Signal(7)
		self.load = Signal()
		self.value = Signal(7)

	def elaborate(self, platform: Platform) -> Module:
		m = Module()

		with m.If(self.load):
			m.d.sync += self.x.eq(Mux(self.value <= 100, self.value, 100))
		with m.Elif(self.x == 100):
			m.d.sync += self.x.eq(0)
		with m.Else():
			m.d.sync += self.x.eq(self.x + 1)
		
		return m

	def ports(self) -> List[Signal]:
		return [self.x, self.load, self.value]

if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	m = Module()
	m.submodules.clocky = clocky = Clocky()

	rst = ResetSignal()
	clk = ClockSignal()

	if args.action == "generate":
		# note - printing doesn't work in 'generate' mode,
		# because stdout is directed to a file in build.sh
		# todo - change this
		import textwrap
		with open(current_filename+".sby", "w") as sby_file:
			file_content = f"""
			[tasks]
			cover
			bmc

			[options]
			bmc: mode bmc
			cover: mode cover
			depth 40 			# new! 40 cycles
			multiclock off

			[engines]
			smtbmc boolector

			[script]
			read_ilang toplevel.il
			prep -top top

			[files]
			toplevel.il
			"""
			# [1:] removes first newline
			sby_file.write(textwrap.dedent(file_content[1:])) 

		def sync_formal_verification():
			def that_clock_increments():
				# and that no value will be loaded in at t=0
				with m.If((clocky.x > 0) & (Past(clocky.load) == 0)):
					m.d.sync += Assert(clocky.x == (Past(clocky.x) + 1)[:7])

			def that_rollover_occurs_at_100():
				# In the case when we may have just rolled over,
				with m.If(clocky.x == 0):
					# and that the clock domain hasn't just been reset
					with m.If(~Past(rst)):
						# and that we haven't just loaded zero 
						with m.If(~Past(clocky.load)):
							# ensure that the previous value was 100
							m.d.sync += Assert(Past(clocky.x) == 100)

			def that_load_works():
				# that the clock domain hasn't just been reset
				with m.If(~Past(rst)):
					with m.If(Past(clocky.load)):
						m.d.sync += Assert(clocky.x == Mux(
							Past(clocky.value)<=100,
							Past(clocky.value),
							100))

			def cover__can_clock_increment_to_3():
				# that the clock domain hasn't just been reset
				with m.If(~Past(rst)):
					# and that we haven't just loaded anything 
					with m.If(~Past(clocky.load)):
						# Can x get to 3, where the previous step is not a load?
						m.d.sync += Cover(clocky.x == 3) # cover: find inputs such that this is true. Note the 'depth 40' in the .sby file relates to the cycles requiried to get to this point
					

			def expected_behavior():
				""" this is too big to understand! 
				do smaller bits, like above """
				# if a reset just happened
				with m.If(Past(rst)):
					m.d.sync += Assert(clocky.x == 0)

				# not in reset,
				# normal clock operation
				with m.Else():
					# if a new value was loaded
					with m.If(Past(clocky.load)):

						# if the new value is less than the max
						next_value = Past(clocky.value)
						with m.If(next_value <= 0x64):
							m.d.sync += Assert(clocky.x == next_value)

						# otherwise this is what it should default to
						with m.Else():
							m.d.sync += Assert(clocky.x == 0x64)

					# normal clock incrementing operation
					with m.Else():

						# make sure incrementing works
						with m.If(Past(clocky.x) < 0x64):
							m.d.sync += Assert(clocky.x == (Past(clocky.x) + 1)[:7]) 
						
						# make sure the clock rolls over properly at 0x64 (100)
						with m.If(Past(clocky.x) == 0x64):
							m.d.sync += Assert(clocky.x == 0)

			expected_behavior()

			that_clock_increments()
			that_rollover_occurs_at_100()
			that_load_works()
			cover__can_clock_increment_to_3()

		sync_formal_verification() # formal verification with a clock domain

		# main_runner is only useful for outputting code to run through yosys
		# we don't use this when we use amaranth's native simulator
		main_runner(parser, args, m, ports=[] + clocky.ports())

	elif args.action == "simulate":
		sim = Simulator(m)
		sim.add_clock(1e-6)

		# Yield will cause one clock cycle to go by
		# Yield with a statement will 'execute the statement right away'
		def process():
			yield
			yield clocky.load.eq(1)    
			yield clocky.value.eq(95)
			yield 
			yield clocky.load.eq(0)
			yield
			yield
			yield clocky.load.eq(1)
			yield
			yield
			yield

		sim.add_sync_process(process) # for clocked processes
		
		with sim.write_vcd(
			f"{current_filename}_simulate.vcd",
			f"{current_filename}_simulate.gtkw", 
			traces=[] + clocky.ports()): # todo - how to add clk, reset signals?

			sim.run()
