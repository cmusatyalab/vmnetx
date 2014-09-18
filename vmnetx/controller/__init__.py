#
# vmnetx.controller - Interfaces for VM execution
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

import errno
from functools import wraps
import glib
import gobject
import os
import socket
import sys
from urllib import pathname2url, url2pathname
from urlparse import urlsplit, urlunsplit

from ..reference import PackageReference, BadReferenceError
from ..util import ErrorBuffer, RangeConsolidator

class MachineExecutionError(Exception):
    pass


class MachineStateError(Exception):
    pass


class Controller(gobject.GObject):
    __gsignals__ = {
        'startup-progress': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_UINT64)),
        'startup-rejected-memory': (gobject.SIGNAL_RUN_LAST,
                gobject.TYPE_NONE, ()),
        'startup-failed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (ErrorBuffer,)),
        'vm-started': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
        'vm-stopped': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'network-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'network-reconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'fatal-error': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (ErrorBuffer,)),
    }

    STATE_UNINITIALIZED = 0
    STATE_STOPPED = 1
    STATE_STARTING = 2
    STATE_RUNNING = 3
    STATE_STOPPING = 4
    STATE_DESTROYED = 5

    def __init__(self):
        gobject.GObject.__init__(self)

        # Publicly readable
        self.vm_name = None
        self.state = self.STATE_UNINITIALIZED
        self.is_remote = False
        self.viewer_password = None
        self.max_mouse_rate = None
        self.disk_chunk_size = None
        self.disk_chunks = ChunkStateArray()
        self.disk_stats = {}

        # Publicly writable
        self.scheme = None
        self.username = None
        self.password = None

    @classmethod
    def get_for_ref(cls, package_ref):
        # package_ref can be:
        # - local path or file URL to .netx file
        # - local path or file/http/https/vmnetx+http/vmnetx+https URL to
        #   .nxpk file
        # - vmnetx URL

        # Check for local file path or file URL.
        url = package_ref
        parsed = urlsplit(url)
        # With absolute paths on Windows, the drive letter is parsed into the
        # scheme field
        if (parsed.scheme == '' or
                (sys.platform == 'win32' and len(parsed.scheme) == 1)):
            local_path = url
        elif parsed.scheme == 'file':
            local_path = url2pathname(parsed.path)
        else:
            local_path = None

        # Perform URL substitutions.
        if local_path:
            # Local file; try to parse as package reference.
            try:
                url = PackageReference.parse(local_path).url
            except BadReferenceError:
                # Failed.  Assume it's a package.
                if local_path == url:
                    # We weren't given a URL, so make one.
                    url = urlunsplit(('file', '',
                            pathname2url(os.path.abspath(local_path)),
                            '', ''))
        elif parsed.scheme in ('vmnetx+http', 'vmnetx+https'):
            # Drop "vmnetx+" from the scheme
            url = url.replace('vmnetx+', '', 1)

        # Return correct controller
        parsed = urlsplit(url)
        try:
            if parsed.scheme == 'vmnetx':
                category = 'Remote'
                from .remote import RemoteController
                return RemoteController(url=url)
            else:
                category = 'Local'
                from .local import LocalController
                return LocalController(url=url)
        except ImportError:
            raise MachineExecutionError(('%s execution of virtual machines ' +
                    'is not supported on this system') % category)

    def initialize(self):
        raise NotImplementedError

    @classmethod
    def setup_environment(cls):
        pass

    def start_vm(self):
        raise NotImplementedError

    def connect_viewer(self, callback):
        '''Create a new connection for the SPICE viewer without blocking.
        When done, call callback(sock=sock) on success or
        callback(error=string) on error.'''
        raise NotImplementedError

    def stop_vm(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError

    @staticmethod
    def _connect_socket(address, callback):
        def ready(sock, cond):
            if not cond & (glib.IO_OUT | glib.IO_ERR):
                return True
            # Get error code
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                # os.strerror() can't convert WinSock error codes, but the
                # socket module has an undocumented mapping table
                errortab = getattr(socket, 'errorTab', {})
                callback(error=errortab.get(err, os.strerror(err)))
                sock.close()
            else:
                sock.setblocking(1)
                callback(sock=sock)
            return False

        try:
            sock = socket.socket()
            sock.setblocking(0)
            sock.connect(address)
        except socket.error, e:
            # EWOULDBLOCK on Windows (actually WSAEWOULDBLOCK)
            if e.errno in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                # IO_ERR on Windows
                glib.io_add_watch(sock, glib.IO_OUT | glib.IO_ERR, ready)
            else:
                callback(error=str(e))
                sock.close()
        else:
            sock.setblocking(1)
            callback(sock=sock)

    @staticmethod
    def _ensure_state(state):
        def decorator(func):
            @wraps(func)
            def wrapper(self, *args, **kwargs):
                if self.state != state:
                    raise MachineStateError('Machine in inappropriate state')
                return func(self, *args, **kwargs)
            return wrapper
        return decorator
gobject.type_register(Controller)


# pylint thinks Controller is only subclassed once.  Perhaps it's being
# confused by conditional imports?
class _DummyControllerSubclass(Controller):
    def initialize(self):
        raise ValueError

    def start_vm(self):
        raise ValueError

    def connect_viewer(self, _callback):
        raise ValueError

    def stop_vm(self):
        raise ValueError

    def shutdown(self):
        raise ValueError


class Statistic(gobject.GObject):
    __gsignals__ = {
        'stat-changed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING, gobject.TYPE_UINT64)),
    }

    def __init__(self, name):
        gobject.GObject.__init__(self)
        self.name = name
        self._value = 0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        self.emit('stat-changed', self.name, value)
gobject.type_register(Statistic)


class ChunkStateArray(gobject.GObject):
    INVALID = 0  # Beyond EOF.  Never stored in chunks array.
    MISSING = 1
    CACHED = 2
    ACCESSED = 3
    MODIFIED = 4
    ACCESSED_MODIFIED = 5

    __gsignals__ = {
        'chunk-state-changed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_UINT64)),
        'image-resized': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64,)),
    }

    def __init__(self):
        gobject.GObject.__init__(self)
        self._chunks = []

    def __len__(self):
        return len(self._chunks)

    def __getitem__(self, key):
        return self._chunks.__getitem__(key)

    def _ensure_size(self, chunks):
        """Ensure the image is at least @chunks chunks long."""
        current = len(self._chunks)
        if chunks > current:
            self._chunks.extend([self.MISSING] * (chunks - current))
            self.emit('image-resized', chunks)
            self.emit('chunk-state-changed', current, chunks - 1)

    def set_size(self, chunks):
        current = len(self._chunks)
        if chunks < current:
            del self._chunks[chunks:]
            self.emit('image-resized', chunks)
            self.emit('chunk-state-changed', chunks, current - 1)
        else:
            self._ensure_size(chunks)

    def update_chunks(self, state, first, last):
        # We may be notified of a chunk beyond the current EOF before we
        # are notified that the image has been resized.
        self._ensure_size(last + 1)
        def emit(first, last):
            self.emit('chunk-state-changed', first, last)
        with RangeConsolidator(emit) as c:
            for chunk in xrange(first, last + 1):
                cur_state = self._chunks[chunk]
                if ((cur_state == self.ACCESSED and
                        state == self.MODIFIED) or
                        (cur_state == self.MODIFIED and
                        state == self.ACCESSED)):
                    state = self.ACCESSED_MODIFIED
                if cur_state < state:
                    self._chunks[chunk] = state
                    c.emit(chunk)
gobject.type_register(ChunkStateArray)
