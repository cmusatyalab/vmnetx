#
# vmnetx.protocol - Remote control protocol
#
# Copyright (C) 2013 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# COPYING.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

import errno
from functools import wraps
import glib
import gobject
import logging
import msgpack
import socket
import struct

_log = logging.getLogger(__name__)


class _MessageError(Exception):
    pass


class _AsyncSocket(gobject.GObject):
    DEFAULT_RECV_BUF = 65536

    __gsignals__ = {
        'close': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, sock):
        gobject.GObject.__init__(self)
        self._sock = sock
        self._sock.setblocking(0)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except socket.error:
            # sockets produced by socketpair() are AF_UNIX
            pass
        self._source = None

        self._recv_buf = ''
        self._recv_remaining = 0
        self._recv_callback = None
        self._recv_closed = False

        self._send_buf = ''
        self._send_closing = False
        self._send_closed = False

        self._update()

    def _update(self):
        if self._sock is None:
            return
        if (self._send_closing and not self._send_closed and
                not self._send_buf):
            try:
                self._sock.shutdown(socket.SHUT_WR)
            except socket.error:
                pass
            self._send_closed = True
        if self._send_closed and self._recv_closed:
            if self._source is not None:
                glib.source_remove(self._source)
                self._source = None
            self._sock.close()
            self._sock = None
            self.emit('close')
            return
        cond = 0
        if self._recv_callback is not None and not self._recv_closed:
            cond |= glib.IO_IN
        if self._send_buf and not self._send_closed:
            cond |= glib.IO_OUT
        if self._source is not None:
            glib.source_remove(self._source)
        self._source = glib.io_add_watch(self._sock, cond, self._io_ready)

    def _io_ready(self, _source, cond):
        if cond & glib.IO_IN:
            try:
                buf = self._sock.recv(self._recv_remaining or
                        self.DEFAULT_RECV_BUF)
            except socket.error, e:
                if e.errno != errno.EAGAIN:
                    self.shutdown()
            else:
                if buf == '':
                    self.shutdown()
                else:
                    self._recv_buf += buf
                    if self._recv_remaining is not None:
                        self._recv_remaining -= len(buf)
                    if (self._recv_remaining is None or
                            self._recv_remaining == 0):
                        cb = self._recv_callback
                        self._recv_callback = None
                        cb(self._recv_buf)
                        self._recv_buf = ''
                        self._update()

        if cond & glib.IO_OUT:
            try:
                count = self._sock.send(self._send_buf)
            except socket.error:
                self._send_closed = True
                self._send_buf = ''
                self._update()
            else:
                self._send_buf = self._send_buf[count:]
                if not self._send_buf:
                    self._update()

        return True

    def send(self, buf):
        if self._send_closing or self._send_closed:
            raise IOError('Socket closed')
        self._send_buf += buf
        self._update()

    def recv(self, callback, count=None):
        '''Call callback when count bytes have been received.  If count is
        None, call when any bytes have been received.'''
        if self._recv_callback is not None:
            raise ValueError('Callback already registered')
        if self._recv_closed:
            raise IOError('Socket closed')
        self._recv_remaining = count
        self._recv_callback = callback
        self._update()

    def shutdown(self):
        if not self._recv_closed:
            self._recv_closed = True
            try:
                self._sock.shutdown(socket.SHUT_RD)
            except socket.error:
                pass

        # Defer SHUT_WR until send buffer is empty
        self._send_closing = True

        self._update()
gobject.type_register(_AsyncSocket)


