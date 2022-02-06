from typing import List

from amaranth import Elaboratable, Module, Signal, Mux, ClockSignal, ResetSignal, Shape
from amaranth.build import Platform
from amaranth.cli import main_parser, main_runner
from amaranth.sim import Simulator, Delay
from amaranth.asserts import Assert, Assume, Cover, Past

from amaranth_boards.ulx3s import ULX3S_85F_Platform

class Blinker(Elaboratable):
	def __init__(self):
		pass

	def elaborate(self, platform: Platform) -> Module:
		m = Module()

		clk_freq = platform.default_clk_frequency

		timer = Signal(
			shape = range(int(clk_freq // 2)),
			reset = int(clk_freq // 2) - 1
		)

		led = platform.request("led").o

		def timer_counts_down_with_rollover():
			with m.If(timer == 0):
				m.d.sync += timer.eq(timer.reset)
			with m.Else():
				m.d.sync += timer.eq(timer - 1)

		def toggle_led_on_timer_rollover():
			with m.If(timer == 0):
				m.d.sync += led.eq(~led)

		timer_counts_down_with_rollover()
		toggle_led_on_timer_rollover()

		return m

	def ports(self) -> List[Signal]:
		return []

if __name__ == "__main__":
	from pathlib import Path
	current_filename = str(Path(__file__).absolute()).split(".py")[0]

	parser = main_parser()
	args = parser.parse_args()

	if args.action == "generate":
		assert 0, "Not implemented"

	elif args.action == "simulate":
		assert 0, "Not implemented"

	else: # upload
		platform = ULX3S_85F_Platform()
		platform.build(Blinker(), do_program=False, # instead, program with the external fujprog tool, because sudo is needed
			build_dir=f"{current_filename}_build")

