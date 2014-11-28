#!/usr/bin/python

import RPi.GPIO as GPIO
import nrf24
import sys
import time
import spidev
import argparse
import os


pipes = ( [0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2] )


class Relay:
	def __init__(self):
		self.pins = {0: 17, 1: 18}
		for pin in self.pins.values():
			GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
	def output(self, pin, state):
		#print "setting pin", pin, state and "on" or "off"
		GPIO.output(pin, not state) # These devices are active-low.
	def cleanup(self):
		pass # this will be done later: GPIO.cleanup()


class Temperature:
	def __init__(self, major=0, minor=0):
		self.spi = spidev.SpiDev()
		self.spi.open(major, minor)
	def read(self):
		return self._calcTemp(self.spi.xfer2([0,0]))
	def _calcTemp(self, buf):
		return (((buf[0] << 8) | buf[1]) >> 3) * 0.0625
	def cleanup(self):
		self.spi.close()


class Boiler:
	def __init__(self, major, minor, ce_pin, irq_pin, relay, temperature):
		self.radio = nrf24.NRF24()
		self.radio.begin(major, minor, ce_pin, irq_pin)
		#self.radio.enableDynamicPayloads()
		self.radio.setPayloadSize(1)
		self.radio.printDetails()
		self.radio.openWritingPipe(pipes[0])
		self.radio.openReadingPipe(1, pipes[1])
		self.radio.setAutoAck(1)

	def run():
		self.radio.startListening()
		while True:
			try:
				pipe = [0]
				while not self.radio.available(pipe):
					time.sleep(10000/1e6)
				recv_buffer = []
				self.radio.read(recv_buffer)
				print recv_buffer
				for byte in recv_buffer:
					pin = relay.pins[byte >> 1]
					state = byte & 0x1
					relay.output(pin, state)
			except Exception:
				pass	
			finally:
				relay.cleanup()
		self.radio.stopListening()

	def cleanup(self):
		self.radio.end()


class Controller:
	def __init__(self, major, minor, ce_pin, irq_pin, temperature):
		self.radio = nrf24.NRF24()
		self.radio.begin(major, minor, ce_pin, irq_pin)
		#self.radio.enableDynamicPayloads()
		self.radio.setPayloadSize(1)
		self.radio.openWritingPipe(pipes[0])
		self.radio.openReadingPipe(1, pipes[1])
	def run(on, off):
		for relay in on:
			self.radio.write([int(relay) << 1 | 1])
		for relay in off:
			self.radio.write([int(relay) << 1 | 0])
		# Do this if dynamic payload lengths ever work.
		#if on or off:
		#	self.radio.write([int(relay) << 1 | 0 for relay in off] + [int(relay) << 1 | 1 for relay in on])
	def cleanup(self):
		self.radio.end()


if __name__ == '__main__':
	GPIO.setmode(GPIO.BCM)
	radio = None
	parser = argparse.ArgumentParser()
	parser.add_argument('--mode', required=True, choices=['boiler', 'controller'])
	parser.add_argument('--pidfile',  '-p')
	parser.add_argument('--on', '-1', action='append', default=[])
	parser.add_argument('--off', '-0', action='append', default=[])
	args = parser.parse_args()
	if args.pidfile:
		with open(args.pidfile, 'w') as f:
			print >>f, os.getpid()
	try:
		if args.mode == 'boiler':
			radio = Boiler(0, 0, 25, 24, Relay(), Temperature(0, 1))
			radio.run()
		elif args.mode == 'controller':
			radio = Controller(0, 1, 25, 24, Temperature(0, 0))
			radio.run(args.on, args.off)
	finally:
		if radio:
			radio.cleanup()
		if args.pidfile:
			os.unlink(args.pidfile)
		GPIO.cleanup()
	sys.exit(0)
