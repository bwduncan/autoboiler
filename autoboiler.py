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
import select
from collections import deque, defaultdict, namedtuple
from Queue import Queue, Empty


PIPES = ([0xe7, 0xe7, 0xe7, 0xe7, 0xe7], [0xc2, 0xc2, 0xc2, 0xc2, 0xc2])
CHANNEL = 0x20


class Button(object):
    def __init__(self, pins):
        self.pins = pins
        self.states = {}
        self.events = Queue()
        for i, pin in enumerate(self.pins):
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(pin, GPIO.FALLING, callback=self.add_event,
                                  bouncetime=500)
            self.states[pin] = i

    def add_event(self, channel):
        self.events.put(self.states[channel])


class Relay(object):
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


class Temperature(object):
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

    def __exit__(self, type_, value, traceback):
        self.cleanup()


class Boiler(object):
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
                recv_buffer = self.recv(10)
                print "recv_buffer", recv_buffer, "temp", self.temperature.read()
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
                    print "pin", pin, "query", query, "state", state
                    if query:
                        self.radio.write([self.relay.state(pin)])
                    else:
                        self.relay.output(pin, state)
                start = time.time()
                result = self.radio.write(self.temperature.rawread())
                if not result:
                    print datetime.datetime.now(), "Did not receive ACK from controller after", time.time() - start, "seconds."
            except Exception as exc:
                print exc

    def recv(self, timeout=None):
        end = time.time() + timeout
        pipe = [0]
        self.radio.startListening()
        try:
            while not self.radio.available(pipe) and (timeout is None or time.time() < end):
                time.sleep(10000 / 1e6)
            if self.radio.available(pipe):
                recv_buffer = []
                self.radio.read(recv_buffer)
                return recv_buffer
            return []
        finally:
            self.radio.stopListening()

    def cleanup(self):
        self.radio.end()

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.cleanup()


action = namedtuple('action', 'metric value pin state')

class Controller(object):
    def __init__(self, major, minor, ce_pin, irq_pin, temperature, db, sock, relay):
        self.temperature = temperature
        self.db = db
        self.sock = sock
        self.relay = relay
        self.actions = []
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
                recv_buffer = self.recv(10, rfds=[self.sock])

                if recv_buffer and len(recv_buffer) == 2:
                    self.db.write(1, self.temperature.calc_temp(recv_buffer))
                temp = self.temperature.read()
                self.db.write(0, temp)

                for i, (metric, value, pin, state) in enumerate(sorted(self.actions)):
                    if metric == 'temp' and temp >= value or \
                            metric == 'time' and time.time() >= value:
                        del self.actions[i]
                        result = self.control(pin, state)
                        print '\n', datetime.datetime.now(), "action matched:", metric, value, pin, state, "=>", result
                        if not result:
                            print 'action failed, will retry in 10s.'
                            self.actions.append(action(metric, value, pin, state))
                        break
                try:
                    conn, _ = self.sock.accept()
                except socket.error as exc:
                    if exc.errno != errno.EAGAIN:
                        raise
                else:
                    try:
                        conn.settimeout(10)
                        recv_line = conn.recv(1024)
                        args = recv_line[:-1].split(None, 2)
                        if len(args) > 2:
                            state, pin, arg = args
                            pin = int(pin)
                            if state == 'boost':
                                args = arg.split()
                                if len(args) == 2:
                                    metric, value = args
                                    value = float(value)
                                    if metric == 'temp' and temp >= value:
                                        conn.sendall('temperature already above target!\n')
                                        continue
                                    if metric == 'time' and temp <= 0:
                                        conn.sendall('time delta must be positive!\n')
                                        continue
                                    if metric == 'time':
                                        value += time.time()
                                    self.actions.append(action(metric, value, pin, 'off'))
                                    print '\n', datetime.datetime.now(), "added action", state, metric, value, pin
                                    state = 'on'  # continue to turn the boiler on
                        else:
                            state, pin = args
                            pin = int(pin)
                        result = self.control(pin, state)
                        recv_buffer = ''  # Need to clear buffer each time through the loop.
                        if state.lower() == 'query':
                            if pin < 0:  # A hack to control local relays.
                                recv_buffer = self.relay.state(-pin)
                            else:
                                recv_buffer = self.recv(1)
                        elif state.lower() == 'queryactions':
                            recv_buffer = str(self.actions)
                        if not recv_buffer:
                            recv_buffer = ''
                        elif len(recv_buffer) == 1:
                            recv_buffer = recv_buffer[0]
                        conn.sendall('%s %s\n' % ('OK' if result else 'timed out', recv_buffer))
                        print
                        print 'OK' if result else 'timed out', recv_line, recv_buffer
                    except Exception as exc:
                        print
                        print '\n', datetime.datetime.now(), "got invalid line:", repr(recv_line), exc
                        try:
                            conn.sendall('invalid request: {!s}\n'.format(exc))
                        except socket.error:
                            pass
                    finally:
                        conn.close()
        except KeyboardInterrupt:
            print

    def control(self, pin, state):
        if pin < 0:
            self.relay.output(-pin - 1, state.lower() == 'on')
            return True
        else:
            cmd = pin << 2 | (state.lower() == 'query') << 1 | (state.lower() == 'on')
            return self.radio.write(chr(cmd))

    def recv(self, timeout=None, rfds=None):
        if rfds is None:
            rfds = []
        end = time.time() + (timeout or 0.0)
        pipe = [0]
        self.radio.startListening()
        try:
            while not self.radio.available(pipe) and (timeout is None or time.time() < end):
                #time.sleep(10000 / 1e6)
                r, _, _ = select.select(rfds, [], [], 10000 / 1e6)
                if r:
                    return []
            if self.radio.available(pipe):
                recv_buffer = []
                self.radio.read(recv_buffer)
                return recv_buffer
            return []
        finally:
            self.radio.stopListening()

    def cleanup(self):
        self.radio.end()
        self.db.close()
        self.temperature.cleanup()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.cleanup()


