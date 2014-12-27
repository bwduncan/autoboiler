#!/usr/bin/python

import RPi.GPIO as GPIO
import nrf24
import sys
import time
import spidev
import argparse
import os
import sqlite3
import datetime


pipes = ([0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2])


class Relay:
    def __init__(self):
        self.pins = {0: 17, 1: 18}
        for pin in self.pins.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

    def output(self, pin, state):
        # print "setting pin", pin, state and "on" or "off"
        GPIO.output(pin, not state)  # These devices are active-low.

    def cleanup(self):
        pass  # this will be done later: GPIO.cleanup()


class Temperature:
    def __init__(self, major=0, minor=0):
        self.spi = spidev.SpiDev()
        self.spi.open(major, minor)

    def rawread(self):
        return self.spi.xfer2([0, 0])

    def read(self):
        return self.calcTemp(self.rawread())

    def calcTemp(self, buf):
        return (((buf[0] << 8) | buf[1]) >> 3) * 0.0625

    def cleanup(self):
        self.spi.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.spi.close()


class Boiler:
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, relay):
        self.relay = relay
        self.temperature = temperature
        self.radio = nrf24.NRF24()
        self.radio.begin(major, minor, ce_pin, irq_pin)
        self.radio.enableDynamicPayloads()
        self.radio.printDetails()
        self.radio.openWritingPipe(pipes[0])
        self.radio.openReadingPipe(1, pipes[1])
        self.radio.setAutoAck(1)

    def run(self):
        while True:
            try:
                self.radio.startListening()
                recv_buffer = self.recv(10)
                for byte in recv_buffer:
                    pin = self.relay.pins[byte >> 1]
                    state = byte & 0x1
                    self.relay.output(pin, state)
                self.radio.StopListening()
                self.radio.write(self.temperature.rawread())
            except Exception:
                pass

    def recv(self, timeout=None):
        end = time.time() + timeout
        pipe = [0]
        while not self.radio.available(pipe) and (timeout is None or time.time() < end):
            time.sleep(10000/1e6)
        if self.radio.available(pipe):
            recv_buffer = []
            self.radio.read(recv_buffer)
            return recv_buffer
        return []

    def cleanup(self):
        self.radio.end()


class Controller:
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, db):
        self.temperature = temperature
        self.db = db
        self.radio = nrf24.NRF24()
        self.radio.begin(major, minor, ce_pin, irq_pin)
        self.radio.enableDynamicPayloads()
        self.radio.printDetails()
        self.radio.openWritingPipe(pipes[0])
        self.radio.openReadingPipe(1, pipes[1])

    def run(self):
        try:
            while True:
                self.radio.startListening()
                recv_buffer = self.recv(10)
                self.radio.stopListening()
                self.db.write(0, self.temperature.read())
                if recv_buffer:
			self.db.write(1, self.temperature.calcTemp(recv_buffer))
        except KeyboardInterrupt:
            pass

    def recv(self, timeout=None):
        end = time.time() + timeout
        pipe = [0]
        while not self.radio.available(pipe) and (timeout is None or time.time() < end):
            time.sleep(10000/1e6)
        if self.radio.available(pipe):
            recv_buffer = []
            self.radio.read(recv_buffer)
            return recv_buffer
        return []

    def cleanup(self):
        self.radio.end()
        self.db.close()
        self.temperature.cleanup()


class DBWriter:
    def __init__(self):
        self.conn = sqlite3.connect('/var/lib/autoboiler/autoboiler.sqlite3')
        self.conn.isolation_level = None
        self.c = self.conn.cursor()
        self.c.execute('''CREATE TABLE IF NOT EXISTS temperature (date datetime, sensor integer, temperature real)''')

    def write(self, idx, value):
        print ' '*idx*20, datetime.datetime.now(), idx, value, '        \r',
        sys.stdout.flush()
        try:
                self.c.execute('''insert into temperature values (?, ?, ?)''',
                               (datetime.datetime.now(), idx, value))
        except sqlite3.OperationalError:
                pass

    def close(self):
        self.conn.commit()
        self.conn.close()


if __name__ == '__main__':
    GPIO.setmode(GPIO.BCM)
    radio = None
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', required=True, choices=['boiler', 'controller'])
    parser.add_argument('--pidfile',  '-p')
    args = parser.parse_args()
    with open('/etc/default/templog') as f:
        i = int(f.read())
    if args.pidfile:
        with open(args.pidfile, 'w') as f:
            print >>f, os.getpid()
    try:
        if args.mode == 'boiler':
            radio = Boiler(0, 0, 25, 24, Temperature(0, 1), Relay())
            radio.run()
        elif args.mode == 'controller':
            db = DBWriter()
            radio = Controller(0, 1, 25, 24, Temperature(0, 0), db)
            radio.run()
    finally:
        if radio:
            radio.cleanup()
            GPIO.cleanup()
        if args.pidfile:
            os.unlink(args.pidfile)
    sys.exit(0)
