#
# vmnetx - Virtual machine network execution
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

from __future__ import division
from lxml import etree
from lxml.builder import ElementMaker
import os
import shutil
import struct
import subprocess

MANIFEST_NAMESPACE = 'http://olivearchive.org/xmlns/vmnetx/manifest'

MANIFEST_NAME = 'machine.vnx'
MACHINE_NAME = 'machine.xml'
DISK_NAME = 'disk.qcow'
MEMORY_NAME = 'memory.img'

class MachineError(Exception):
    pass


class QemuMemoryHeader(object):
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
            raise MachineError('Invalid memory image magic')
        if version != self.HEADER_VERSION:
            raise MachineError('Unknown memory image version %d' % version)
        if header != [0] * self.HEADER_UNUSED_VALUES:
            raise MachineError('Unused header values not 0')

        # Read XML, drop trailing NUL padding
        self.xml = f.read(self._xml_len - 1).rstrip('\0')
        if f.read(1) != '\0':
            raise MachineError('Missing NUL byte after XML')

    def seek_body(self, f):
        f.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, f):
        # Calculate header
        if len(self.xml) > self._xml_len - 1:
            # If this becomes a problem, we could write out a larger xml_len,
            # though this must be page-aligned.
            raise MachineError('self.xml is too large')
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
        hdr = QemuMemoryHeader(fin)
        if hdr.compressed != hdr.COMPRESS_RAW:
            raise MachineError('Cannot recompress save format %d' % compressed)

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
    except (MachineError, IOError), e:
        print 'Copying memory image without recompressing: %s' % str(e)
        fin.seek(0)
        fout.seek(0)
        fout.truncate()
        shutil.copyfileobj(fin, fout)


def copy_disk(in_path, out_path):
    print 'Copying and compressing disk image...'
    if subprocess.call(['qemu-img', 'convert', '-cp', '-O', 'qcow2',
            in_path, out_path]) != 0:
        raise MachineError('qemu-img failed')


def copy_machine(in_xml, out_dir):
    # Get disk path
    try:
        machine = etree.parse(in_xml)
    except (IOError, lxml.etree.XMLSyntaxError), e:
        raise MachineError(str(e))
    in_disks = machine.xpath('/domain/devices/disk/source/@file')
    if len(in_disks) == 0:
        raise MachineError('Could not locate machine disk image')
    if len(in_disks) > 1:
        raise MachineError('Machine has multiple disk images')
    in_disk = in_disks[0]

    # Get memory path
    in_memory = os.path.join(os.path.dirname(in_xml), 'save',
            '%s.save' % os.path.splitext(os.path.basename(in_xml))[0])

    # Copy disk
    if not os.path.isdir(out_dir):
        os.mkdir(out_dir)
    copy_disk(in_disk, os.path.join(out_dir, DISK_NAME))

    # Copy memory
    if os.path.exists(in_memory):
        copy_memory(in_memory, os.path.join(out_dir, MEMORY_NAME))
    else:
        print 'No memory image found'

    # Modify and write out XML
    # Substitute generic VM name
    machine.xpath('/domain/name')[0].text = 'machine'
    # Remove path information from disk image
    disk_tag = machine.xpath('/domain/devices/disk/source')[0]
    disk_tag.set('file', DISK_NAME)
    # Write it out
    machine.write(os.path.join(out_dir, MACHINE_NAME), encoding='UTF-8',
            pretty_print=True)


def write_manifest(base_url, out_dir, name):
    E = ElementMaker(namespace=MANIFEST_NAMESPACE,
            nsmap={None: MANIFEST_NAMESPACE})
    xml = E.image(
        E.machine(location=os.path.join(base_url, MACHINE_NAME)),
        E.disk(location=os.path.join(base_url, DISK_NAME)),
        name=name,
    )
    if os.path.exists(os.path.join(out_dir, MEMORY_NAME)):
        xml.append(E.memory(location=os.path.join(base_url, MEMORY_NAME)))

    with open(os.path.join(out_dir, MANIFEST_NAME), 'w') as f:
        f.write(etree.tostring(xml, encoding='UTF-8', pretty_print=True,
                xml_declaration=True))


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 5:
        print 'Usage: %s name machine_xml out_dir base_url' % sys.argv[0]
        sys.exit(1)
    _name, _machine_xml, _out_dir, _base_url = sys.argv[1:]
    copy_machine(_machine_xml, _out_dir)
    write_manifest(_base_url, _out_dir, _name)
