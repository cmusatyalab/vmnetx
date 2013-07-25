#
# vmnetx.controller.remote - Remote execution of a VM
#
# Copyright (C) 2008-2013 Carnegie Mellon University
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

import gobject
import gtk
import logging
import socket
from urlparse import urlsplit

from . import Controller, MachineExecutionError, MachineStateError
from ..protocol import ClientEndpoint
from ..util import ErrorBuffer

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

    def _auth_ok(self, _endp, state, _name):
        if state == 'running':
            self._endp.send_attach_viewer()
        else:
            self._fail('Server in unexpected state: %s' % state)

    def _auth_failed(self, _endp, error):
        self._fail('Authentication failed: %s' % error)

    def _attaching_viewer(self, _endp):
        if self._callback is None:
            return
        a, b = socket.socketpair()
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
            gtk.main()
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
            gtk.main_quit()


class RemoteController(Controller):
    DEFAULT_PORT = 18923

    # pylint is confused by named tuples
    # pylint: disable=E1103
    def __init__(self, url, use_spice):
        Controller.__init__(self)
        if not use_spice:
            raise MachineExecutionError(
                    'Remote VM access requires SPICE support')
        self.is_remote = True

        parsed = urlsplit(url)
        if parsed.scheme != 'vmnetx':
            raise MachineExecutionError('Unsupported URI scheme')
        self._address = (parsed.hostname, parsed.port or self.DEFAULT_PORT)
        self.viewer_password = parsed.path.lstrip('/')

        self._endp = None
        self._handlers = []
        self._closed = False
    # pylint: enable=E1103

    def initialize(self):
        handlers = []
        with _TemporaryMainLoop() as loop:
            def auth_ok(_endp, state, name):
                self.state = (
                        self.STATE_STARTING if state == 'starting' else
                        self.STATE_RUNNING if state == 'running' else
                        self.STATE_STOPPING if state == 'stopping' else
                        self.STATE_STOPPED)
                self.vm_name = name
                # Rebind signal handlers
                for handler in handlers:
                    self._endp.disconnect(handler)
                def connect(signal, handler):
                    self._handlers.append(self._endp.connect(signal, handler))
                connect('startup-progress', self._startup_progress)
                connect('startup-complete', self._startup_complete)
                connect('startup-cancelled', self._startup_cancelled)
                connect('startup-rejected-memory',
                        self._startup_rejected_memory)
                connect('startup-failed', self._startup_failed)
                connect('vm-stopped', self._vm_stopped)
                connect('error', self._error)
                connect('close', self._shutdown)
                loop.quit()

            def auth_failed(_endp, error):
                loop.fail(error)
                self._endp.shutdown()

            def conn_error(_endp, message):
                loop.fail('Protocol error: %s' % message)
                self._endp.shutdown()

            def shutdown(_endp):
                self._closed = True
                loop.fail('Control connection closed')

            def connected(sock=None, error=None):
                assert sock is not None or error is not None
                if error is not None:
                    loop.fail(error)
                else:
                    self._endp = ClientEndpoint(sock)
                    def connect(signal, handler):
                        handlers.append(self._endp.connect(signal, handler))
                    connect('auth-ok', auth_ok)
                    connect('auth-failed', auth_failed)
                    connect('error', conn_error)
                    connect('close', shutdown)
                    self._endp.send_authenticate(self.viewer_password)

            self._connect_socket(self._address, connected)

        # Connected.  Kick off state machine when main loop starts.
        if self.state == self.STATE_STOPPED:
            gobject.idle_add(self.emit, 'vm-stopped')
        elif self.state == self.STATE_RUNNING:
            gobject.idle_add(self.emit, 'startup-complete')

    def _startup_progress(self, _endp, fraction):
        self.emit('startup-progress', int(fraction * 10000), 10000)

    def _startup_complete(self, _endp):
        self.state = self.STATE_RUNNING
        self.emit('startup-complete')

    def _startup_cancelled(self, _endp):
        self.state = self.STATE_STOPPED
        self.emit('startup-cancelled')

    def _startup_rejected_memory(self, _endp):
        self.emit('startup-rejected-memory')

    def _startup_failed(self, _endp, message):
        self.state = self.STATE_STOPPED
        self.emit('startup-failed', ErrorBuffer(message))

    def _vm_stopped(self, _endp):
        self.state = self.STATE_STOPPED
        self.emit('vm-stopped')

    def _error(self, _endp, message):
        self.emit('fatal-error', ErrorBuffer('Protocol error: %s' % message))
        self._endp.shutdown()

    def _shutdown(self, _endp):
        self.emit('fatal-error', ErrorBuffer('Control connection closed'))
        self._closed = True

    @Controller._ensure_state(Controller.STATE_STOPPED)
    def start_vm(self):
        self.state = self.STATE_STARTING
        self._endp.send_start_vm()

    def startup_cancel(self):
        if self.state == self.STATE_STARTING:
            self.state = self.STATE_STOPPING
            self._endp.send_startup_cancel()
        elif self.state != self.STATE_STOPPING:
            raise MachineStateError('Machine in inappropriate state')

    @Controller._ensure_state(Controller.STATE_RUNNING)
    def connect_viewer(self, callback):
        def connected(sock=None, error=None):
            assert sock is not None or error is not None
            if error is not None:
                callback(error=error)
            else:
                _ViewerConnection(sock, self.viewer_password, callback)
        self._connect_socket(self._address, connected)

    def stop_vm(self):
        if (self.state == self.STATE_STARTING or
                self.state == self.STATE_RUNNING):
            self._endp.send_stop_vm()

    def shutdown(self):
        if self._endp is None:
            return

        # Rebind handlers
        for handler in self._handlers:
            self._endp.disconnect(handler)
        self._handlers = []

        if not self._closed:
            with _TemporaryMainLoop() as loop:
                def shutdown(_endp):
                    loop.quit()
                self._endp.connect('close', shutdown)
                self.stop_vm()
                self._endp.shutdown()
gobject.type_register(RemoteController)
