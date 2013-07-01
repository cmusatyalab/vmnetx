#
# vmnetx.define - Creation of a new VMNetX-compatible VM
#
# Copyright (C) 2013 Carnegie Mellon University
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

from contextlib import closing
import libvirt
import os
import subprocess

from .domain import DomainXML, DomainXMLError
from .util import DetailException

class MachineDefinitionError(DetailException):
    pass


def define_machine(name, memory_mb, disk_gb):
    with closing(libvirt.open('qemu:///session')) as conn:
        # Ensure machine doesn't exist
        try:
            conn.lookupByName(name)
            raise MachineDefinitionError('Machine already exists')
        except libvirt.libvirtError:
            pass

        # Ensure disk doesn't exist
        disk_dir = os.path.join(os.path.expanduser('~'), 'VirtualMachines')
        disk_path = os.path.join(disk_dir, name + '.qcow')
        if os.path.exists(disk_path):
            raise MachineDefinitionError('%s already exists' % disk_path)

        # Create disk
        if not os.path.exists(disk_dir):
            os.makedirs(disk_dir)
        with open('/dev/null', 'r+') as null:
            ret = subprocess.call(['qemu-img', 'create', '-f', 'qcow2',
                    disk_path, str(disk_gb) + 'G'], stdout=null)
        if ret != 0:
            raise MachineDefinitionError("Couldn't create disk image")

        # Create machine
        try:
            domain_xml = DomainXML.get_template(conn, name, disk_path,
                    'qcow2', memory_mb)
            conn.defineXML(domain_xml.xml)
        except DomainXMLError, e:
            raise MachineDefinitionError(str(e), e.detail)
        except libvirt.libvirtError, e:
            raise MachineDefinitionError(str(e))
