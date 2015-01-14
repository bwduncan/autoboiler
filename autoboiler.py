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
import errno
import socket


PIPES = ([0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2])


class Relay:
    def __init__(self, pins):
        self.pins = pins
        for pin in self.pins.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

    def output(self, pin, state):
        print "setting pin", pin, state and "on" or "off"
        GPIO.output(self.pins[pin], not state)  # These devices are active-low.

    def cleanup(self):
        pass  # this will be done later: GPIO.cleanup()


class Temperature:
    def __init__(self, major=0, minor=0):
        self.spi = spidev.SpiDev()
        self.spi.open(major, minor)

    def rawread(self):
        return self.spi.xfer2([0, 0])

    def read(self):
        return self.calc_temp(self.rawread())

    @staticmethod
    def calc_temp(buf):
        return (((buf[0] << 8) | buf[1]) >> 3) * 0.0625

    def cleanup(self):
        self.spi.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.cleanup()


class Boiler:
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, relay):
        self.relay = relay
        self.temperature = temperature
        self.radio = nrf24.NRF24()
        self.radio.begin(major, minor, ce_pin, irq_pin)
        self.radio.setChannel(96)
        self.radio.enableDynamicPayloads()
        self.radio.printDetails()
        self.radio.openWritingPipe(PIPES[0])
        self.radio.openReadingPipe(1, PIPES[1])

    def run(self):
        while True:
            try:
                self.radio.startListening()
                recv_buffer = self.recv(10)
                self.radio.stopListening()
                for byte in recv_buffer:
                    pin = byte >> 1
                    state = byte & 0x1
                    self.relay.output(pin, state)
                result = self.radio.write(self.temperature.rawread())
                if not result:
                    print datetime.datetime.now(), "Did not receive ACK from controller."
            except Exception as e:
                print e

    def recv(self, timeout=None):
        end = time.time() + timeout
        pipe = [0]
        while not self.radio.available(pipe) and (timeout is None or time.time() < end):
            time.sleep(10000 / 1e6)
        if self.radio.available(pipe):
            recv_buffer = []
            self.radio.read(recv_buffer)
            return recv_buffer
        return []

    def cleanup(self):
        self.radio.end()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.cleanup()


class Controller:
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, db, sock):
        self.temperature = temperature
        self.db = db
        self.sock = sock
        self.radio = nrf24.NRF24()
        self.radio.begin(major, minor, ce_pin, irq_pin)
        self.radio.setChannel(96)
        self.radio.enableDynamicPayloads()
        self.radio.printDetails()
        self.radio.openWritingPipe(PIPES[0])
        self.radio.openReadingPipe(1, PIPES[1])

    def run(self):
        try:
            while True:
                self.radio.startListening()
                recv_buffer = self.recv(10)
                self.radio.stopListening()
                if recv_buffer and len(recv_buffer) == 2:
                    self.db.write(1, self.temperature.calc_temp(recv_buffer))
                self.db.write(0, self.temperature.read())
                try:
                    conn, addr = sock.accept()
                except socket.error as e:
                    if e.errno != errno.EAGAIN:
                        raise
                else:
                    try:
                        conn.settimeout(10)
                        recv_line = conn.recv(1024)
                        state, pin = recv_line[:-1].split()
                        cmd = int(pin) << 1 | (state.lower() == 'on')
                        result = self.radio.write(chr(cmd))
                        conn.sendall('%s\n' % ('OK' if result else 'timed out'))
                        print
                        print 'OK' if result else 'timed out'
                    except Exception as e:
                        print
                        print "got invalid line:", repr(recv_line), e
                        try:
                            conn.sendall('invalid request\n')
                        except socket.error:
                            pass
                    finally:
                        conn.close()
        except KeyboardInterrupt:
            print

    def recv(self, timeout=None):
        end = time.time() + timeout
        pipe = [0]
        while not self.radio.available(pipe) and (timeout is None or time.time() < end):
            time.sleep(10000 / 1e6)
        if self.radio.available(pipe):
            recv_buffer = []
            self.radio.read(recv_buffer)
            return recv_buffer
        return []

    def cleanup(self):
        self.radio.end()
        self.db.close()
        self.temperature.cleanup()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.cleanup()


class DBWriter:
    def __init__(self):
        self.conn = sqlite3.connect('/var/lib/autoboiler/autoboiler.sqlite3')
        self.conn.isolation_level = None
        self.c = self.conn.cursor()
        self.c.execute('''CREATE TABLE IF NOT EXISTS temperature
                          (date datetime, sensor integer, temperature real)''')
        self.c.execute('''CREATE INDEX IF NOT EXISTS temperature_date
                          ON temperature(date)''')

    def write(self, idx, value):
        line = "%s %d %f" % (datetime.datetime.now(), idx, value)
        if idx > 0:
            print '\033[%dC' % len(line * idx),
        print line, '\r',
        sys.stdout.flush()
        try:
            self.c.execute('''insert into temperature values (?, ?, ?)''',
                           (datetime.datetime.now(), idx, value))
        except sqlite3.OperationalError:
            pass

    def close(self):
        self.conn.commit()
        self.c.close()
        self.conn.close()


if __name__ == '__main__':
    GPIO.setmode(GPIO.BCM)
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
            with Boiler(0, 0, 25, 24, Temperature(0, 1), Relay({0: 17, 1: 18})) as radio:
                radio.run()
        elif args.mode == 'controller':
            sockname = '/var/lib/autoboiler/autoboiler.socket'
            try:
                os.unlink(sockname)
            except OSError as e:
                if e.errno != errno.ENOENT and os.path.exists(sockname):
                    raise
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(sockname)
            os.chmod(sockname, 0777)
            sock.setblocking(0)
            sock.listen(1)
            with Controller(0, 1, 25, 24, Temperature(0, 0), DBWriter(), sock) as radio:
                radio.run()
    finally:
        GPIO.cleanup()
        if args.pidfile:
            os.unlink(args.pidfile)
    sys.exit(0)

# vim: set et sw=4 ts=4 sts=4 ai:
