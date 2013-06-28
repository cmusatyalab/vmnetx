#
# vmnetx.controller.local.monitor - Track vmnetfs disk and memory state
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

from __future__ import division
import glib
import gobject
import io
import os

from .. import ChunkStateArray, Statistic
from ...util import RangeConsolidator

class _Monitor(gobject.GObject):
    def close(self):
        raise NotImplementedError()
gobject.type_register(_Monitor)


class StatMonitor(_Monitor):
    def __init__(self, reporter, image_path, name):
        _Monitor.__init__(self)
        self._reporter = reporter
        self._path = os.path.join(image_path, 'stats', name)
        self._fh = None
        self._source = None
        self._read()

    def _read(self):
        try:
            self._fh = io.open(self._path)
        except IOError:
            # Stop accessing this stat
            return
        value = int(self._fh.readline().strip())
        if value != self._reporter.value:
            self._reporter.value = value
        self._source = glib.io_add_watch(self._fh, glib.IO_IN | glib.IO_ERR,
                self._reread)

    def _reread(self, _fh, _condition):
        self.close()
        self._read()
        return True

    def close(self):
        if self._fh and not self._fh.closed:
            glib.source_remove(self._source)
            self._fh.close()
gobject.type_register(StatMonitor)


class _StreamMonitorBase(_Monitor):
    def __init__(self, path):
        _Monitor.__init__(self)
        # We need to set O_NONBLOCK in open() because FUSE doesn't pass
        # through fcntl()
        self._fh = io.FileIO(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        self._source = glib.io_add_watch(self._fh, glib.IO_IN | glib.IO_ERR,
                self._read)
        self._buf = ''
        # Defer initial update until requested by caller, to allow the
        # caller to connect to our signal

    def _read(self, _fh=None, _condition=None):
        try:
            buf = self._fh.read()
        except IOError:
            # e.g. vmnetfs crashed
            self.close()
            return False

        if buf == '':
            # EOF
            self.close()
            return False
        elif buf is not None:
            # We got some output
            lines = (self._buf + buf).split('\n')
            # Save partial last line, if any
            self._buf = lines.pop()
            # Process lines
            self._handle_lines(lines)
        return True

    def _handle_lines(self, lines):
        raise NotImplementedError()

    def update(self):
        self._read()

    def close(self):
        if not self._fh.closed:
            glib.source_remove(self._source)
            self._fh.close()


class LineStreamMonitor(_StreamMonitorBase):
    __gsignals__ = {
        'line-emitted': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING,)),
    }

    def _handle_lines(self, lines):
        for line in lines:
            self.emit('line-emitted', line)
gobject.type_register(LineStreamMonitor)


class _ChunkStreamMonitor(_StreamMonitorBase):
    __gsignals__ = {
        'chunk-emitted': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_UINT64)),
    }

    def _handle_lines(self, lines):
        def emit_range(first, last):
            self.emit('chunk-emitted', first, last)
        with RangeConsolidator(emit_range) as c:
            for line in lines:
                c.emit(int(line))
gobject.type_register(_ChunkStreamMonitor)


class ChunkMapMonitor(_Monitor):
    STREAMS = {
        ChunkStateArray.CACHED: 'chunks_cached',
        ChunkStateArray.ACCESSED: 'chunks_accessed',
        ChunkStateArray.MODIFIED: 'chunks_modified',
    }

    def __init__(self, reporter, image_path):
        _Monitor.__init__(self)
        self._reporter = reporter
        self._monitors = []

        reporter = Statistic('chunks')
        reporter.connect('stat-changed', self._resize_image)
        self._monitors.append(StatMonitor(reporter, image_path, 'chunks'))

        for state, name in self.STREAMS.iteritems():
            m = _ChunkStreamMonitor(os.path.join(image_path, 'streams', name))
            m.connect('chunk-emitted', self._update_chunk, state)
            m.update()
            self._monitors.append(m)

    def _resize_image(self, _monitor, _name, chunks):
        self._reporter.set_size(chunks)

    def _update_chunk(self, _monitor, first, last, state):
        self._reporter.update_chunks(state, first, last)

    def close(self):
        for m in self._monitors:
            m.close()
        self._monitors = []
gobject.type_register(ChunkMapMonitor)


class LoadProgressMonitor(_Monitor):
    __gsignals__ = {
        'progress': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT64, gobject.TYPE_UINT64)),
    }

    def __init__(self, image_path):
        _Monitor.__init__(self)
        self.chunks = self._read_stat(image_path, 'chunks')
        self._chunk_size = self._read_stat(image_path, 'chunk_size')
        self._seen = 0
        self._stream = _ChunkStreamMonitor(os.path.join(image_path,
                'streams', 'chunks_accessed'))
        self._stream.connect('chunk-emitted', self._progress)

    def _read_stat(self, image_path, name):
        path = os.path.join(image_path, 'stats', name)
        with io.open(path) as fh:
            return int(fh.readline().strip())

    def _progress(self, _monitor, first, last):
        # We don't keep a bitmap of previously-seen chunks, because we
        # assume that vmnetfs will never emit a chunk twice.  This is true
        # so long as the image is not resized.
        self._seen += last - first + 1
        self.emit('progress', self._seen * self._chunk_size,
                self.chunks * self._chunk_size)

    def close(self):
        self._stream.close()
gobject.type_register(LoadProgressMonitor)
