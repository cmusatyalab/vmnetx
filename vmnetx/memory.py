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
import os
import struct
import subprocess
import sys

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


MEMORY_COMPRESS_COMMANDS = {
    LibvirtQemuMemoryHeader.COMPRESS_RAW: None,
    LibvirtQemuMemoryHeader.COMPRESS_XZ: ('xz', '-9c'),
    LibvirtQemuMemoryHeader.COMPRESS_LZOP: ('lzop', '-c'),
}
MEMORY_DECOMPRESS_COMMANDS = {
    LibvirtQemuMemoryHeader.COMPRESS_RAW: None,
    LibvirtQemuMemoryHeader.COMPRESS_XZ: ('xz', '-dc'),
    LibvirtQemuMemoryHeader.COMPRESS_LZOP: ('lzop', '-dc', '--ignore-warn'),
}


def copy_memory(in_path, out_path, xml=None, compression='xz', verbose=True,
        low_priority=False):
    def report(line, newline=True):
        if not verbose:
            return
        if newline:
            print line
        else:
            print line,
            sys.stdout.flush()

    # Open files, read header
    fin = open(in_path, 'r')
    fout = open(out_path, 'w')
    hdr = LibvirtQemuMemoryHeader(fin)

    # Determine input and output compression
    compress_in = hdr.compressed
    if compress_in not in MEMORY_DECOMPRESS_COMMANDS:
        raise MemoryImageError('Cannot decode save format %d' % compress_in)
    if compression == 'xz':
        compress_out = hdr.COMPRESS_XZ
    elif compression == 'lzop':
        compress_out = hdr.COMPRESS_LZOP
    elif compression == None:
        compress_out = hdr.COMPRESS_RAW
    else:
        raise ValueError('Unknown compression: %s' % compression)
    if compress_out not in MEMORY_COMPRESS_COMMANDS:
        raise ValueError('Cannot encode save format %d' % compress_out)

    # Write header
    hdr.compressed = compress_out
    if xml is not None:
        hdr.xml = xml
    hdr.write(fout, extend=True)
    fout.flush()

    processes = []
    try:
        # Start compressor/decompressor if required
        if compress_in != compress_out:
            for command in (MEMORY_COMPRESS_COMMANDS[compress_out],
                    MEMORY_DECOMPRESS_COMMANDS[compress_in]):
                if not command:
                    continue
                if low_priority:
                    # Python < 3.3 doesn't have os.setpriority(), so we use
                    # the command-line utility
                    command = ['nice'] + list(command)
                pipe_r, pipe_w = os.pipe()
                proc = subprocess.Popen(command, stdin=pipe_r, stdout=fout,
                        close_fds=True)
                processes.append(proc)
                os.close(pipe_r)
                fout.close()
                fout = os.fdopen(pipe_w, 'w')

        # Copy body; report progress
        fin.seek(0, 2)
        total = fin.tell()
        hdr.seek_body(fin)
        if compress_in != compress_out and compress_out != hdr.COMPRESS_RAW:
            action = 'Copying and compressing'
        else:
            action = 'Copying'
        while True:
            buf = fin.read(1 << 20)
            if not buf:
                break
            fout.write(buf)
            report('\r%s memory image: %3d%%' % (action,
                    100 * fin.tell() / total), newline=False)
        report('')
    finally:
        # Clean up
        fin.close()
        fout.close()
        failed = False
        for proc in reversed(processes):
            proc.wait()
            failed = failed or proc.returncode
        if failed:
            raise IOError('Compressor/decompressor failed')
