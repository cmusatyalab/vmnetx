#
# vmnetx.generate - Generation of a vmnetx machine image
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
from tempfile import NamedTemporaryFile
from urlparse import urlunsplit

from .domain import DomainXML, DomainXMLError
from .package import Package
from .util import DetailException

class MachineGenerationError(DetailException):
    pass


class _QemuMemoryHeader(object):
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
            raise MachineGenerationError('Invalid memory image magic')
        if version != self.HEADER_VERSION:
            raise MachineGenerationError('Unknown memory image version %d' %
                    version)
        if header != [0] * self.HEADER_UNUSED_VALUES:
            raise MachineGenerationError('Unused header values not 0')

        # Read XML, drop trailing NUL padding
        self.xml = fh.read(self._xml_len - 1).rstrip('\0')
        if fh.read(1) != '\0':
            raise MachineGenerationError('Missing NUL byte after XML')
    # pylint: enable=W1401

    def seek_body(self, fh):
        fh.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, fh):
        # Calculate header
        if len(self.xml) > self._xml_len - 1:
            # If this becomes a problem, we could write out a larger xml_len,
            # though this must be page-aligned.
            raise MachineGenerationError('self.xml is too large')
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


def copy_memory(in_path, out_path, xml=None, compress=True):
    # Disable buffering on fin to ensure that the file offset inherited
    # by xz is exactly what we pass to seek()
    fin = open(in_path, 'r', 0)
    fout = open(out_path, 'w')
    hdr = _QemuMemoryHeader(fin)
    # Ensure the input is uncompressed, even if we will not be compressing
    if hdr.compressed != hdr.COMPRESS_RAW:
        raise MachineGenerationError('Cannot recompress save format %d' %
                hdr.compressed)

    # Write header
    if compress:
        hdr.compressed = hdr.COMPRESS_XZ
    if xml is not None:
        hdr.xml = xml
    hdr.write(fout)

    # Print size of uncompressed image
    fin.seek(0, 2)
    total = fin.tell()
    hdr.seek_body(fin)
    if compress:
        action = 'Copying and compressing'
    else:
        action = 'Copying'
    print '%s memory image (%d MB)...' % (action, (total - fin.tell()) >> 20)

    # Write body
    fout.flush()
    if compress:
        ret = subprocess.call(['xz', '-9cv'], stdin=fin, stdout=fout)
        if ret:
            raise IOError('XZ compressor failed')
    else:
        while True:
            buf = fin.read(1 << 20)
            if not buf:
                break
            fout.write(buf)
            print '  %3d%%\r' % (100 * fout.tell() / total),
            sys.stdout.flush()
        print


def copy_disk(in_path, type, out_path, raw=False):
    if raw:
        print 'Copying disk image...'
        ret = subprocess.call(['qemu-img', 'convert', '-p', '-f', type,
                '-O', 'raw', in_path, out_path])
    else:
        print 'Copying and compressing disk image...'
        ret = subprocess.call(['qemu-img', 'convert', '-cp', '-f', type,
                '-O', 'qcow2', in_path, out_path])

    if ret != 0:
        raise MachineGenerationError('qemu-img failed')


def generate_machine(name, in_xml, out_file, compress=True):
    # Parse domain XML
    try:
        with open(in_xml) as fh:
            domain = DomainXML(fh.read(), validate=DomainXML.VALIDATE_STRICT)
    except (IOError, DomainXMLError), e:
        raise MachineGenerationError(str(e), getattr(e, 'detail', None))

    # Get memory path
    in_memory = os.path.join(os.path.dirname(in_xml), 'save',
            '%s.save' % os.path.splitext(os.path.basename(in_xml))[0])

    # Generate domain XML
    domain_xml = domain.get_for_storage(disk_type='qcow2' if compress
            else 'raw').xml

    temp_disk = None
    temp_memory = None
    try:
        # Copy disk
        out_dir = os.path.dirname(out_file)
        temp_disk = NamedTemporaryFile(dir=out_dir, prefix='disk-')
        copy_disk(domain.disk_path, domain.disk_type, temp_disk.name,
                raw=not compress)

        # Copy memory
        if os.path.exists(in_memory):
            temp_memory = NamedTemporaryFile(dir=out_dir, prefix='memory-')
            copy_memory(in_memory, temp_memory.name, domain_xml,
                    compress=compress)
        else:
            print 'No memory image found'

        # Write package
        print 'Writing package...'
        try:
            Package.create(out_file, name, domain_xml, temp_disk.name,
                    temp_memory.name if temp_memory else None)
        except:
            os.unlink(out_file)
            raise
    finally:
        if temp_disk:
            temp_disk.close()
        if temp_memory:
            temp_memory.close()


def compress_machine(in_file, out_file, name=None):
    '''Read an uncompressed machine package and write a compressed one.'''

    url = urlunsplit(('file', '', os.path.abspath(in_file), '', ''))
    package = Package(url)

    # Parse domain XML
    try:
        domain = DomainXML(package.domain.data,
                validate=DomainXML.VALIDATE_STRICT)
    except DomainXMLError, e:
        raise MachineGenerationError(str(e), e.detail)

    # Generate new domain XML with updated disk type
    domain_xml = domain.get_for_storage(keep_uuid=True).xml

    temp_disk = None
    temp_memory = None
    try:
        # Copy disk
        out_dir = os.path.dirname(out_file)
        temp_disk = NamedTemporaryFile(dir=out_dir, prefix='disk-')
        with NamedTemporaryFile(dir=out_dir, prefix='in-') as temp_in:
            print 'Extracting disk image...'
            package.disk.write_to_file(temp_in)
            temp_in.flush()
            copy_disk(temp_in.name, domain.disk_type, temp_disk.name)

        # Copy memory
        if package.memory:
            temp_memory = NamedTemporaryFile(dir=out_dir, prefix='memory-')
            with NamedTemporaryFile(dir=out_dir, prefix='in-') as temp_in:
                print 'Extracting memory image...'
                package.memory.write_to_file(temp_in)
                temp_in.flush()
                copy_memory(temp_in.name, temp_memory.name, domain_xml)
        else:
            print 'No memory image found'

        # Write package
        print 'Writing package...'
        try:
            Package.create(out_file, name or package.name, domain_xml,
                    temp_disk.name, temp_memory.name if temp_memory else None)
        except:
            os.unlink(out_file)
            raise
    finally:
        if temp_disk:
            temp_disk.close()
        if temp_memory:
            temp_memory.close()
