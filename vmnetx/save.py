#
# vmnetx.save - Handling of session save files (.nxsv)
#
# Copyright (C) 2012-2014 Carnegie Mellon University
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
import os
from tempfile import NamedTemporaryFile
import threading
import zipfile

from .memory import copy_memory
from .util import ensure_ranges_nonoverlapping

DISK_DIRNAME = 'disk'
MEMORY_FILENAME = 'memory.img'


class _SaveProgressWrapper(object):
    def __init__(self, callback, disk_bytes, memory_bytes):
        self._lock = threading.Lock()
        self._cb = callback
        self._disk_count = 0
        self._memory_count = 0
        self._total = disk_bytes + memory_bytes
        self._cb(0)

    def __call__(self, disk=None, memory=None):
        with self._lock:
            self._disk_count = disk or self._disk_count
            self._memory_count = memory or self._memory_count
            self._cb((self._disk_count + self._memory_count) / self._total)


class _SaveMemoryRecompressor(threading.Thread):
    def __init__(self, path, domain_xml, compression, progress):
        threading.Thread.__init__(self, name='vmnetx-recompress-memory')
        self._in_path = path
        self._domain_xml = domain_xml
        self._compression = compression
        self._progress = progress
        self._out_file = None
        self.out_path = None

    # We intentionally catch all exceptions
    # pylint: disable=bare-except
    def run(self):
        try:
            memory_bytes = os.stat(self._in_path).st_size
            def memory_progress(cur, _compressing):
                self._progress(memory=cur * memory_bytes)
            self._out_file = NamedTemporaryFile(prefix='memory-')
            copy_memory(self._in_path, self._out_file.name,
                    xml=self._domain_xml, compression=self._compression,
                    progress=memory_progress)
            self.out_path = self._out_file.name
        except:
            self.close()
    # pylint: enable=bare-except

    def close(self):
        if self._out_file:
            self._out_file.close()
            self._out_file = None


class SaveFile(object):
    MAX_CHUNK = 1 << 20

    def __init__(self):
        raise TypeError('Non-instantiable')

    @classmethod
    def create(cls, out, domain_xml, disk_path, disk_ranges, memory_path=None,
            memory_compression='lzop', progress=lambda progress: None):
        # disk_ranges is a sequence of non-overlapping (offset, length) pairs
        ensure_ranges_nonoverlapping(disk_ranges)

        disk_bytes = sum(range[1] for range in disk_ranges)
        memory_bytes = os.stat(memory_path).st_size if memory_path else 0
        progress = _SaveProgressWrapper(progress, disk_bytes, memory_bytes)
        if memory_path:
            recompressor = _SaveMemoryRecompressor(memory_path, domain_xml,
                    memory_compression, progress)
            recompressor.start()
        else:
            recompressor = None

        temp = NamedTemporaryFile(dir=os.path.dirname(out),
                prefix='.vmnetx-save-', delete=False)
        temp.close()
        zip = None
        try:
            zip = zipfile.ZipFile(temp.name, 'w', zipfile.ZIP_DEFLATED, True)
            zip.comment = 'VMNetX saved session'

            # Store disk chunks
            with open(disk_path, 'rb') as fh:
                progress_bytes = 0
                for offset, length in disk_ranges:
                    fh.seek(offset)
                    while length > 0:
                        buf = fh.read(min(cls.MAX_CHUNK, length))
                        curlen = len(buf)
                        if curlen == 0:
                            raise IOError('Short read from disk image')
                        zip.writestr('%s/%d' % (DISK_DIRNAME, offset), buf)
                        offset += curlen
                        length -= curlen
                        progress_bytes += curlen
                        progress(disk=progress_bytes)

            # Wait for memory image recompression, then copy to zip
            if recompressor:
                recompressor.join()
                if recompressor.out_path is None:
                    raise IOError('Memory image recompression failed')
                zip.write(recompressor.out_path, MEMORY_FILENAME,
                        zipfile.ZIP_STORED)
        except:
            if zip:
                zip.close()
            try:
                os.unlink(temp.name)
            except OSError:
                pass
            raise
        else:
            zip.close()
            os.rename(temp.name, out)
        finally:
            if recompressor:
                recompressor.join()
                recompressor.close()
