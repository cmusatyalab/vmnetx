#
# vmnetx.controller.remote - Remote execution of a VM
#
# Copyright (C) 2008-2015 Carnegie Mellon University
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

import logging
from urlparse import urlsplit

import gi
gi.require_version('GObject', '2.0')
gi.require_version('GLib', '2.0')
gi.require_version('Gtk', '3.0')
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk

from . import Controller, MachineExecutionError
from ..protocol import ClientEndpoint, EndpointStateError
from ..util import ErrorBuffer, BackoffTimer, socketpair

_log = logging.getLogger(__name__)


class _ViewerConnection(object):
    def __init__(self, sock, token, callback):
        self._callback = callback
        self._endp = ClientEndpoint(sock)
        self._endp.connect('auth-ok', self._auth_ok)
        self._endp.connect('auth-failed', self._auth_failed)
        self._endp.connect('attaching-viewer', self._attaching_viewer)
        self._endp.connect('error', self._error)
        self._endp.connect('close', self._shutdown)
        self._endp.send_authenticate(token)

    def _auth_ok(self, _endp, state, _name, _max_mouse_rate,
            _server_timeout_min, _server_timeout_max):
        if state == 'running':
            self._endp.send_attach_viewer()
        else:
            self._fail('Server in unexpected state: %s' % state)

    def _auth_failed(self, _endp, error):
        self._fail('Authentication failed: %s' % error)

    def _attaching_viewer(self, _endp):
        if self._callback is None:
            return
        a, b = socketpair()
        self._endp.start_forwarding(a)
        self._callback(sock=b)
        self._callback = None

    def _error(self, _endp, message):
        self._fail('Protocol error: %s' % message)

    def _shutdown(self, _endp):
        self._fail('Connection closed')

    def _fail(self, message):
        if self._callback is not None:
            self._callback(error=message)
            self._callback = None
        self._endp.shutdown()


class _TemporaryMainLoop(object):
    def __init__(self, error_exception=MachineExecutionError):
        self._error_exception = error_exception
        self._error = None
        self.running = False
        self.finished = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Propagate nested exceptions, if any
        if exc_type is not None:
            return False
        # Run gtk main loop
        if not self.finished:
            self.running = True
            Gtk.main()
            self.running = False
        # Propagate saved error from main loop, if any
        if self._error is not None:
            raise self._error_exception(self._error)
        return False

    def fail(self, message):
        if self._error is None:
            self._error = message
        self.quit()

    def quit(self):
        self.finished = True
        if self.running:
            Gtk.main_quit()