class _Endpoint(gobject.GObject):
    LENGTH_FMT = '!I'
    MAX_MESSAGE_SIZE = 1 << 20

    __gsignals__ = {
        'error': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING,)),
        'close': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, sock):
        gobject.GObject.__init__(self)
        self._asock = _AsyncSocket(sock)
        self._asock.connect('close', self._shutdown)
        self._protocol_disabled = False
        self._peer = None
        self._open_sockets = 1
        self._next_message()

    def _next_message(self):
        if self._asock is not None and not self._protocol_disabled:
            self._asock.recv(self._recv_length,
                    struct.calcsize(self.LENGTH_FMT))

    def _recv_length(self, buf):
        count = struct.unpack(self.LENGTH_FMT, buf)[0]
        if count > self.MAX_MESSAGE_SIZE:
            _log.warning('Received oversized message of length %d', count)
            self.send_error('Message too large')
            self._recv_overflow(count)
        else:
            self._asock.recv(self._recv, count)

    def _recv(self, buf):
        try:
            try:
                # use_list default has changed over time
                msg = msgpack.unpackb(buf, use_list=False)
                mtype = msg.pop('_')
                _log.debug('Received: %s', mtype)
            except (AttributeError, KeyError, ValueError), e:
                raise _MessageError('Invalid message')

            self._dispatch(mtype, msg)
        except _MessageError, e:
            _log.debug('Error: %s', e)
            self.send_error(str(e))
        finally:
            self._next_message()

    def _recv_overflow(self, remaining):
        if remaining > 0:
            next = min(self.MAX_MESSAGE_SIZE, remaining)
            self._asock.recv(lambda _: self._recv_overflow(remaining - next),
                    next)
        else:
            self._next_message()

    def _transmit(self, mtype, **kwargs):
        _log.debug('Sent: %s', mtype)
        kwargs['_'] = mtype
        buf = msgpack.packb(kwargs)
        self._asock.send(struct.pack(self.LENGTH_FMT, len(buf)))
        self._asock.send(buf)

    def _dispatch(self, mtype, msg):
        try:
            if mtype == 'error':
                self.emit('error', msg['message'])

            else:
                raise _MessageError('Unknown message type')

        except KeyError, e:
            raise _MessageError('Missing field in message: %s' % e)

    def set_protocol_disabled(self, disabled):
        if self._protocol_disabled == disabled:
            return
        self._protocol_disabled = disabled
        if not disabled:
            self._next_message()

    def start_forwarding(self, peer):
        self.set_protocol_disabled(True)
        self._peer = _AsyncSocket(peer)
        self._peer.connect('close', self._shutdown)
        self._open_sockets += 1
        self._asock.recv(lambda buf: self._forward(self._asock, self._peer,
                buf))
        self._peer.recv(lambda buf: self._forward(self._peer, self._asock,
                buf))

    def _forward(self, src, dest, buf):
        dest.send(buf)
        src.recv(lambda buf: self._forward(src, dest, buf))

    def send_error(self, message):
        self._transmit('error', message=message)

    def _shutdown(self, _asock):
        self.shutdown()
        self._open_sockets -= 1
        if self._open_sockets == 0:
            self.emit('close')

    def shutdown(self):
        asock = self._asock
        peer = self._peer
        self._asock = self._peer = None
        if peer is not None:
            peer.shutdown()
        if asock is not None:
            asock.shutdown()
gobject.type_register(_Endpoint)


class ServerEndpoint(_Endpoint):
    __gsignals__ = {
        'authenticate': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN,
                (gobject.TYPE_STRING,),
                gobject.signal_accumulator_true_handled),
        'attach-viewer': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN, (),
                gobject.signal_accumulator_true_handled),
        'start-vm': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN, (),
                gobject.signal_accumulator_true_handled),
        'startup-cancel': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN, (),
                gobject.signal_accumulator_true_handled),
        'stop-vm': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN, (),
                gobject.signal_accumulator_true_handled),
    }

    def __init__(self, sock):
        _Endpoint.__init__(self, sock)
        self._authenticated = False

    # Default signal handler for messages that must be handled
    def _fail_if_not_handled(self, _obj):
        self.send_error('Unsupported operation')
    do_authenticate = _fail_if_not_handled
    do_attach_viewer = _fail_if_not_handled
    do_start_vm = _fail_if_not_handled
    do_startup_cancel = _fail_if_not_handled
    do_stop_vm = _fail_if_not_handled

    def _need_auth(self):
        if not self._authenticated:
            raise _MessageError('Not authenticated')

    def _dispatch(self, mtype, msg):
        try:
            if mtype == 'authenticate':
                self.emit('authenticate', msg['token'])

            elif mtype == 'attach-viewer':
                self._need_auth()
                self.emit('attach-viewer')

            elif mtype == 'start-vm':
                self._need_auth()
                self.emit('start-vm')

            elif mtype == 'startup-cancel':
                self._need_auth()
                self.emit('startup-cancel')

            elif mtype == 'stop-vm':
                self._need_auth()
                self.emit('stop-vm')

            else:
                _Endpoint._dispatch(self, mtype, msg)

        except KeyError, e:
            raise _MessageError('Missing field in %s message: %s' % (mtype, e))

    def send_auth_ok(self, state, name):
        self._authenticated = True
        self._transmit('auth-ok', state=state, name=name)

    def send_auth_failed(self, error=None):
        self._transmit('auth-failed', error=error)

    def send_attaching_viewer(self):
        self._transmit('attaching-viewer')

    def send_startup_progress(self, fraction):
        self._transmit('startup-progress', fraction=fraction)

    def send_startup_complete(self, check_display):
        self._transmit('startup-complete', check_display=check_display)

    def send_startup_cancelled(self):
        self._transmit('startup-cancelled')

    def send_startup_rejected_memory(self):
        self._transmit('startup-rejected-memory')

    def send_startup_failed(self, message):
        self._transmit('startup-failed', message=message)

    def send_vm_stopped(self):
        self._transmit('vm-stopped')
