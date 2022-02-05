# Doc

How could I best communicate what's going on


here's an idea, does it work?

xx
```python
								def when_burst_ends_change_fifo_or_readwrite():
									with self.m.If((self.burst_index + 1) == self.burstlen): # burst finished
										self.m.d.sdram += self.burst_index.eq(0)
```
yyy
```python
										with self.m.If((self.numburst_index + 1) == self.numbursts): # done several bursts with this fifo, now move on
											self.m.d.sdram += self.numburst_index.eq(0)
```
kljlkjlk jlk jlk 

```python
											self.m.d.sdram += self.fifo_index.eq(next_dstfifo_index) # prepare to do the next fifo
```

 jkl jkl jlkj lk jlk 

```python
											with self.m.If(self.core.refresh_controller.request_to_refresh_soon):
												self.m.next = "REFRESH_OR_IDLE"
```


jkljklj