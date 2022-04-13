# Amaram test - ulx3s counter

If, on the ulx3s, an incrementing counter is used to fill up an amaram-fifo, can the ulx3s's esp32 read back the data as expected?



# notes

1. put a fresh copy of micropython on the device with `mcu reflash binary`
2. `mcu update-firmware`, then in repl start with `import main`, `main.run_test()`