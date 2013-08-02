#
# vmnetx.server - VMNetX thin client server
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

from __future__ import division
import base64
import errno
import glib
import gobject
import logging
import os
import socket
from threading import Thread, Lock
import time

from .http import HttpServer
from ..controller import Controller, MachineExecutionError, MachineStateError
from ..controller.local import LocalController
from ..package import Package
from ..protocol import ServerEndpoint

_log = logging.getLogger(__name__)


class _ServerConnection(gobject.GObject):
    __gsignals__ = {
        'need-controller': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_BOOLEAN,
            (gobject.TYPE_STRING,), gobject.signal_accumulator_true_handled),
        'ping': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'close': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'destroy-token': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, sock):
        gobject.GObject.__init__(self)
        self._controller = None
        self._endp = ServerEndpoint(sock)
        self._endp.connect('authenticate', self._client_authenticate)
        self._endp.connect('attach-viewer', self._client_attach_viewer)
        self._endp.connect('start-vm', self._client_start_vm)
        self._endp.connect('stop-vm', self._client_stop_vm)
        self._endp.connect('destroy-vm', self._client_destroy_vm)
        self._endp.connect('ping', self._client_ping)
        self._endp.connect('error', self._client_error)
        self._endp.connect('close', self._client_shutdown)
        self._controller_sources = []

    def shutdown(self):
        self._endp.shutdown()

    def set_controller(self, controller):
        self._controller = controller

    def _client_authenticate(self, _endp, token):
        if self._controller is not None:
            self._endp.send_error('Already authenticated')
            return True

        self.emit('need-controller', token)
        if self._controller is None:
            self._endp.send_auth_failed('Authentication failed')
            return True

        # Now we can start forwarding controller signals.  We disconnect
        # from the controller at shutdown to avoid leaking _ServerConnection
        # objects.
        def connect(signal, handler):
            source = self._controller.connect(signal, handler)
            self._controller_sources.append(source)
        connect('startup-progress', self._ctrl_startup_progress)
        connect('startup-rejected-memory', self._ctrl_startup_rejected_memory)
        connect('startup-failed', self._ctrl_startup_failed)
        connect('vm-started', self._ctrl_vm_started)
        connect('vm-stopped', self._ctrl_vm_stopped)

        cs = self._controller.state
        state = ('stopped' if cs == LocalController.STATE_STOPPED else
                'starting' if cs == LocalController.STATE_STARTING else
                'running' if cs == LocalController.STATE_RUNNING else
                'stopping' if cs == LocalController.STATE_STOPPING else
                'unknown')
        self._endp.send_auth_ok(state, self._controller.vm_name,
                self._controller.max_mouse_rate)
        _log.info('Authenticated')
        return True

    def _client_attach_viewer(self, _endp):
        def done(sock=None, error=None):
            assert error is not None or sock is not None
            if error is not None:
                self._endp.set_protocol_disabled(False)
                self._endp.send_error("Couldn't connect viewer")
                return True
            self._endp.send_attaching_viewer()
            # Stop forwarding controller signals
            self._disconnect_controller()
            self._endp.start_forwarding(sock)
            _log.info('Attaching viewer')
        self._endp.set_protocol_disabled(True)
        self._controller.connect_viewer(done)
        return True

    def _client_start_vm(self, _endp):
        try:
            self._controller.start_vm()
            _log.info('Starting VM')
        except MachineStateError:
            self._endp.send_error("Can't start VM unless it is stopped")
        return True

    def _client_stop_vm(self, _endp):
        try:
            self._controller.stop_vm()
            _log.info('Stopping VM')
        except MachineStateError:
            self._endp.send_error("Can't stop VM unless it is running")
        return True

    def _client_destroy_vm(self, _endp):
        self.emit('destroy-token')
        _log.info('Destroying VM')
        return True

    def _client_ping(self, _endp):
        self.emit('ping')

    def _client_error(self, _endp, message):
        _log.warning("Protocol error: %s", message)
        self.shutdown()

    def _disconnect_controller(self):
        for source in self._controller_sources:
            self._controller.disconnect(source)
        self._controller_sources = []

    def _client_shutdown(self, _endp):
        self._disconnect_controller()
        self.emit('close')

    def _ctrl_startup_progress(self, _obj, count, total):
        self._endp.send_startup_progress(count / total)

    def _ctrl_startup_rejected_memory(self, _obj):
        self._endp.send_startup_rejected_memory()

    def _ctrl_startup_failed(self, _obj, error):
        self._endp.send_startup_failed(error.exception)

    def _ctrl_vm_started(self, _obj, check_display):
        self._endp.send_vm_started(check_display)

    def _ctrl_vm_stopped(self, _obj):
        self._endp.send_vm_stopped()
