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

from __future__ import division
import hashlib
import os
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile

from vmnetx.domain import DomainXML, DomainXMLError
from vmnetx.manifest import Manifest, ReferenceInfo

DOMAIN_TEMPLATE = 'domain-%s.xml'
DISK_TEMPLATE = 'disk-%s.qcow'
DISK_RAW_TEMPLATE = 'disk-%s.raw'
MEMORY_TEMPLATE = 'memory-%s.img'

class MachineGenerationError(Exception):
    pass


# File handle arguments don't need more than two letters
# pylint: disable=C0103
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
# pylint: enable=C0103


class _Progress(object):
    def __init__(self, msg, total, enable=True):
        self._enable = enable
        self._msg = msg
        self._pct = -1
        self._count = 0
        self._total = total

    def update(self, count):
        self._count += count
        cur_pct = 100 * self._count // self._total
        if self._enable and cur_pct > self._pct:
            print '  %s: %3d%%\r' % (self._msg, cur_pct),
            sys.stdout.flush()
            self._pct = cur_pct

    def finish(self):
        if self._enable:
            print


def copy_memory(in_path, out_path, xml=None, compress=True):
    # Ensure the input is uncompressed, even if we will not be compressing
    fin = open(in_path)
    fout = open(out_path, 'w')
    hdr = _QemuMemoryHeader(fin)
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


# pylint is confused by hashlib.sha256()
# pylint: disable=E1101
def finalize_blob(in_path, name_template):
    # Rename a blob to include its SHA-256 hash.  Template must contain "%s".
    # Return the new path and blob size.
    hash = hashlib.sha256()
    size = os.stat(in_path).st_size
    prog = _Progress('Computing hash', size, size > 1 << 20)
    with open(in_path) as fh:
        for buf in iter(lambda: fh.read(128 << 10), ''):
            hash.update(buf)
            prog.update(len(buf))
    prog.finish()
    out_path = os.path.join(os.path.dirname(in_path),
            name_template % hash.hexdigest())
    os.rename(in_path, out_path)
    return out_path, size
# pylint: enable=E1101


def generate_machine(name, in_xml, base_url, out_file, compress=True):
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
    out_dir = os.path.dirname(out_file)
    temp = NamedTemporaryFile(dir=out_dir, prefix='disk-', delete=False)
    temp.close()
    copy_disk(domain.disk_path, domain.disk_type, temp.name,
            raw=not compress)
    out_disk, out_disk_size = finalize_blob(temp.name,
            DISK_TEMPLATE if compress else DISK_RAW_TEMPLATE)

    # Generate domain XML
    domain_xml = domain.get_for_storage(os.path.basename(out_disk),
            disk_type='qcow2' if compress else 'raw').xml

    # Copy memory
    if os.path.exists(in_memory):
        temp = NamedTemporaryFile(dir=out_dir, prefix='memory-', delete=False)
        temp.close()
        copy_memory(in_memory, temp.name, domain_xml, compress=compress)
        out_memory, out_memory_size = finalize_blob(temp.name,
                MEMORY_TEMPLATE)
    else:
        print 'No memory image found'
        out_memory = None
        out_memory_size = 0

    # Write out domain XML
    temp = NamedTemporaryFile(dir=out_dir, prefix='domain-', delete=False)
    temp.write(domain_xml)
    temp.close()
    out_domain, _out_domain_size = finalize_blob(temp.name, DOMAIN_TEMPLATE)

    # Generate manifest
    def blob_info(path, size=0):
        return ReferenceInfo(
            location=os.path.join(base_url, os.path.basename(path)),
            size=size,
        )
    domain = blob_info(out_domain)
    disk = blob_info(out_disk, out_disk_size)
    if out_memory is not None:
        memory = blob_info(out_memory, out_memory_size)
    else:
        memory = None
    manifest = Manifest(name=name, domain=domain, disk=disk, memory=memory)

    # Write out manifest
    with open(out_file, 'w') as f:
        f.write(manifest.xml)
