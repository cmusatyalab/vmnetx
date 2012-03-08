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
from lxml import etree
import os
import subprocess
import tempfile
import threading
import urllib2
import uuid

from vmnetx.vmnetfs import VMNetFS

class MachineExecutionError(Exception):
    pass


class _ReferencedObject(object):
    def __init__(self, element, dir=None, chunk_size=131072):
        self.url = element.get('location')
        self.size = int(element.get('size'))
        self.chunk_size = chunk_size

        # Ensure a crafted URL can't escape the cache directory
        basepath = os.path.expanduser(os.path.join('~', '.vmnetx', 'cache'))
        self.cache = os.path.realpath(os.path.join(basepath, self.url,
                str(chunk_size)))
        if not self.cache.startswith(basepath):
            raise MachineExecutionError('Invalid object URL')


class _VMNetFSRunner(threading.Thread):
    def __init__(self, disk, memory):
        threading.Thread.__init__(self, name='vmnetfs')
        self.mountpoint = tempfile.mkdtemp(prefix='vmnetx-', dir='/var/tmp')
        self._fs = VMNetFS(self.mountpoint,
            disk.url, disk.cache, disk.size, 0, disk.chunk_size,
            memory.url, memory.cache, memory.size, 0, memory.chunk_size)
        self.disk_path = os.path.join(self.mountpoint, 'disk', 'image')
        self.memory_path = os.path.join(self.mountpoint, 'memory', 'image')

    def run(self):
        # Thread function
        self._fs.run()

    def stop(self):
        self._fs.terminate()
        self.join()
        os.rmdir(self.mountpoint)


class Machine(object):
    NS = '{http://olivearchive.org/xmlns/vmnetx/manifest}'

    def __init__(self, manifest_path):
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._vnc_socket_dir = tempfile.mkdtemp(prefix='vmnetx-socket-')
        self.vnc_listen_address = os.path.join(self._vnc_socket_dir, 'vnc')

        # Parse manifest
        image = etree.parse(manifest_path).getroot()
        self.name = image.get('name')
        domain = _ReferencedObject(image.find(self.NS + 'domain'))
        disk = _ReferencedObject(image.find(self.NS + 'disk'))
        memory = _ReferencedObject(image.find(self.NS + 'memory'))

        # Set up vmnetfs
        self._fs = _VMNetFSRunner(disk, memory)

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')
        self._domain_xml = self._get_domain_xml(domain)

    def start(self):
        # Start vmnetfs
        self._fs.start()

        # Start VM
        self._conn.restoreFlags(self._fs.memory_path, self._domain_xml,
                libvirt.VIR_DOMAIN_SAVE_RUNNING)

    def stop(self):
        # Stop instance
        try:
            instance = self._conn.lookupByName(self._domain_name)
        except libvirt.libvirtError:
            pass
        else:
            instance.destroy()
        self._conn.close()

        # Stop vmnetfs
        self._fs.stop()

        # Delete VNC socket
        try:
            os.unlink(self.vnc_listen_address)
            os.rmdir(self._vnc_socket_dir)
        except OSError:
            pass

    def _get_domain_xml(self, domain):
        # Read XML from server
        fh = urllib2.urlopen(domain.url)
        try:
            xml = fh.read()
        finally:
            fh.close()

        # Validate it
        self._validate_domain_xml(xml)

        # Run sanity checks
        '''
        XXX:
        - ensure there is only one disk
        - ensure the local filesystem is not touched
        - check for bindings directly to hardware
        '''

        # Update it
        tree = etree.fromstring(xml)
        # Ensure machine name is unique
        name_nodes = tree.xpath('/domain/name')
        if len(name_nodes) != 1:
            raise MachineExecutionError('Error locating machine name XML node')
        name_nodes[0].text = self._domain_name
        # Update path to hard disk
        source_nodes = tree.xpath('/domain/devices/disk[@device="disk"]/source')
        if len(source_nodes) != 1:
            raise MachineExecutionError('Error locating machine disk XML node')
        source_nodes[0].set('file', self._fs.disk_path)
        # Remove graphics declarations
        devices_node = tree.xpath('/domain/devices')[0]
        for node in tree.xpath('/domain/devices/graphics'):
            devices_node.remove(node)
        # Add new graphics declaration
        graphics_node = etree.SubElement(devices_node, 'graphics')
        graphics_node.set('type', 'vnc')
        graphics_node.set('socket', self.vnc_listen_address)
        xml = etree.tostring(tree, pretty_print=True)

        # Validate it again
        self._validate_domain_xml(xml)

        return xml

    @staticmethod
    def _validate_domain_xml(xml):
        with tempfile.NamedTemporaryFile(prefix='vmnetx-xml-') as fh:
            fh.write(xml)
            fh.flush()

            with open('/dev/null', 'w') as null:
                if subprocess.call(['virt-xml-validate', fh.name, 'domain'],
                        stdout=null, stderr=null):
                    raise MachineExecutionError('Domain XML does not validate')
