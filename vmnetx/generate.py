#
# vmnetx.generate - Generation of a vmnetx machine image
#
# Copyright (C) 2012 Carnegie Mellon University
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

import os
import shutil
import struct
import subprocess

from vmnetx.domain import DomainXML, DomainXMLError
from vmnetx.manifest import Manifest, ReferenceInfo

MANIFEST_NAME = 'machine.vnx'
DOMAIN_NAME = 'domain.xml'
DISK_NAME = 'disk.qcow'
MEMORY_NAME = 'memory.img'

class MachineGenerationError(Exception):
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

    def __init__(self, f):
        # Read header struct
        f.seek(0)
        buf = f.read(self.HEADER_LENGTH)
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
        self.xml = f.read(self._xml_len - 1).rstrip('\0')
        if f.read(1) != '\0':
            raise MachineGenerationError('Missing NUL byte after XML')

    def seek_body(self, f):
        f.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, f):
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
        f.seek(0)
        f.write(struct.pack(self.HEADER_FORMAT, *header))
        f.write(struct.pack('%ds' % self._xml_len, self.xml))


def copy_memory(in_path, out_path):
    # Recompress if possible
    fin = open(in_path)
    fout = open(out_path, 'w')
    try:
        hdr = _QemuMemoryHeader(fin)
        if hdr.compressed != hdr.COMPRESS_RAW:
            raise MachineGenerationError('Cannot recompress save format %d' %
                    compressed)

        # Write header
        hdr.compressed = hdr.COMPRESS_XZ
        hdr.write(fout)

        # Print size of uncompressed image
        fin.seek(0, 2)
        total = fin.tell()
        hdr.seek_body(fin)
        print 'Copying and compressing memory image (%d MB)...' % (
                (total - fin.tell()) >> 20)

        # Write body
        fout.flush()
        ret = subprocess.call(['xz', '-9cv'], stdin=fin, stdout=fout)
        if ret:
            raise IOError('XZ compressor failed')
    except (MachineGenerationError, IOError), e:
        print 'Copying memory image without recompressing: %s' % str(e)
        fin.seek(0)
        fout.seek(0)
        fout.truncate()
        shutil.copyfileobj(fin, fout)


def copy_disk(in_path, out_path):
    print 'Copying and compressing disk image...'
    if subprocess.call(['qemu-img', 'convert', '-cp', '-O', 'qcow2',
            in_path, out_path]) != 0:
        raise MachineGenerationError('qemu-img failed')


def copy_machine(in_xml, out_dir):
    # Parse domain XML
    try:
        with open(in_xml) as fh:
            domain = DomainXML(fh.read())
    except (IOError, DomainXMLError), e:
        raise MachineGenerationError(str(e))

    # Get memory path
    in_memory = os.path.join(os.path.dirname(in_xml), 'save',
            '%s.save' % os.path.splitext(os.path.basename(in_xml))[0])

    # Copy disk
    if not os.path.isdir(out_dir):
        os.mkdir(out_dir)
    copy_disk(domain.disk_path, os.path.join(out_dir, DISK_NAME))

    # Copy memory
    out_memory = os.path.join(out_dir, MEMORY_NAME)
    if os.path.exists(in_memory):
        copy_memory(in_memory, out_memory)
    else:
        print 'No memory image found'
        # Ensure there's not one left over
        if os.path.exists(out_memory):
            os.unlink(out_memory)

    # Write out domain XML
    with open(os.path.join(out_dir, DOMAIN_NAME), 'w') as fh:
        fh.write(domain.get_for_storage(DISK_NAME).xml)


def write_manifest(base_url, out_dir, name):
    def blob_info(name):
        return ReferenceInfo(location=os.path.join(base_url, name),
                size=str(os.stat(os.path.join(out_dir, name)).st_size))

    domain = ReferenceInfo(location=os.path.join(base_url, DOMAIN_NAME),
            size=0)
    disk = blob_info(DISK_NAME)
    if os.path.exists(os.path.join(out_dir, MEMORY_NAME)):
        memory = blob_info(MEMORY_NAME)
    else:
        memory = None
    manifest = Manifest(name=name, domain=domain, disk=disk, memory=memory)

    with open(os.path.join(out_dir, MANIFEST_NAME), 'w') as f:
        f.write(manifest.xml)
