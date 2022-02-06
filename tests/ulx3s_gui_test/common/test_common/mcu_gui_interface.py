# for the wifi interface 

MY_ID = '1'  # Client-unique string
PORT = 8123
TIMEOUT = 2000*3

# note, using json for the config file so micropython can load it easily
import json
with open("mcu_gui_interface.configjson") as fp:
	configdata = json.loads(fp.read())

SERVER = configdata["wifi"]["SERVER"]
SSID = configdata["wifi"]["SSID"]
PW = configdata["wifi"]["PW"]


print("/".join(__file__.split("/")[:-1]))