def tridian(mylist, sum=sum, sorted=sorted):
    """Optimised median function. Assumes delta is 21."""
    return sum(sorted(mylist)[7:14]) / 7.


def tridian_slow(mylist):
    """Unoptimised median function."""
    sorts = sorted(mylist)
    tri = len(sorts) / 3
    return sum(sorts[tri:2 * tri]) / float(tri)


class DBWriter(object):
    def __init__(self):
        self.buf = defaultdict(deque)
        self.con = sqlite3.connect('/var/lib/autoboiler/autoboiler.sqlite3')
        self.con.isolation_level = None
        self.cur = self.con.cursor()
        self.cur.execute('''CREATE TABLE IF NOT EXISTS temperature
                          (date datetime, sensor integer, temperature real)''')
        self.cur.execute('''CREATE TABLE IF NOT EXISTS temperature_raw
                          (date datetime, sensor integer, temperature real)''')
        self.cur.execute('''CREATE INDEX IF NOT EXISTS temperature_raw_sensor_date
                          ON temperature_raw(sensor, date)''')
        self.cur.execute('''CREATE INDEX IF NOT EXISTS temperature_sensor_date
                          ON temperature(sensor, date)''')

    def write(self, idx, value):
        data = (datetime.datetime.now(), idx, value)
        line = "%s %d %f" % data
        if idx > 0:
            print '\033[%dC' % len(line) * idx,
        print line, '\r',
        sys.stdout.flush()
        self.buf[idx].append(data)
        try:
            self.cur.execute('insert into temperature_raw values (?, ?, ?)',
                             data)
            if len(self.buf[idx]) >= 21:
                # Take the middle-ish value to use for the time.
                data = (self.buf[idx][10][0], idx, tridian([x[2] for x in self.buf[idx]]))
                self.buf[idx].popleft()
                self.cur.execute('insert into temperature values (?, ?, ?)',
                                 data)
        except sqlite3.OperationalError as exc:
            print '\n', exc

    def close(self):
        self.con.commit()
        self.cur.close()
        self.con.close()


def main():
    GPIO.setmode(GPIO.BCM)
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', required=True, choices=['boiler', 'controller'])
    parser.add_argument('--pidfile',  '-p', default='/var/run/autoboiler.pid')
    parser.add_argument('--sock', '-s', default='/var/lib/autoboiler/autoboiler.socket')
    args = parser.parse_args()
    if args.pidfile:
        with open(args.pidfile, 'w') as f:
            print >>f, os.getpid()
    try:
        if args.mode == 'boiler':
            with Boiler(0, 0, 25, 24, Temperature(0, 1), Relay([17, 18]), Button([23, 24])) as radio:
                radio.run()
        elif args.mode == 'controller':
            try:
                os.unlink(args.sock)
            except OSError as exc:
                if exc.errno != errno.ENOENT and os.path.exists(args.sock):
                    raise
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(args.sock)
            os.chmod(args.sock, 0777)
            sock.setblocking(0)
            sock.listen(1)
            with Controller(0, 1, 25, 24, Temperature(0, 0), DBWriter(), sock, Relay([15, 14])) as radio:
                radio.run()
    finally:
        GPIO.cleanup()
        if args.pidfile:
            os.unlink(args.pidfile)
        if args.sock and args.mode == 'controller':
            try:
                os.unlink(args.sock)
            except OSError as exc:
                if exc.errno != errno.ENOENT and os.path.exists(args.sock):
                    raise
    sys.exit(0)

if __name__ == '__main__':
    main()

# vim: set et sw=4 ts=4 sts=4 ai:
