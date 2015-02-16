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
from Queue import Queue, Empty


PIPES = ([0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2])
CHANNEL = 0x20


class Button:
    def __init__(self, pins):
        self.pins = pins
        self.states = {}
        self.events = Queue()
        for n, pin in enumerate(self.pins):
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(pin, GPIO.FALLING, callback=lambda channel: self.events.put(self.states[channel]), bouncetime=500)
            self.states[pin] = n

class Relay:
    def __init__(self, pins):
        self.pins = pins
        self.states = []
        for pin in self.pins:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
            self.states.append(0)

    def output(self, pin, state):
        print "setting pin", pin, state and "on" or "off"
        self.states[pin] = state
        GPIO.output(self.pins[pin], not state)  # These devices are active-low.

    def state(self, pin):
        return self.states[pin]

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
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, relay, button):
        self.relay = relay
        self.temperature = temperature
        self.button = button
        self.radio = nrf24.NRF24()
        self.radio.begin(major, minor, ce_pin, irq_pin)
        self.radio.setDataRate(self.radio.BR_250KBPS)
        self.radio.setChannel(CHANNEL)
        self.radio.setAutoAck(1)
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
                print "recv_buffer",recv_buffer,"temp",self.temperature.read()
                while True:
                    try:
                        event = self.button.events.get_nowait()
                    except Empty:
                        break
                    else:
                        recv_buffer.append(event)  # pin = 0, query = 0, state = event
                for byte in recv_buffer:
                    pin = byte >> 2
                    query = byte >> 1 & 1
                    state = byte & 1
                    print "pin",pin,"query",query,"state",state
                    if query:
                        self.radio.write([self.relay.state(pin)])
                    else:
                        self.relay.output(pin, state)
                start = time.time()
                result = self.radio.write(self.temperature.rawread())
                if not result:
                    print datetime.datetime.now(), "Did not receive ACK from controller after",time.time()-start,"seconds."
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
        self.radio.setDataRate(self.radio.BR_250KBPS)
        self.radio.setChannel(CHANNEL)
        self.radio.setAutoAck(1)
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
                        cmd = int(pin) << 2 | (state.lower() == 'query') << 1 | (state.lower() == 'on')
                        result = self.radio.write(chr(cmd))
                        recv_buffer = []
                        if state.lower() == 'query':
                            self.radio.startListening()
                            recv_buffer = self.recv(1)
                            self.radio.stopListening()
                        if not recv_buffer:
                            recv_buffer = '?'
                        elif len(recv_buffer) == 1:
                            recv_buffer = recv_buffer[0]
                        conn.sendall('%s %s\n' % ('OK' if result else 'timed out', recv_buffer))
                        print
                        print 'OK' if result else 'timed out', recv_line, recv_buffer
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
        end = time.time() + (timeout or 0.0)
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
    if args.pidfile:
        with open(args.pidfile, 'w') as f:
            print >>f, os.getpid()
    try:
        if args.mode == 'boiler':
            with Boiler(0, 0, 25, 24, Temperature(0, 1), Relay([17, 18]), Button([23, 24])) as radio:
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
