#
# vmnetx.execute - Execution of a virtual machine
#
# Copyright (C) 2011-2012 Carnegie Mellon University
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

import libvirt
import os
import tempfile
import threading
import urllib2
import uuid

from vmnetx.domain import DomainXML
from vmnetx.manifest import Manifest
from vmnetx.vmnetfs import VMNetFS

class MachineExecutionError(Exception):
    pass


class _ReferencedObject(object):
    def __init__(self, info, dir=None, chunk_size=131072):
        self.url = info.location
        self.size = info.size
        self.chunk_size = chunk_size

        # Ensure a crafted URL can't escape the cache directory
        basepath = os.path.expanduser(os.path.join('~', '.vmnetx', 'cache'))
        self.cache = os.path.realpath(os.path.join(basepath, self.url,
                str(chunk_size)))
        if not self.cache.startswith(basepath):
            raise MachineExecutionError('Invalid object URL')


class Machine(object):
    def __init__(self, manifest_path):
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._vnc_socket_dir = tempfile.mkdtemp(prefix='vmnetx-socket-')
        self.vnc_listen_address = os.path.join(self._vnc_socket_dir, 'vnc')

        # Parse manifest
        with open(manifest_path) as fh:
            manifest = Manifest(xml=fh.read())
        self.name = manifest.name
        domain = _ReferencedObject(manifest.domain)
        disk = _ReferencedObject(manifest.disk)
        memory = _ReferencedObject(manifest.memory)

        # Start vmnetfs
        self._fs = VMNetFS(disk.url, disk.cache, disk.size, 0, disk.chunk_size,
                memory.url, memory.cache, memory.size, 0, memory.chunk_size)
        self._fs.start()
        self.disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(self.disk_path, 'image')
        self.memory_path = os.path.join(self._fs.mountpoint, 'memory')
        self._memory_image_path = os.path.join(self.memory_path, 'image')

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')

        # Fetch and validate domain XML
        fh = urllib2.urlopen(domain.url)
        try:
            xml = fh.read()
        finally:
            fh.close()
        self._domain_xml = DomainXML(xml).get_for_execution(self._domain_name,
                disk_image_path, self.vnc_listen_address).xml

    def start_vm(self):
        try:
            self._conn.restoreFlags(self._memory_image_path, self._domain_xml,
                    libvirt.VIR_DOMAIN_SAVE_RUNNING)
        except libvirt.libvirtError, e:
            raise MachineExecutionError(str(e))

    def stop_vm(self):
        try:
            self._conn.lookupByName(self._domain_name).destroy()
        except libvirt.libvirtError:
            # Assume that the VM did not exist or was already dying
            pass

    def close(self):
        # Close libvirt connection
        self._conn.close()

        # Delete VNC socket
        try:
            os.unlink(self.vnc_listen_address)
            os.rmdir(self._vnc_socket_dir)
        except OSError:
            pass

        # Terminate vmnetfs
        self._fs.terminate()