gobject.type_register(ServerEndpoint)


class ClientEndpoint(_Endpoint):
    STATE_UNAUTHENTICATED = 0
    STATE_AUTHENTICATING = 1
    STATE_RUNNING = 2
    STATE_ATTACHING_VIEWER = 3
    STATE_VIEWER = 4

    __gsignals__ = {
        'auth-ok': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING, gobject.TYPE_STRING)),
        'auth-failed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING,)),
        'attaching-viewer': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'startup-progress': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_DOUBLE,)),
        'startup-complete': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
        'startup-cancelled': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'startup-rejected-memory': (gobject.SIGNAL_RUN_LAST,
                gobject.TYPE_NONE, ()),
        'startup-failed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING,)),
        'vm-stopped': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, sock):
        _Endpoint.__init__(self, sock)
        self._state = self.STATE_UNAUTHENTICATED

    def _need_dispatch_state(self, state):
        if self._state != state:
            raise _MessageError('Invalid state for operation')

    def _dispatch(self, mtype, msg):
        try:
            if mtype == 'auth-ok':
                self._need_dispatch_state(self.STATE_AUTHENTICATING)
                self._state = self.STATE_RUNNING
                self.emit('auth-ok', msg['state'], msg['name'])

            elif mtype == 'auth-failed':
                self._need_dispatch_state(self.STATE_AUTHENTICATING)
                self._state = self.STATE_UNAUTHENTICATED
                self.emit('auth-failed', msg['error'])

            elif mtype == 'attaching-viewer':
                self._need_dispatch_state(self.STATE_ATTACHING_VIEWER)
                self._state = self.STATE_VIEWER
                self.emit('attaching-viewer')

            elif mtype == 'startup-progress':
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('startup-progress', msg['fraction'])

            elif mtype == 'startup-complete':
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('startup-complete', msg['check_display'])

            elif mtype == 'startup-cancelled':
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('startup-cancelled')

            elif mtype == 'startup-rejected-memory':
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('startup-rejected-memory')

            elif mtype == 'startup-failed':
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('startup-failed', msg['message'])

            elif mtype == 'vm-stopped':
                if self._state == self.STATE_ATTACHING_VIEWER:
                    # Could happen on viewer connections while the setup
                    # handshake is running
                    return
                self._need_dispatch_state(self.STATE_RUNNING)
                self.emit('vm-stopped')

            else:
                _Endpoint._dispatch(self, mtype, msg)

        except KeyError, e:
            raise _MessageError('Missing field in message: %s' % e)

    # We're accessing a protected member of this class, but pylint doesn't
    # know that.
    # This is a decorator, it doesn't take "self".
    # pylint: disable=W0212,E0213
    def _need_send_state(state):
        def decorator(func):
            @wraps(func)
            def wrapper(self, *args, **kwargs):
                if self._state != state:
                    raise ValueError('Sending client message from ' +
                            'invalid state')
                return func(self, *args, **kwargs)
            return wrapper
        return decorator
    # pylint: enable=W0212,E0213

    @_need_send_state(STATE_UNAUTHENTICATED)
    def send_authenticate(self, token):
        self._state = self.STATE_AUTHENTICATING
        self._transmit('authenticate', token=token)

    @_need_send_state(STATE_RUNNING)
    def send_attach_viewer(self):
        self._state = self.STATE_ATTACHING_VIEWER
        self._transmit('attach-viewer')

    @_need_send_state(STATE_RUNNING)
    def send_start_vm(self):
        self._transmit('start-vm')

    @_need_send_state(STATE_RUNNING)
    def send_startup_cancel(self):
        self._transmit('startup-cancel')

    @_need_send_state(STATE_RUNNING)
    def send_stop_vm(self):
        self._transmit('stop-vm')
gobject.type_register(ClientEndpoint)
