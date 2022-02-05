# https://godatadriven.com/blog/a-practical-guide-to-using-setup-py/

from setuptools import setup, find_packages

setup(
	name='amaram',
	version='0.0.1',
	packages=find_packages(include=['amaram']),
	# install_requires=[
	# 	"amaranth",
	# ],
	#  entry_points = {
	# 	'console_scripts': ['lxdev=lxdev.standalone_cli:main'],
	# }
)
