# import sys, os, json
# sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
# import test_common

# # **test_common.get_ila_constructor_kwargs()



from amaranth import Const, Cat, Signal
from amlib.debug.ila import ILAFrontend
from amlib.utils.bits import bits

import sys, os, json
sys.path.append(os.path.join(os.getcwd(), "tests/ulx3s_gui_test/common"))
import test_common


class myILAFrontend(ILAFrontend):
	""" 
	UART-based ILA transport.

	Parameters
	------------
	data: Array[Int]
		The array of data to view
	"""

	def __init__(self, data):

		class placeholder_ila_class():
			def __init__(self, *, signals, sample_depth, domain="sync", sample_rate=60e6, samples_pretrigger=1):
				self.domain             = domain
				self.signals            = signals
				self.inputs             = Cat(*signals)
				self.sample_width       = len(self.inputs)
				self.sample_depth       = sample_depth
				self.samples_pretrigger = samples_pretrigger
				self.sample_rate        = sample_rate
				self.sample_period      = 1 / sample_rate

				# and to make other needed values,
				self.bits_per_sample = 2 ** ((self.sample_width - 1).bit_length())
				self.bytes_per_sample = self.bits_per_sample // 8

		self.src_data = data
		super().__init__(placeholder_ila_class(**test_common.get_ila_constructor_kwargs()))

	# def _split_samples(self, all_samples):
	# 	""" Returns an iterator that iterates over each sample in the raw binary of samples. """

	# 	sample_width_bytes = self.ila.bytes_per_sample

	# 	# Iterate over each sample, and yield its value as a bits object.
	# 	for i in range(0, len(all_samples), sample_width_bytes):
	# 		raw_sample    = all_samples[i:i + sample_width_bytes]
	# 		sample_length = len(Cat(self.ila.signals))

	# 		yield bits.from_bytes(raw_sample, length=sample_length, byteorder='big')

	def _read_samples(self):
		""" 
		Turns previously read samples into a list of 
		amaranth values.
		"""
		# all_samples = []
		# for int_value in self.src_data:
		# 	all_samples.append(int_value.to_bytes(4, "big"))
		# print(all_samples)
		# return list(self._split_samples(all_samples))
	
		for int_value in self.src_data:
			yield bits.from_int(int_value)

