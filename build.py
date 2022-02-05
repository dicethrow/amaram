import argparse, os, importlib, glob, shutil
from termcolor import cprint
import pandown, lxdev
	
def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("topic", type=str, help="microcontroller, fpga, gui, ...?")
	parser.add_argument("task", type=str, help="action to do")
	parser.add_argument('--file', default=None)
	args = parser.parse_args()

	if args.topic == "gui":
		if args.task == "start-gui":
			assert 0, "Neither of these ways work. Use a different approach."
			if False:
				# hacky - import the file we want, which is in the same place as where the rshell source is
				path = os.path.join(os.path.dirname(__file__), "gui_software/source/gui.py")
				spec = importlib.util.spec_from_file_location("pyboard", path)
				gui = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(gui)
				# now it's as if we ran 'import main', which wouldn't be possible in a less hacky way
				gui.start_gui()
			if False:
				lxdev.run_local_cmd("~/Documents/venv_ge/bin/python3.9 gui_software/source/main.py")
		else:
			cprint("Invalid task given, aborting", "red")
			return

	elif args.topic in ["mcu", "fpga"]:

		if args.topic == "mcu":
			remote_interface_to_use = mcu_interface
		elif args.topic == "fpga":
			remote_interface_to_use = fpga_interface
		else:
			assert 0, "Invalid task given, aborting"

		assert args.task in remote_interface_to_use.defined_tasks, "Invalid task given, aborting"
		
		with remote_interface_to_use(
			host = "lxd_fpga-dev-ECP5", 
			lxd_container_name = "fpga-dev-ECP5",
			local_working_directory = os.path.dirname(os.path.realpath(__file__))
			) as ssh_remote_client:

			ssh_remote_client.do_task(args.task, local_filename=args.file)
		
	else:
		cprint("Invalid topic given, aborting", "red")
		return
		
	cprint(f"*** {args.topic} - {args.task} done ***", "green", flush=True)

