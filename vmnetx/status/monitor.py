#
# vmnetx.status.monitor - Track VMNetX disk and memory state
#
# Copyright (C) 2008-2012 Carnegie Mellon University
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
import glib
import gobject
import io
import os

class _Monitor(gobject.GObject):
    def close(self):
        raise NotImplementedError()
gobject.type_register(_Monitor)


class _StatMonitor(_Monitor):
    def __init__(self, name, path):
        _Monitor.__init__(self)
        self.name = name
        self.value = None
        self._path = path
        self._fh = None
        self._source = None
        self._read()

    def _read(self):
        try:
            self._fh = io.open(self._path)
        except IOError:
            # Stop accessing this stat
            return
        self.value = self._process_value(self._fh.readline().strip())
        self.emit('stat-changed', self.name, self.value)
        self._source = glib.io_add_watch(self._fh, glib.IO_IN, self._reread)

    def _process_value(self, value):
        raise NotImplementedError()

    def _reread(self, _fh, _condition):
        self.close()
        self._read()
        return True

    def close(self):
        if self._fh and not self._fh.closed:
            glib.source_remove(self._source)
            self._fh.close()

    @classmethod
    def open_all(cls, image_path):
        stats = {}
        stats.update(_IntStatMonitor.open_all(image_path))
        return stats
gobject.type_register(_StatMonitor)


class _IntStatMonitor(_StatMonitor):
    STATS = ('bytes_read', 'bytes_written', 'chunk_dirties', 'chunk_fetches')

    __gsignals__ = {
        'stat-changed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING, gobject.TYPE_UINT64)),
    }

    def _process_value(self, value):
        return int(value)

    @classmethod
    def open_all(cls, image_path):
        return dict((name, cls(name, os.path.join(image_path, 'stats', name)))
                for name in cls.STATS)
gobject.type_register(_IntStatMonitor)


# To avoid pylint R0922.  Remove when a second subclass of _StatMonitor
# is added.
class _DummyStatMonitor(_StatMonitor):
    def _process_value(self, value):
        return value


class _ChunkStreamMonitor(_Monitor):
    __gsignals__ = {
        'chunk-emitted': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64,)),
    }

    def __init__(self, path):
        _Monitor.__init__(self)
        # We need to set O_NONBLOCK in open() because FUSE doesn't pass
        # through fcntl()
        self._fh = io.FileIO(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        self._source = glib.io_add_watch(self._fh, glib.IO_IN, self._read)
        self._buf = ''
        # Defer initial update until requested by caller, to allow the
        # caller to connect to our signal

    def _read(self, _fh=None, _condition=None):
        buf = self._fh.read()
        if buf == '':
            # EOF
            self.close()
        elif buf is not None:
            # We got some output
            lines = (self._buf + buf).split('\n')
            # Save partial last line, if any
            self._buf = lines.pop()
            for line in lines:
                self.emit('chunk-emitted', int(line))
        return True

    def update(self):
        self._read()

    def close(self):
        if not self._fh.closed:
            glib.source_remove(self._source)
            self._fh.close()
gobject.type_register(_ChunkStreamMonitor)


class ChunkMapMonitor(_Monitor):
    INVALID = 0  # Beyond EOF.  Never stored in chunks array.
    MISSING = 1
    CACHED = 2
    ACCESSED = 3
    MODIFIED = 4

    STREAMS = {
        CACHED: 'chunks_cached',
        ACCESSED: 'chunks_accessed',
        MODIFIED: 'chunks_modified',
    }

    __gsignals__ = {
        'chunk-state-changed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_INT)),
    }

    def __init__(self, image_path):
        _Monitor.__init__(self)
        self._monitors = []

        chunks = _IntStatMonitor('chunks',
                os.path.join(image_path, 'stats', 'chunks'))
        chunks.connect('stat-changed', self._resize_image)
        self._monitors.append(chunks)
        self.chunks = [self.MISSING] * chunks.value

        for state, name in self.STREAMS.iteritems():
            m = _ChunkStreamMonitor(os.path.join(image_path, 'streams', name))
            m.connect('chunk-emitted', self._update_chunk, state)
            m.update()
            self._monitors.append(m)

    def _ensure_size(self, chunks):
        """Ensure the image is at least @chunks chunks long."""
        current = len(self.chunks)
        if chunks > current:
            self.chunks.extend([self.MISSING] * (chunks - current))
            for chunk in xrange(current, chunks):
                self.emit('chunk-state-changed', chunk, self.chunks[chunk])

    def _resize_image(self, _monitor, _name, chunks):
        current = len(self.chunks)
        if chunks < current:
            del self.chunks[chunks:]
            for chunk in xrange(chunks, current):
                self.emit('chunk-state-changed', chunk, self.INVALID)
        else:
            self._ensure_size(chunks)

    def _update_chunk(self, _monitor, chunk, state):
        # We may be notified of a chunk beyond the current EOF before we
        # are notified that the image has been resized.
        self._ensure_size(chunk + 1)
        if self.chunks[chunk] < state:
            self.chunks[chunk] = state
            self.emit('chunk-state-changed', chunk, state)

    def close(self):
        for m in self._monitors:
            m.close()
        self._monitors = []
gobject.type_register(ChunkMapMonitor)


class ImageMonitor(_Monitor):
    def __init__(self, image_path):
        _Monitor.__init__(self)
        self.chunk_size = self._read_stat(image_path, 'chunk_size')
        self.chunk_map = ChunkMapMonitor(image_path)
        self.stats = _StatMonitor.open_all(image_path)

    def _read_stat(self, image_path, name):
        path = os.path.join(image_path, 'stats', name)
        with io.open(path) as fh:
            return int(fh.readline().strip())

    def close(self):
        self.chunk_map.close()
        for s in self.stats.values():
            s.close()
        self.stats = {}
gobject.type_register(ImageMonitor)