class RemoteController(Controller):
    DEFAULT_PORT = 18923

    PHASE_INIT = 0
    PHASE_RUN = 1
    PHASE_STOP = 2

    def __init__(self, url):
        Controller.__init__(self)
        self.is_remote = True

        parsed = urlsplit(url)
        if parsed.scheme != 'vmnetx':
            raise MachineExecutionError('Unsupported URI scheme')
        self._address = (parsed.hostname, parsed.port or self.DEFAULT_PORT)
        self.viewer_password = parsed.path.lstrip('/')

        # Main loop for initialize() and shutdown()
        self._loop = None

        self._phase = self.PHASE_INIT
        self._endp = None
        self._handlers = []
        self._backoff = BackoffTimer()
        self._backoff.connect('attempt', self._attempt_connection)
        self._disconnected_timeout = None # seconds
        self._disconnected_timeout_source = None

    @Controller._ensure_state(Controller.STATE_UNINITIALIZED)
    def initialize(self):
        assert self._phase == self.PHASE_INIT
        with _TemporaryMainLoop() as self._loop:
            # A failed attempt will terminate the main loop, so this will
            # only try once
            self._backoff.attempt()

        # Connected
        self._loop = None
        self._phase = self.PHASE_RUN
        # Kick off state machine when main loop starts.
        self._notify_stable_state()

    def _notify_stable_state(self):
        if self.state == self.STATE_STOPPED:
            GObject.idle_add(self.emit, 'vm-stopped')
        elif self.state == self.STATE_RUNNING:
            GObject.idle_add(self.emit, 'vm-started', False)

    def _attempt_connection(self, _backoff):
        self._connect_socket(self._address, self._connected)

    def _connected(self, sock=None, error=None):
        assert sock is not None or error is not None
        if self._phase == self.PHASE_STOP:
            return
        if error is not None:
            if self._phase == self.PHASE_INIT:
                self._loop.fail(error)
            else:
                self._backoff.attempt()
        else:
            self._backoff.reset()
            self._endp = ClientEndpoint(sock)
            def connect(signal, handler):
                self._handlers.append(self._endp.connect(signal, handler))
            connect('auth-ok', self._auth_ok)
            connect('auth-failed', self._auth_failed)
            connect('startup-progress', self._startup_progress)
            connect('startup-rejected-memory',
                    self._startup_rejected_memory)
            connect('startup-failed', self._startup_failed)
            connect('vm-started', self._vm_started)
            connect('vm-stopped', self._vm_stopped)
            connect('vm-destroyed', self._vm_destroyed)
            connect('error', self._error)
            connect('close', self._shutdown)
            self._endp.send_authenticate(self.viewer_password)

    def _auth_failed(self, _endp, error):
        if self._phase == self.PHASE_INIT:
            self._loop.fail(error)
            self._endp.shutdown()
        elif self._phase == self.PHASE_RUN:
            self.emit('fatal-error',
                    ErrorBuffer('Reauthentication failed: %s' % error))
            self._endp.shutdown()

    def _auth_ok(self, _endp, state, name, max_mouse_rate,
            _server_timeout_min, server_timeout_max):
        if self._phase == self.PHASE_STOP:
            return
        self.state = (self.STATE_STARTING if state == 'starting' else
                self.STATE_RUNNING if state == 'running' else
                self.STATE_STOPPING if state == 'stopping' else
                self.STATE_STOPPED)
        self._endp.start_pinging()
        if self._phase == self.PHASE_INIT:
            self.vm_name = name
            self.max_mouse_rate = max_mouse_rate or None
            self._disconnected_timeout = server_timeout_max or None
            self._loop.quit()
        elif self._phase == self.PHASE_RUN:
            if self._disconnected_timeout_source is not None:
                GLib.source_remove(self._disconnected_timeout_source)
                self._disconnected_timeout_source = None
            self.emit('network-reconnect')
            self._notify_stable_state()

    def _startup_progress(self, _endp, fraction):
        if self._phase == self.PHASE_RUN:
            self.emit('startup-progress', int(fraction * 10000), 10000)

    def _vm_started(self, _endp, check_display):
        if self._phase == self.PHASE_RUN:
            self.state = self.STATE_RUNNING
            self.emit('vm-started', check_display)

    def _startup_rejected_memory(self, _endp):
        if self._phase == self.PHASE_RUN:
            self.emit('startup-rejected-memory')

    def _startup_failed(self, _endp, message):
        if self._phase == self.PHASE_RUN:
            self.emit('startup-failed', ErrorBuffer(message))

    def _vm_stopped(self, _endp):
        if self._phase == self.PHASE_RUN:
            self.state = self.STATE_STOPPED
            self.emit('vm-stopped')

    def _vm_destroyed(self, _endp):
        if self._phase == self.PHASE_RUN:
            self.emit('fatal-error', ErrorBuffer('Virtual machine terminated'))
            self._endp.shutdown()

    def _error(self, _endp, message):
        if self._phase == self.PHASE_INIT:
            self._loop.fail('Protocol error: %s' % message)
            self._endp.shutdown()
        elif self._phase == self.PHASE_RUN:
            self.emit('fatal-error',
                    ErrorBuffer('Protocol error: %s' % message))
            self._endp.shutdown()

    def _shutdown(self, _endp):
        for handler in self._handlers:
            self._endp.disconnect(handler)
        self._handlers = []
        self._endp = None

        if self._phase == self.PHASE_INIT:
            self._loop.fail('Control connection closed')
        elif self._phase == self.PHASE_RUN:
            if (self._disconnected_timeout_source is None and
                    self._disconnected_timeout is not None):
                self._disconnected_timeout_source = GLib.timeout_add_seconds(
                        self._disconnected_timeout, self._reconnection_failed)
            self.emit('network-disconnect')
            self._backoff.attempt()
        elif self._phase == self.PHASE_STOP:
            self._loop.quit()

    def _reconnection_failed(self):
        # We have not been able to reconnect and the server has GC'd our VM.
        self.emit('fatal-error', ErrorBuffer('Lost connection with server'))
        self._backoff.reset()
        if self._endp is not None:
            # Connection in progress but not authenticated
            self._endp.shutdown()

    def _want_state(self, wanted):
        if self._endp is None:
            return

        try:
            # Only handle transitions for which we can usefully issue a
            # command.  Other transitions will be handled by the UI after a
            # subsequent event.
            if wanted == self.STATE_RUNNING:
                if self.state == self.STATE_STOPPED:
                    self.state = self.STATE_STARTING
                    self._endp.send_start_vm()

            elif wanted == self.STATE_STOPPED:
                if (self.state == self.STATE_STARTING or
                        self.state == self.STATE_RUNNING):
                    self.state = self.STATE_STOPPING
                    self._endp.send_stop_vm()

            elif wanted == self.STATE_DESTROYED:
                if self.state != self.STATE_DESTROYED:
                    self.state = self.STATE_DESTROYED
                    self._endp.send_destroy_vm()
        except (EndpointStateError, IOError):
            # Can't send messages right now; drop on floor
            pass

    def start_vm(self):
        self._want_state(self.STATE_RUNNING)

    def connect_viewer(self, callback):
        if self.state != self.STATE_RUNNING:
            callback(error='Machine in inappropriate state')
            return
        def connected(sock=None, error=None):
            assert sock is not None or error is not None
            if error is not None:
                callback(error=error)
            else:
                _ViewerConnection(sock, self.viewer_password, callback)
        self._connect_socket(self._address, connected)

    def stop_vm(self):
        self._want_state(self.STATE_STOPPED)

    def shutdown(self):
        if self._endp is not None:
            self._phase = self.PHASE_STOP
            with _TemporaryMainLoop() as self._loop:
                self._want_state(self.STATE_DESTROYED)
                self._endp.shutdown()
            self._loop = None
        self.state = self.STATE_DESTROYED
GObject.type_register(RemoteController)