class fpga_interface(lxdev.RemoteClient):
	defined_tasks = [
		"upload-uart-passthrough-binary",

		"simulate-current-file",
		"generate-current-file",
		"upload-current-file"
	]

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def do_task(self, task, local_filename=None):	

		if task == "upload-uart-passthrough-binary":
			# self2.run_communication_test()
			self.rsync_to_container()
			self.upload_binary("./fpga_gateware/compiled_binaries/ulx3s_85f_passthru.bit")
		
		elif task == "simulate-current-file":
			sim_manager = fpga_interface.simulate_manager(
				fpga_interface=self, local_filename = local_filename)

			self.rsync_to_container()
			self.check_python_venv()
			sim_manager.simulate_file()
			self.rsync_from_container()
			sim_manager.view_simulation_results(on_host_pc=True)
			sim_manager.remove_simulation_results()
		
		elif task == "generate-current-file":
			gen_manager = fpga_interface.generate_manager(
				fpga_interface=self, local_filename = local_filename)

			self.rsync_to_container()
			self.check_python_venv()
			gen_manager.generate_file()
			gen_manager.run_through_symbyosis()
			self.rsync_from_container()
			sby_success, local_sby_trace_file = gen_manager.inspect_symbyosis_results()
			if local_sby_trace_file != None:
				gen_manager.show_sby_vcd_file(local_sby_trace_file, on_host_pc=True)
			gen_manager.remove_generate_results()
		
		elif task == "upload-current-file":
			upl_manager = fpga_interface.upload_manager(
				fpga_interface=self, local_filename=local_filename)
			
			self.rsync_to_container()
			self.check_python_venv()
			upl_manager.build_bitstream()
			self.rsync_from_container()
			upl_manager.upload_bitstream()
			# upl_manager.remove_bitstream_files() # commented out so I can see info about resource usage etc

		else:
			assert 0, "Invalid task given, aborting"

	# def run_communication_test(self):
	# 	cprint("Running communication test to device...", "yellow", flush=True)
	# 	result = self.execute_commands(f"sudo ~/Documents/oss-cad-suite/bin/fujprog", pass_to_stdin=b'\x03\x03')
	# 	# b'\r\x03\x03') # ctrl-c twice, from https://github.com/dhylands/rshell/blob/master/rshell/pyboard.py line 185
		
	# 	assert any("ULX3S" in line for line in result), f"Failed comms test with error of {result}, is the ULX3S board plugged in?"
	# 	cprint(" OK", "green", flush=True)


	def check_python_venv(self):
		cprint("Checking for correct python venv...", "yellow", flush=True)
		result = self.execute_commands("which python3")
		assert any("venv_fpga" in line for line in result), "Desired python venv is not set up"
		cprint(" OK", "green", flush=True)

	def upload_binary(self, binary_filename):
		# todo: add option of writing to flash?
		cprint("Writing binary file to FPGA...", "yellow", flush=True)
		result, error = self.execute_commands(f"sudo ~/Documents/oss-cad-suite/bin/fujprog {binary_filename}", get_stderr=True, within_remote_working_dir=True)
		assert any("Completed" in line for line in result+error), f"Failed fpga upload binary with error of {result},{error}"
		cprint(" OK", "green", flush=True)


	class upload_manager:
		def __init__(self, fpga_interface, local_filename):
			self.fpga_interface = fpga_interface
			self.local_filename = local_filename

			self.rel_remote_filename = self.fpga_interface.get_remote_filename_from_local(self.local_filename, get_as_relative=True)
		
		# def add_build_tools_to_path(self):
		# # for the ecp5 tools from oss toolchain https://github.com/YosysHQ/oss-cad-suite-build
		#source ~/Documents/oss-cad-suite/environment

		def build_bitstream(self):
			cprint(f"build bitstream of {self.rel_remote_filename}...", "yellow")
			result, error = self.fpga_interface.execute_commands(
				[
					# f"cd {self.fpga_interface.get_remote_filename_from_local(os.path.dirname(self.rel_remote_filename))}",
					"PATH=$PATH:~/Documents/oss-cad-suite/bin", # for yosys, and any others?
					f"python3 {self.rel_remote_filename}", # defaults to upload if no arg passed
				],
				within_remote_working_dir=True,
				get_stderr=True) 
			assert not any("failed" in line for line in result+error), f"Failed fpga upload binary with error of {result},{error}"
			assert not any("Traceback" in line for line in result+error), f"Failed fpga upload binary with error of {result},{error}"

			cprint(" OK", "green", flush=True)
		
		def upload_bitstream(self):
			bitstream_filename = f'{self.rel_remote_filename.replace(".py", "_build")}/top.bit'
			cprint(f"uploading bitstream of {bitstream_filename}...", "yellow")
			self.fpga_interface.upload_binary(bitstream_filename)
			cprint(" OK", "green", flush=True)
		
		def remove_bitstream_files(self):
			local_bitstream_dir = self.rel_remote_filename.replace(".py", "_build")
			cprint(f"remove bitstream files of {local_bitstream_dir}...", "yellow")
			shutil.rmtree(local_bitstream_dir)
			cprint(" OK", "green", flush=True)

	class simulate_manager:
		def __init__(self, fpga_interface, local_filename):
			self.fpga_interface = fpga_interface
			self.local_filename = local_filename

			self.rel_remote_filename = self.fpga_interface.get_remote_filename_from_local(self.local_filename, get_as_relative=True)
			
		def simulate_file(self):			
			cprint(f"simulate of {self.rel_remote_filename}...", "yellow")
			result, error = self.fpga_interface.execute_commands(f"python3 {self.rel_remote_filename} simulate -c 10", get_stderr=True, within_remote_working_dir=True)
			assert not any("Traceback" in line for line in result+error), f"Failed fpga simulate with error of {result},{error}"
			assert not any("[Errno" in line for line in result+error), f"Failed fpga simulate with error of {result},{error}"
			cprint(" OK", "green", flush=True)

		def view_simulation_results(self, on_host_pc):
			def make_gtkw_file_refer_to_relative_dumpfile(gtkw_filename):
				# from https://www.geeksforgeeks.org/python-program-to-replace-specific-line-in-file/
				with open(gtkw_filename, 'r', encoding='utf-8') as file:
					lines = file.readlines()
				
				# print(data)
				changed = False
				for i, line in enumerate(lines):
					if "[dumpfile]" in line:
						print(line)
						print(self.fpga_interface.remote_working_directory)
						lines[i] = lines[i].replace(self.fpga_interface.remote_working_directory, ".")
						changed = True
						break
				assert changed, "Path was not found, so it could not be made relative"
				
				with open(gtkw_filename, 'w', encoding='utf-8') as file:
					file.writelines(lines)

			if on_host_pc:
				gtkw_filename = self.local_filename.replace('.py', '_simulate.gtkw')
				make_gtkw_file_refer_to_relative_dumpfile(gtkw_filename)
				cmd = f"gtkwave {gtkw_filename} --rcvar 'do_initial_zoom_fit yes'"
				
				lxdev.run_local_gui_cmd(cmd)

			else:
				# note! the .gtkw doesn't work, but the .vcd does. The error seems similar to the one for local display, but is not fixed yet.
				cmd = f"~/Documents/oss-cad-suite/bin/gtkwave {self.fpga_interface.remote_working_directory}/{self.rel_remote_filename.replace('.py', '_simulate.vcd')}  --rcvar 'do_initial_zoom_fit yes'"
				lxdev.run_local_gui_cmd(f"ssh -X lxd_fpga-dev-ECP5 '{cmd}'")

		def remove_simulation_results(self):
			# just remove locally, the --delete in the rsync options
			# will mean that the remote dir will not accumulate unexpected things
			for suffix in ["_simulate.gtkw", "_simulate.vcd"]:
				local_filename_to_delete = self.local_filename.replace(".py", suffix)
				os.remove(local_filename_to_delete)
				cprint(f"Removed local file, {local_filename_to_delete}", "blue", flush=True)


	class generate_manager:
		def __init__(self, fpga_interface, local_filename):
			self.fpga_interface = fpga_interface
			self.local_filename = local_filename

			self.rel_remote_filename = self.fpga_interface.get_remote_filename_from_local(self.local_filename, get_as_relative=True)
			self.rel_remote_dirname = os.path.dirname(self.rel_remote_filename)
			self.no_dir_filename = os.path.relpath(self.rel_remote_filename, self.rel_remote_dirname)

		def generate_file(self):			
			cprint(f"Running 'generate' on {self.no_dir_filename}...", "yellow", flush=True)
			
			cmd = f"cd {os.path.join(self.fpga_interface.remote_working_directory, self.rel_remote_dirname)} && "
			cmd += f"python3 {self.no_dir_filename} generate -t il > toplevel.il"
			result, error = self.fpga_interface.execute_commands(cmd, get_stderr=True)	
			# print("Result and error are: ", result, error)
			assert not any("Traceback" in line for line in error), f"Failed generate with error of {result},{error}"
			cprint(" OK", "green", flush=True)

		def run_through_symbyosis(self):
			cprint(f"Running sby on {self.no_dir_filename}...", "yellow", flush=True)
			self.sby_settings_filename = self.no_dir_filename.replace('.py', '.sby')
			self.sby_result_filename = self.no_dir_filename.replace('.py', '_generate_sby_output.txt')
			cmd = f"cd {os.path.join(self.fpga_interface.remote_working_directory, self.rel_remote_dirname)} && "
			cmd += f"~/Documents/oss-cad-suite/bin/sby -f {self.sby_settings_filename} > {self.sby_result_filename}"
			result, error = self.fpga_interface.execute_commands(cmd, get_stderr=True)
			print("Result and error are: ", result, error)
			cprint(" OK", "green", flush=True)

		def inspect_symbyosis_results(self):
			cprint(f"Inspecting symbyosis results...", "yellow", flush=True)
			with open(f"{os.path.dirname(self.local_filename)}/{self.sby_result_filename}", "r", encoding="utf-8") as f:
				lines = f.readlines()
			success = True
			found_tracefiles = []
			last_location = []
			for line in lines:
				line = line.replace("\n", "")
				line = line[13:] # get rid of unneeded time info

				if not success:
					break

				### look for errors firstly
				if ("ERROR" in line) or ("FAIL" in line):
					content_colour = "red"
					success = False # so open gtkwave viewer now				

				### for the 'it worked here's an example' thing
				elif ("Reached" in line):
					for segment in line.split(" "):
						if ".py" in segment:
							last_location.append(segment)
				elif ("trace" in line) and (".vcd" in line) and ("summary" in line) and ("starting" not in line):
					content_colour = "yellow"
					found_tracefiles.append({
						"filename" : line.split(" ")[-1],
						"source_location" : last_location[0] # assumption
					})
					last_location = last_location[1:]

				### for the 'it broke here's a counterexample' one
				elif ("Assert failed in " in line):
					for segment in line.split(" "):
						if ".py" in segment:
							last_location.append(segment)
				elif ("counterexample trace" in line):
					content_colour = "yellow"
					found_tracefiles.append({
						"filename" : line.split(" ")[-1],
						"source_location" : last_location[0] + " (counterexample)" # assumption
					})
					last_location = last_location[1:]
				
				### if every test passed, then no need to open gtkviewer
				elif "PASS" in line:
					content_colour = "green"

				else:
					content_colour = "blue"

				cprint(line, content_colour, flush=True)

			if len(found_tracefiles) > 0:
				# now ask which one to use
				if success:
					cprint("Passed tests", "green")
				else:
					cprint("Failed tests", "red")
				cprint("Which tracefile to open? Type the index, or press enter to skip", "green")
				for index, entry in enumerate(found_tracefiles):
					cprint(f"{index+1} {entry['filename']} \t {entry['source_location']}", "yellow")
				chosen_index = input()
				if chosen_index == "":
					chosen_file = None
				else:
					chosen_file = found_tracefiles[int(chosen_index)-1]["filename"]
			else:
				chosen_file = None

			cprint(" OK", "green", flush=True)

			return success, chosen_file
		
		def show_sby_vcd_file(self, local_sby_trace_file, on_host_pc):
			cprint(f"Opening symbyosis output in gtkwave...", "yellow", flush=True)
			if on_host_pc:
				cmd = f"gtkwave {os.path.dirname(self.local_filename)}/{local_sby_trace_file} --rcvar 'do_initial_zoom_fit yes'"
				lxdev.run_local_gui_cmd(cmd)

			else:
				assert 0, "not implemented yet"
				# note! the .gtkw doesn't work, but the .vcd does. The error seems similar to the one for local display, but is not fixed yet.
				# cmd = f"~/Documents/oss-cad-suite/bin/gtkwave {self.fpga_interface.remote_working_directory}/{self.rel_remote_filename.replace('.py', '_simulate.vcd')}  --rcvar 'do_initial_zoom_fit yes'"
				# lxdev.run_local_gui_cmd(f"ssh -X lxd_fpga-dev-ECP5 '{cmd}'")
			cprint(" OK", "green", flush=True)

		def remove_generate_results(self):
			cprint(f"Removing all files from running 'generate'...", "yellow", flush=True)
			# so remove everything that has the same start as the filename in question,
			# plus the toplevel.il file.
			files_and_dirs_to_delete = glob.glob(self.local_filename.replace(".py", "*"))
			files_and_dirs_to_delete += [f"{os.path.dirname(self.local_filename)}/toplevel.il"]

			# important - prevent the source file from being removed
			files_and_dirs_to_delete.remove(self.local_filename)

			print(files_and_dirs_to_delete)
			for file in files_and_dirs_to_delete:
				if os.path.isdir(file):
					shutil.rmtree(file)
				elif os.path.isfile(file):
					os.remove(file)
				else:
					assert 0, "this shouldn't happen"
			cprint(" OK", "green", flush=True)

	

