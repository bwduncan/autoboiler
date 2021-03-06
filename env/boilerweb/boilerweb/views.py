# vim: set fileencoding=utf-8:
from pyramid.response import Response
from pyramid.view import view_config

from pyramid.httpexceptions import HTTPFound
from sqlalchemy.exc import DBAPIError

from .models import (
    DBSession,
    temperature,
    channel,
    )

from datetime import datetime, timedelta
import StringIO
import socket
from contextlib import closing
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plot


@view_config(route_name='home', renderer='templates/home.pt')
def my_view(request):
    try:
        zero = DBSession.query(temperature).filter(temperature.sensor == 0).order_by(temperature.date.desc()).first()
        one = DBSession.query(temperature).filter(temperature.sensor == 1).order_by(temperature.date.desc()).first()
    except DBAPIError as e:
        print e
        return Response(conn_err_msg, content_type='text/plain', status_int=500)
    return {'zero': zero, 'one': one, 'project': 'boilerweb'}


@view_config(route_name='queryactions')
def queryactions(request):
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
        sock.settimeout(10)
        try:
            sock.connect('/var/lib/autoboiler/autoboiler.socket')
            sock.sendall('queryactions\n')
            return Response(sock.recv(1024))
        except (socket.timeout, socket.error) as e:
            return Response(str(e))


@view_config(route_name='query')
def query(request):
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
        sock.settimeout(10)
        try:
            sock.connect('/var/lib/autoboiler/autoboiler.socket')
            sock.sendall('query {channel}\n'.format(channel=int(request.params.get('channel', 0))))
            return Response(sock.recv(1024))
        except (socket.timeout, socket.error) as e:
            return Response(str(e))


@view_config(request_method='GET', route_name='control', renderer='templates/control.pt')
def get_control_view(request):
    return {'channels': DBSession.query(channel).order_by(channel.name).all()}


@view_config(request_method='POST', route_name='control', renderer='templates/control.pt')
def post_control_view(request):
    params = {}
    params['channel'] = int(request.params['channel'])
    if request.params['state'].lower() not in ('on', 'off', 'boost'):
        raise ValueError('state must be "on", "off" or "boost"')
    if request.params['state'].lower() == 'boost':
        if 'value' in request.params:
            params['value'] = float(request.params['value'])
        else:
            raise KeyError('value undefined')
        if 'metric' in request.params:
            if request.params['metric'] not in ('temp', 'time'):
                raise ValueError('metric must be "temp" or "time"')
            else:
                params['metric'] = request.params['metric']
        else:
            raise KeyError('metric undefined')
        params['state_human'] = 'boosted'
        if params['metric'] == 'temp':
            params['state_human'] += u' to {} °C'.format(params['value'])
        elif params['metric'] == 'time':
            if params['value'] <= 0:
                raise ValueError('time value must be greater than zero')
            params['state_human'] += ' for {} minutes'.format(params['value'] / 60.)
    else:
        params['state_human'] = 'turned ' + request.params['state'].lower()
    params['state'] = request.params['state'].lower()
    params['name'] = DBSession.query(channel.name)\
                              .filter(channel.id==params['channel'])\
                              .one()[0]

    request.session.flash(u"You asked for the {name} to be {state_human}.".format(**params))
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
        sock.settimeout(10)
        try:
            sock.connect('/var/lib/autoboiler/autoboiler.socket')
            if 'metric' in params and 'value' in params:
                cmd = '{state} {channel} {metric} {value}\n'.format(**params)
            else:
                cmd = '{state} {channel}\n'.format(**params)
            sock.sendall(cmd)
            reply = sock.recv(1024)
        except (socket.timeout, socket.error) as e:
            reply = e
    request.session.flash("The result was: " + str(reply))
    return HTTPFound()


def index_min(values):
    return min(range(len(values)), key=values.__getitem__)


def index_max(values):
    return max(range(len(values)), key=values.__getitem__)


def plot_data(request, ax, sensor):
    start_time = datetime.now() - timedelta(days=float(request.params.get('days', 1)))
    data = DBSession.query(temperature.date, temperature.temperature)\
                    .filter(temperature.sensor == sensor)\
                    .filter(temperature.date > start_time)\
                    .order_by(temperature.date).all()
    data0 = []
    x = []
    for d in data:
        data0.append(d.temperature)
        x.append(d.date)
    if len(data0) == 0:  # Still no data, there really is nothing to draw
        return
    line_colours = ['r-', 'b-', 'g-']
    ax.plot_date(matplotlib.dates.date2num(x), data0, line_colours[sensor], xdate=True)
    ax.text(x[0], data0[0], u'%2.1f°C' % data0[0])
    ax.text(x[-1], data0[-1], u'%2.1f°C' % data0[-1])
    maxtemp = index_max(data0)
    mintemp = index_min(data0)
    edge = len(data0) / 8
    if edge < mintemp < len(data0) - edge - 1:
        ax.text(x[mintemp], data0[mintemp], u'%2.1f°C' % data0[mintemp])
    if edge < maxtemp < len(data0) - edge - 1:
        ax.text(x[maxtemp], data0[maxtemp], u'%2.1f°C' % data0[maxtemp])
    return x, data0


@view_config(route_name='graph')
def graph_view(request):
    try:
        fig = plot.figure()
        ax = fig.add_subplot(111)
        plot_data(request, ax, 0)
        plot_data(request, ax, 1)
        ax.set_xlabel("Time")
        ax.set_ylabel(u"Temperature (°C)")
        fig.autofmt_xdate()
        imgdata = StringIO.StringIO()
        fig.savefig(imgdata, format='svg')
        return Response(imgdata.getvalue(), content_type='image/svg+xml')
    except DBAPIError:
        conn_err_msg = """\
<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
  "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg width="10cm" height="10cm" viewBox="0 0 100 300"
     xmlns="http://www.w3.org/2000/svg" version="1.1">
  <desc>Database connection error message</desc>
  <text x="0" y="0" fill="red">Database error.</text>
  </svg>"""
        return Response(conn_err_msg, content_type='image/svg+xml', status_int=500)
    finally:
        plot.close('all')

conn_err_msg = """\
Pyramid is having a problem using your SQL database.  The problem
might be caused by one of the following things:

1.  You may need to run the "initialize_boilerweb_db" script
    to initialize your database tables.  Check your virtual
    environment's "bin" directory for this script and try to run it.

2.  Your database server may not be running.  Check that the
    database server referred to by the "sqlalchemy.url" setting in
    your "development.ini" file is running.

After you fix the problem, please restart the Pyramid application to
try it again.
"""