gobject.type_register(_ServerConnection)


class _TokenState(gobject.GObject):
    __gsignals__ = {
        'destroy': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, package, username, password):
        # Called from HTTP worker thread
        gobject.GObject.__init__(self)
        self.token = base64.urlsafe_b64encode(os.urandom(15))
        self._package = package
        self._username = username
        self._password = password
        self._controller = None
        self._conns = set()
        self._valid = True
        self.last_seen = time.time()

    def get_controller(self, conn):
        if self._controller is None:
            self._controller = LocalController(package=self._package,
                viewer_password=self.token)
            self._controller.username = self._username
            self._controller.password = self._password
            self._controller.initialize()
            if not self._controller.use_spice:
                raise MachineExecutionError('SPICE support is unavailable')
            self.last_seen = time.time()
        self._conns.add(conn)
        conn.connect('ping', self._update_last_seen)
        conn.connect('close', self._close)
        conn.connect('destroy-token', lambda _conn: self.shutdown())
        return self._controller

    def _update_last_seen(self, _conn):
        self.last_seen = time.time()

    def _close(self, conn):
        self._conns.remove(conn)

    def shutdown(self):
        if self._valid:
            self._valid = False
            conns = list(self._conns)
            for conn in conns:
                conn.shutdown()
            if self._controller is not None:
                self._controller.shutdown()
                self._controller = None
            self.emit('destroy')
gobject.type_register(_TokenState)


class VMNetXServer(object):
    def __init__(self, options):
        glib.threads_init()
        self._options = options
        self._http = None
        self._unauthenticated_conns = set()
        self._listen = None
        self._listen_source = None
        self._gc_timer = None

        # Accessed by HTTP server
        self._lock = Lock()
        self._tokens = {}  # token -> _TokenState

    def initialize(self):
        # Prepare environment for local controllers
        LocalController.setup_environment()

        http_server = HttpServer(self._options, self)
        host = self._options['http_host']
        port = self._options['http_port']
        self._http = Thread(target=http_server.run,
                kwargs={"host": host, "port": port, "threaded": True})
        # The http server should exit when the main thread terminates
        self._http.daemon = True
        self._http.start()

        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind((self._options['host'], self._options['port']))
        self._listen.listen(16)
        self._listen.setblocking(0)
        self._listen_source = glib.io_add_watch(self._listen, glib.IO_IN,
                self._accept)

        # Start garbage collection
        self._gc_timer = glib.timeout_add_seconds(self._options['gc_interval'],
                self._gc)

    def _accept(self, _source, _cond):
        while True:
            try:
                sock, _addr = self._listen.accept()
            except socket.error, e:
                if e.errno == errno.EAGAIN:
                    return True
                else:
                    _log.exception('Accepting connection')
            conn = _ServerConnection(sock)
            conn.connect('need-controller', self._fetch_controller)
            conn.connect('close', self._close)
            self._unauthenticated_conns.add(conn)

    def _close(self, conn):
        try:
            self._unauthenticated_conns.remove(conn)
        except KeyError:
            pass

    def _fetch_controller(self, conn, token):
        _log.debug("Fetching controller for token %s" % token)

        with self._lock:
            try:
                state = self._tokens[token]
            except KeyError:
                return False

            controller = state.get_controller(conn)
            conn.set_controller(controller)
            self._unauthenticated_conns.remove(conn)
            return True

    def create_token(self, package):
        # Called from HTTP worker thread
        with self._lock:
            state = _TokenState(package, self._options['username'],
                    self._options['password'])
            self._tokens[state.token] = state
            state.connect('destroy', self._destroy_token)
        return state.token

    def _destroy_token(self, state):
        with self._lock:
            del self._tokens[state.token]

    def _gc(self):
        _log.debug("GC: Removing stale tokens")
        gc = self._options['gc_interval']
        to = self._options['token_timeout']
        with self._lock:
            # All garbage collection is done with relation to a single start time
            curr = time.time()
            states = self._tokens.values()
            for state in states:
                # Check if the token has not timed out since the last gc call
                if curr > state.last_seen + gc + to:
                    _log.debug('GC: Removing token %s', state.token)
                    state.shutdown()
        return True

    def shutdown(self):
        # Does not shut down web server, since there's no API for doing so
        _log.info("Shutting down VMNetXServer")
        if self._listen_source is not None:
            glib.source_remove(self._listen_source)
            self._listen_source = None
        if self._listen is not None:
            self._listen.close()
            self._listen = None
        if self._gc_timer is not None:
            glib.source_remove(self._gc_timer)
            self._gc_timer = None
        with self._lock:
            states = self._tokens.values()
        for state in states:
            state.shutdown()
        conns = list(self._unauthenticated_conns)
        for conn in conns:
            conn.shutdown()
