#
# vmnetx.memory - libvirt qemu memory image handling
#
# Copyright (C) 2012-2013 Carnegie Mellon University
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
import struct

class MemoryImageError(Exception):
    pass


class LibvirtQemuMemoryHeader(object):
    HEADER_MAGIC = 'LibvirtQemudSave'
    HEADER_VERSION = 2
    # Header values are stored "native-endian".  We only support x86, so
    # assume we don't need to byteswap.
    HEADER_FORMAT = str(len(HEADER_MAGIC)) + 's19I'
    HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)
    HEADER_UNUSED_VALUES = 15

    XML_MINIMUM_PAD = 8 << 10
    XML_END_ALIGNMENT = 4 << 10   # QEMU_MONITOR_MIGRATE_TO_FILE_BS

    COMPRESS_RAW = 0
    COMPRESS_XZ = 3
    COMPRESS_LZOP = 4

    def __init__(self, fh):
        # Read header struct
        fh.seek(0)
        buf = fh.read(self.HEADER_LENGTH)
        header = list(struct.unpack(self.HEADER_FORMAT, buf))
        magic = header.pop(0)
        version = header.pop(0)
        self._xml_len = header.pop(0)
        self.was_running = header.pop(0)
        self.compressed = header.pop(0)

        # Check header
        if magic != self.HEADER_MAGIC:
            raise MemoryImageError('Invalid memory image magic')
        if version != self.HEADER_VERSION:
            raise MemoryImageError('Unknown memory image version %d' % version)
        if header != [0] * self.HEADER_UNUSED_VALUES:
            raise MemoryImageError('Unused header values not 0')

        # Read XML, drop trailing NUL padding
        self.xml = fh.read(self._xml_len - 1).rstrip('\0')
        if fh.read(1) != '\0':
            raise MemoryImageError('Missing NUL byte after XML')

    def seek_body(self, fh):
        fh.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, fh, extend=False):
        '''extend=True does not update the internal state used by
        seek_body().'''
        # Calculate new XML length
        xml_len = self._xml_len
        if extend and xml_len - 1 < len(self.xml) + self.XML_MINIMUM_PAD:
            xml_len = len(self.xml) + self.XML_MINIMUM_PAD + 1
            # Round up the start of the memory image data to a multiple of
            # the typical disk sector size
            xml_len = (((self.HEADER_LENGTH + xml_len +
                    self.XML_END_ALIGNMENT - 1) // self.XML_END_ALIGNMENT) *
                    self.XML_END_ALIGNMENT) - self.HEADER_LENGTH
        if len(self.xml) > xml_len - 1:
            raise MemoryImageError('self.xml is too large')

        # Calculate header
        header = [self.HEADER_MAGIC,
                self.HEADER_VERSION,
                xml_len,
                self.was_running,
                self.compressed]
        header.extend([0] * self.HEADER_UNUSED_VALUES)

        # Write data
        fh.seek(0)
        fh.write(struct.pack(self.HEADER_FORMAT, *header))
        fh.write(struct.pack('%ds' % xml_len, self.xml))
