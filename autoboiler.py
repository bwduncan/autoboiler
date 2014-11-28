#!/usr/bin/python

import RPi.GPIO as GPIO
import nrf24
import sys
import time
import spidev
import argparse
import os

pipes = ( [0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2] )

def boiler(relay):
	radio = nrf24.NRF24()
	radio.begin(0,0,25,24)
	#radio.enableDynamicPayloads()
	radio.setPayloadSize(1)
	radio.printDetails()
	radio.openWritingPipe(pipes[0])
	radio.openReadingPipe(1, pipes[1])
	radio.setAutoAck(1)
	radio.startListening()
	while True:
		try:
			pipe = [0]
			while not radio.available(pipe):
				time.sleep(10000/1e6)
			recv_buffer = []
			radio.read(recv_buffer)
			print recv_buffer
			for byte in recv_buffer:
				pin = relay.pins[byte >> 1]
				state = byte & 0x1
				relay.output(pin, state)
		except Exception:
			pass	
		finally:
			relay.cleanup()
	radio.stopListening()
	return radio

def controller(temperature, on, off):
	radio = nrf24.NRF24()
	radio.begin(0,1,25,24)
	#radio.enableDynamicPayloads()
	radio.setPayloadSize(1)
	radio.openWritingPipe(pipes[0])
	radio.openReadingPipe(1, pipes[1])
	try:
		for relay in on:
			radio.write([int(relay) << 1 | 1])
		for relay in off:
			radio.write([int(relay) << 1 | 0])
		# Do this if dynamic payload lengths ever work.
		#if on or off:
		#	radio.write([int(relay) << 1 | 0 for relay in off] + [int(relay) << 1 | 1 for relay in on])
		return radio
	finally:
		temperature.cleanup()


if __name__ == '__main__':
	GPIO.setmode(GPIO.BCM)
	class relay:
		def __init__(self):
			self.pins = {0: 17, 1: 18}
			for pin in self.pins.values():
				GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
		def output(self, pin, state):
			print "setting pin", pin, state and "on" or "off"
			GPIO.output(pin, not state) # These devices are active-low.
		def cleanup(self):
			pass # this will be done later: GPIO.cleanup()
	class temperature:
		def __init__(self, major=0, minor=0):
			self.spi = spidev.SpiDev()
			self.spi.open(major, minor)
		def read(self):
			return self._calcTemp(self.spi.xfer2([0,0]))
		def _calcTemp(self, buf):
			return (((buf[0] << 8) | buf[1]) >> 3) * 0.0625
		def cleanup(self):
			self.spi.close()
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
			radio = boiler(relay())
		elif args.mode == 'controller':
			radio = controller(temperature(), args.on, args.off)
	finally:
		if radio:
			radio.end()
		if args.pidfile:
			os.unlink(args.pidfile)
		GPIO.cleanup()
	sys.exit(0)
