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

    COMPRESS_RAW = 0
    COMPRESS_XZ = 3

    # pylint is confused by "\0", #111799
    # pylint: disable=W1401
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
    # pylint: enable=W1401

    def seek_body(self, fh):
        fh.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, fh):
        # Calculate header
        if len(self.xml) > self._xml_len - 1:
            # If this becomes a problem, we could write out a larger xml_len,
            # though this must be page-aligned.
            raise MemoryImageError('self.xml is too large')
        header = [self.HEADER_MAGIC,
                self.HEADER_VERSION,
                self._xml_len,
                self.was_running,
                self.compressed]
        header.extend([0] * self.HEADER_UNUSED_VALUES)

        # Write data
        fh.seek(0)
        fh.write(struct.pack(self.HEADER_FORMAT, *header))
        fh.write(struct.pack('%ds' % self._xml_len, self.xml))
