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

import gobject

from ..util import ErrorBuffer, RangeConsolidator

class AbstractController(gobject.GObject):
    __gsignals__ = {
        'startup-progress': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_UINT64)),
        'startup-complete': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'startup-cancelled': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'startup-rejected-memory': (gobject.SIGNAL_RUN_LAST,
                gobject.TYPE_NONE, ()),
        'startup-failed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (ErrorBuffer,)),
    }

    def __init__(self):
        gobject.GObject.__init__(self)

        # Publicly readable
        self.vm_name = None
        self.have_memory = None
        self.use_spice = True
        self.viewer_address = None
        self.viewer_password = None
        self.max_mouse_rate = None
        self.disk_chunk_size = None
        self.disk_chunks = ChunkStateArray()
        self.disk_stats = {}

        # Publicly writable
        self.scheme = None
        self.username = None
        self.password = None

    def initialize(self):
        raise NotImplementedError

    def start_vm(self):
        raise NotImplementedError

    def stop_vm(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError
gobject.type_register(AbstractController)


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


# pylint complains about the missing  __setitem__ and __delitem__, but we
# don't intend to allow array writes
# pylint: disable=R0924
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
# pylint: enable=R0924
