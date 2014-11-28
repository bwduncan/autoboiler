#!/usr/bin/python

import spidev
import os
import sys
import sqlite3
import time
import datetime
import itertools


def main(tc77, idx):
	conn = sqlite3.connect('/var/lib/autoboiler/autoboiler.sqlite3')
	conn.isolation_level = None
	c = conn.cursor()
	c.execute('''CREATE TABLE IF NOT EXISTS temperature (date datetime, sensor integer, temperature real)''')
	try:
		for i in itertools.cycle(range(20)):
			t = tc77.read()
			print datetime.datetime.now(),  t, '        \r',
			sys.stdout.flush()
			c.execute('''insert into temperature values (?, ?, ?)''',
					(datetime.datetime.now(), idx, t))
			conn.commit()
			time.sleep(30)
	except KeyboardInterrupt:
		pass
	finally:
		conn.commit()


class temperature():
	def __init__(self, major=0, minor=0):
		self.spi = spidev.SpiDev()
		self.spi.open(major, minor)
	def read(self):
		return self._calcTemp(self.spi.xfer2([0, 0]))
	def _calcTemp(self, buf):
		return (((buf[0] << 8) | buf[1]) >> 3) * 0.0625
	def __enter__(self):
		return self
	def __exit__(self, type, value, tb):
		self.spi.close()


if __name__ == '__main__':
	with open('/etc/default/templog') as f:
		i = int(f.read())
	with temperature(0, i) as tc77:
		sys.exit(main(tc77, i))