class mcu_interface(lxdev.RemoteClient):
	defined_tasks = [
		"reflash-binary",
		"update-firmware",
		"enter-repl"
	]

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def do_task(self, task, local_filename=None):
		if task == "reflash-binary":
			self.ensure_we_have_compiled_binary("esp32-20220117-v1.18.bin")
			self.select_serial_port()
			self.run_communication_test()
			self.rsync_to_container()				
			self.erase_flash_from_device()
			self.write_binary_to_device()			

		elif task == "update-firmware":
			self.select_serial_port()
			self.rsync_to_container()
			self.rsync_micropython_files_between(from_dir = "mcu_firmware/source/", to_dir = "/pyboard/")
			self.rsync_micropython_files_between(from_dir = "common/saniwa_common/", to_dir = "/pyboard/saniwa_common/") # new!
			self.connect_over_rshell_repl()

		elif task == "enter-repl":
			self.select_serial_port()
			self.connect_over_rshell_repl()#cmd="import main")
		
		else:
			assert 0, "Invalid task given, aborting"


	def ensure_we_have_compiled_binary(self, desired_compiled_binary_filename):
		self.desired_compiled_binary_filename = desired_compiled_binary_filename
		# have we got a copy of the file, locally on the host?
		if not os.path.isfile(f"./mcu_firmware/compiled_binaries/{self.desired_compiled_binary_filename}"):
			if self.desired_compiled_binary_filename == "esp32-20220117-v1.18.bin":
				cprint("Downloading compiled binary file", "yellow")
				lxdev.run_local_cmd("wget https://micropython.org/resources/firmware/esp32-20220117-v1.18.bin --directory-prefix ./mcu_firmware/compiled_binaries/")
			else:
				assert 0, "specified binary unavailable"
			assert os.path.isfile(f"./mcu_firmware/compiled_binaries/{self.desired_compiled_binary_filename}")
		# cprint("binary file available", "yellow")
	
	# what port shall we use?
	def select_serial_port(self):
		ports = self.execute_commands("ls /dev/ttyUSB*")
		assert len(ports) != 0, "No serial port detected, check connection."
		assert len(ports) == 1, f"Found {ports}. Todo: add ability to specify which port"
		self.chosen_port = ports[0] # for now

	def run_communication_test(self):
		cprint("Running communication test to device...", "yellow", flush=True)
		result, error = self.execute_commands(f"esptool.py --chip esp32 --port {self.chosen_port} read_mac", get_stderr=True)
		#  --before default_reset 
		assert any("Chip is ESP32" in line for line in result), f"Failed comms test with error of {result},{error}, maybe hold down boot button?"
		cprint(" OK", "green", flush=True)

	def erase_flash_from_device(self):
		cprint("Erasing flash on device...", "yellow", flush=True)
		result, error = self.execute_commands(f"esptool.py --chip esp32 --port {self.chosen_port} erase_flash", get_stderr=True)
		assert any("Chip erase completed successfully" in line for line in result), f"Failed to erase flash with {result}, {error}"
		cprint(" OK", "green", flush=True)

	def write_binary_to_device(self):
		cprint(f"Writing binary file to device ({self.desired_compiled_binary_filename})...", "yellow", flush=True)
		result, error = self.execute_commands(f"esptool.py --chip esp32 --port {self.chosen_port} write_flash -z 0x1000 mcu_firmware/compiled_binaries/{self.desired_compiled_binary_filename}", get_stderr=True)
		assert any("Hash of data verified" in line for line in result), f"Failed to upload binary with {result}, {error}"
		cprint(" OK", "green", flush=True)

	def rsync_micropython_files_between(self, from_dir, to_dir):
		cprint(f"Copying files using rsync on {self.chosen_port} from {from_dir} to {to_dir}...", "yellow", flush=True)
		
		rshell_cmd = f"connect serial {self.chosen_port}; rsync {from_dir} {to_dir}; ls /pyboard/"

		output, error = self.execute_commands(
			"rshell", 
			get_stderr=True, 
			within_remote_working_dir=True,
			pass_to_stdin=rshell_cmd)

		assert any("connected" in line for line in output), f"Failed to connect over rshell with {output}, {error}"
		assert any((("Checking" in line) or ("Adding" in line)) for line in output), f"Failed to rsync over rshell with {output}, {error}"
		num_files_copied = [("copying" in line) or ("Adding" in line) for line in output].count(True)
		if num_files_copied == 0:
			cprint("(redundant, no file changes)", "magenta", flush=True)
		else:
			cprint(f"(copied {num_files_copied} file{'' if num_files_copied==1 else 's'})", "magenta", flush=True)
	
		cprint(" OK", "green", flush=True)

	def connect_over_rshell_repl(self, cmd = None):
		# todo - deal with cmd arg

		commands = [
			"rshell",
			"connect serial /dev/ttyUSB0",
			"repl"
		]
		
		if cmd != None:
			commands.append(cmd)

		try:
			self.interactive_shell(commands, within_remote_working_dir=True)
		except KeyboardInterrupt:
			pass

		cprint("Leaving micropythonn repl", "green", flush=True)

	

if __name__ == "__main__":
	main()

