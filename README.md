# Amaram

This is a python library, providing RAM peripheral interfaces for amaranth language HDL projects.

Currently, only a n-async-FIFO interface for SDRAM is implemented.

## status

As of Feb 2022, not yet working. 

Test performance:

- On a ULX3S board: [test not yet implemented]
- On a spartan6 board: [test not yet implemented]

## next steps
- add a demo use app, perhaps implementing the backend of a logic analyser using the LUNA interfaces
- Then, replace the backend of the LUNA interface

## later steps
- add quality documentation, and perhaps a video of how it works
- add tests that run on the ULX3S board, and on a second board demonstrating that it can be cross-platform

## thoughts
- make the mcu fifo interface as close as possible to the nmigen fifo simulation interface?
- it seems hard to test! What's the simplest way? connect up to the backend of a logic analyser, then the input to a counter, and make sure no counts are skipped?

## issues
- 


