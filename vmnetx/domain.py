#
# vmnetx.domain - Handling of libvirt domain XML
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

from lxml import etree
import subprocess
from tempfile import NamedTemporaryFile

class DomainXMLError(Exception):
    pass


class DomainXML(object):
    def __init__(self, xml):
        self.xml = xml
        self._validate()

        # Get disk path
        tree = etree.fromstring(xml)
        in_disks = tree.xpath('/domain/devices/disk/source/@file')
        if len(in_disks) == 0:
            raise DomainXMLError('Could not locate machine disk image')
        if len(in_disks) > 1:
            raise DomainXMLError('Machine has multiple disk images')
        self.disk_path = in_disks[0]

    @classmethod
    def _to_xml(cls, tree):
        return etree.tostring(tree, pretty_print=True, encoding='UTF-8',
                xml_declaration=True)

    def _validate(self):
        # Validate schema
        with NamedTemporaryFile(prefix='vmnetx-xml-') as fh:
            fh.write(self.xml)
            fh.flush()

            with open('/dev/null', 'w') as null:
                if subprocess.call(['virt-xml-validate', fh.name, 'domain'],
                        stdout=null, stderr=null):
                    raise DomainXMLError('Domain XML does not validate')

        # Run sanity checks
        '''
        XXX:
        - ensure there is only one disk
        - ensure the local filesystem is not touched
        - check for bindings directly to hardware
        '''

    def get_for_storage(self, disk_name, disk_type='qcow2'):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Substitute generic VM name
        tree.xpath('/domain/name')[0].text = 'machine'

        # Remove path information from disk image
        disk_tag = tree.xpath('/domain/devices/disk/source')[0]
        disk_tag.set('file', '/' + disk_name)

        # Update disk driver
        disk_tag = tree.xpath('/domain/devices/disk[@device="disk"]/driver')[0]
        disk_tag.set('type', disk_type)

        # Return new instance
        return type(self)(self._to_xml(tree))

    def get_for_execution(self, name, disk_image_path, vnc_listen_address):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Ensure machine name is unique
        name_nodes = tree.xpath('/domain/name')
        if len(name_nodes) != 1:
            raise DomainXMLError('Error locating machine name XML node')
        name_nodes[0].text = name

        # Update path to hard disk
        source_nodes = tree.xpath('/domain/devices/disk[@device="disk"]/source')
        if len(source_nodes) != 1:
            raise DomainXMLError('Error locating machine disk XML node')
        source_nodes[0].set('file', disk_image_path)

        # Remove graphics declarations
        devices_node = tree.xpath('/domain/devices')[0]
        for node in tree.xpath('/domain/devices/graphics'):
            devices_node.remove(node)

        # Add new graphics declaration
        graphics_node = etree.SubElement(devices_node, 'graphics')
        graphics_node.set('type', 'vnc')
        graphics_node.set('socket', vnc_listen_address)

        # Return new instance
        return type(self)(self._to_xml(tree))
