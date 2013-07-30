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
import errno
import glib
import gobject
import logging
import socket
from urlparse import urlunsplit

from ..controller import Controller, MachineExecutionError, MachineStateError
from ..controller.local import LocalController
from ..package import Package
from ..protocol import ServerEndpoint

_log = logging.getLogger(__name__)


class _ServerConnection(gobject.GObject):
    __gsignals__ = {
        'close': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, controller, sock):
        gobject.GObject.__init__(self)
        self._controller = controller
        self._endp = ServerEndpoint(sock)
        self._endp.connect('authenticate', self._client_authenticate)
        self._endp.connect('attach-viewer', self._client_attach_viewer)
        self._endp.connect('start-vm', self._client_start_vm)
        self._endp.connect('startup-cancel', self._client_startup_cancel)
        self._endp.connect('stop-vm', self._client_stop_vm)
        self._endp.connect('error', self._client_error)
        self._endp.connect('close', self._client_shutdown)
        self._controller_sources = []

    def shutdown(self):
        self._endp.shutdown()

    def _client_authenticate(self, _endp, token):
        if token != self._controller.viewer_password:
            self._endp.send_auth_failed('Authentication failed')
            return True

        # Now we can start forwarding controller signals.  We disconnect
        # from the controller at shutdown to avoid leaking _ServerConnection
        # objects.
        def connect(signal, handler):
            source = self._controller.connect(signal, handler)
            self._controller_sources.append(source)
        connect('startup-progress', self._ctrl_startup_progress)
        connect('startup-complete', self._ctrl_startup_complete)
        connect('startup-cancelled', self._ctrl_startup_cancelled)
        connect('startup-rejected-memory', self._ctrl_startup_rejected_memory)
        connect('startup-failed', self._ctrl_startup_failed)
        connect('vm-stopped', self._ctrl_vm_stopped)

        cs = self._controller.state
        state = ('stopped' if cs == LocalController.STATE_STOPPED else
                'starting' if cs == LocalController.STATE_STARTING else
                'running' if cs == LocalController.STATE_RUNNING else
                'stopping' if cs == LocalController.STATE_STOPPING else
                'unknown')
        self._endp.send_auth_ok(state, self._controller.vm_name)
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

    def _client_startup_cancel(self, _endp):
        try:
            self._controller.startup_cancel()
            _log.info('Cancelling startup')
        except MachineStateError:
            self._endp.send_error("Can't cancel startup unless VM is starting")
        return True

    def _client_stop_vm(self, _endp):
        try:
            self._controller.stop_vm()
            _log.info('Stopping VM')
        except MachineStateError:
            self._endp.send_error("Can't stop VM unless it is running")
        return True

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

    def _ctrl_startup_complete(self, _obj):
        self._endp.send_startup_complete()

    def _ctrl_startup_cancelled(self, _obj):
        self._endp.send_startup_cancelled()

    def _ctrl_startup_rejected_memory(self, _obj):
        self._endp.send_startup_rejected_memory()

    def _ctrl_startup_failed(self, _obj, error):
        self._endp.send_startup_failed(error.exception)

    def _ctrl_vm_stopped(self, _obj):
        self._endp.send_vm_stopped()
gobject.type_register(_ServerConnection)


class VMNetXServer(object):
    DEFAULT_PORT = 18923

    def __init__(self, package_ref, host=None, port=None, scheme=None,
            username=None, password=None, **_kwargs):
        gobject.threads_init()

        hostname = host
        if host is None:
            host = '0.0.0.0'
            hostname = socket.getfqdn()
        if port is None:
            port = self.DEFAULT_PORT
        else:
            hostname += ':%d' % port

        # The package reference must be a url to the VM
        package = Package(package_ref, username=username, password=password,
                scheme=scheme)
        self._controller = LocalController(package=package, use_spice=True)
        self._controller.setup_environment()
        self._controller.username = username
        self._controller.password = password
        self._controller.initialize()

        try:
            if not self._controller.use_spice:
                raise MachineExecutionError('SPICE support is unavailable')
            self.url = urlunsplit(('vmnetx', hostname,
                    '/' + self._controller.viewer_password, '', ''))

            self._listen = socket.socket()
            self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listen.bind((host, port))
            self._listen.listen(16)
            self._listen.setblocking(0)
            self._listen_source = glib.io_add_watch(self._listen, glib.IO_IN,
                    self._accept)

            self._conns = set()
        except:
            self._controller.shutdown()
            raise

    def _accept(self, _source, _cond):
        while True:
            try:
                sock, _addr = self._listen.accept()
            except socket.error, e:
                if e.errno == errno.EAGAIN:
                    return True
                else:
                    _log.exception('Accepting connection')
            conn = _ServerConnection(self._controller, sock)
            conn.connect('close', self._close)
            self._conns.add(conn)

    def _close(self, conn):
        self._conns.remove(conn)

    def shutdown(self):
        glib.source_remove(self._listen_source)
        self._listen.close()
        conns = list(self._conns)
        for conn in conns:
            conn.shutdown()
        self._controller.shutdown()
