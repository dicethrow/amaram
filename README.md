# Amaram

This is a python library, providing RAM peripheral interfaces for amaranth language HDL projects.

Currently, only a n-async-FIFO interface for SDRAM is implemented, and it has only been tested on a ULX3S board.

# todo
- add quality documentation, and perhaps a video of how it works
- add tests that run on the ULX3S board, and on a second board demonstrating that it can be cross-platform
- add a demo use app, perhaps implementing the backend of a logic analyser using the LUNA interfaces

# issues
- the ascii diagrams assume tab=4spaces, which is not the default on github, so the diagrams are wrong
	- idea: switch between tabs (on my machine) and spaces (on github) using something like this https://stackoverflow.com/questions/2316677/can-git-automatically-switch-between-spaces-and-tabs
	- alternatively, trying to implement a `.editorconfig` file, from the ideas here https://stackoverflow.com/questions/8833953/how-to-change-tab-size-on-github


