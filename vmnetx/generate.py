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
from contextlib import closing
import libvirt
import os
import subprocess
import sys
from tempfile import NamedTemporaryFile

from .domain import DomainXML, DomainXMLError
from .memory import LibvirtQemuMemoryHeader
from .package import Package
from .source import source_open
from .util import DetailException

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


class MachineGenerationError(DetailException):
    pass


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
        raise MachineGenerationError('Cannot decode save format %d' %
                compress_in)
    if compression == 'xz':
        compress_out = hdr.COMPRESS_XZ
    elif compression == 'lzop':
        compress_out = hdr.COMPRESS_LZOP
    elif compression == None:
        compress_out = hdr.COMPRESS_RAW
    else:
        raise ValueError('Unknown compression: %s' % compression)
    if compress_out not in MEMORY_COMPRESS_COMMANDS:
        raise MachineGenerationError('Cannot encode save format %d' %
                compress_out)

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
            data = fh.read()
        with closing(libvirt.open('qemu:///session')) as conn:
            data = DomainXML.make_backward_compatible(conn, data)
        domain = DomainXML(data, validate=DomainXML.VALIDATE_STRICT)
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
                    compression='xz' if compress else None)
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

    package = Package(source_open(filename=in_file))

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
                copy_memory(temp_in.name, temp_memory.name, domain_xml,
                        compression='xz')
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
