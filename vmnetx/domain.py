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
import os
import subprocess
from tempfile import NamedTemporaryFile
import uuid

from vmnetx.util import DetailException

STRICT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'libvirt',
        'domain.rng')

# We want this to be a public attribute
# pylint: disable=C0103
strict_schema = etree.RelaxNG(file=STRICT_SCHEMA_PATH)
# pylint: enable=C0103


class DomainXMLError(DetailException):
    pass


class DomainXML(object):
    def __init__(self, xml, strict=False):
        self.xml = xml
        self._validate(strict)

        # Get disk path and type
        tree = etree.fromstring(xml)
        in_disks = tree.xpath('/domain/devices/disk[@device="disk"]')
        if len(in_disks) == 0:
            raise DomainXMLError('Could not locate machine disk image')
        if len(in_disks) > 1:
            raise DomainXMLError('Machine has multiple disk images')
        disk_paths = in_disks[0].xpath('source/@file')
        disk_types = in_disks[0].xpath('driver/@type')
        if len(disk_paths) != 1 or len(disk_types) != 1:
            raise DomainXMLError('Could not read machine disk declaration')
        self.disk_path = disk_paths[0]
        self.disk_type = disk_types[0]

    @classmethod
    def _to_xml(cls, tree):
        return etree.tostring(tree, pretty_print=True, encoding='UTF-8',
                xml_declaration=True)

    # pylint is confused by Popen.returncode
    # pylint: disable=E1101
    def _validate(self, strict=False):
        # Parse XML
        try:
            tree = etree.fromstring(self.xml)
        except etree.XMLSyntaxError, e:
            raise DomainXMLError('Domain XML does not parse', str(e))

        # Validate schema
        if strict:
            # Validate against schema from minimum supported libvirt
            try:
                strict_schema.assertValid(tree)
            except etree.DocumentInvalid, e:
                raise DomainXMLError('Domain XML does not validate', str(e))
        else:
            # Validate against schema from installed libvirt
            with NamedTemporaryFile(prefix='vmnetx-xml-') as fh:
                fh.write(self.xml)
                fh.flush()

                proc = subprocess.Popen(['virt-xml-validate', fh.name,
                        'domain'], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                out, err = proc.communicate()
                if proc.returncode:
                    raise DomainXMLError('Domain XML does not validate',
                            (out.strip() + '\n' + err.strip()).strip())

        # Run sanity checks
        if strict:
            # For now, require hvm/i686/pc:
            # - The client may not have emulators for arbitrary arches.
            # - KVM won't run x86_64 guests on i686 host kernels.
            # - "pc" is the only machine type supported by both RHEL 6 and
            #   mainline qemu.  It's an alias, so it doesn't buy us any
            #   virtual hw consistency, but at least the VM will start.
            type = tree.xpath('/domain/os/type')
            if len(type) != 1:
                raise DomainXMLError('Could not locate machine hardware type')
            type = type[0]
            machine = '%s/%s/%s' % (type.text, type.get('arch', 'unknown'),
                    type.get('machine', 'unknown'))
            if machine != 'hvm/i686/pc':
                raise DomainXMLError(
                        'Found machine type %s; must be hvm/i686/pc' % machine)

        '''
        XXX:
        - ensure there is only one disk
        - ensure the local filesystem is not touched
        - check for bindings directly to hardware
        '''
    # pylint: enable=E1101

    def get_for_storage(self, disk_name, disk_type='qcow2', keep_uuid=False):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Substitute generic VM name
        tree.xpath('/domain/name')[0].text = 'machine'

        # Regenerate UUID
        if not keep_uuid:
            tree.xpath('/domain/uuid')[0].text = str(uuid.uuid4())

        # Remove path information from disk image
        disk_tag = tree.xpath('/domain/devices/disk/source')[0]
        disk_tag.set('file', '/' + disk_name)

        # Update disk driver
        disk_tag = tree.xpath('/domain/devices/disk[@device="disk"]/driver')[0]
        disk_tag.set('type', disk_type)

        # Return new instance
        return type(self)(self._to_xml(tree))

    def get_for_execution(self, conn, name, disk_image_path,
            vnc_listen_address):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Ensure machine name is unique
        name_nodes = tree.xpath('/domain/name')
        if len(name_nodes) != 1:
            raise DomainXMLError('Error locating machine name XML node')
        name_nodes[0].text = name

        # Update path to emulator
        emulator_nodes = tree.xpath('/domain/devices/emulator')
        if len(emulator_nodes) != 1:
            raise DomainXMLError('Error locating machine emulator XML node')
        emulator_nodes[0].text = self._get_emulator(conn, tree)

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

    @staticmethod
    def _get_emulator(conn, tree):
        caps = etree.fromstring(conn.getCapabilities())

        # Get desired emulator properties
        type_nodes = tree.xpath('/domain/os/type')
        if len(type_nodes) != 1:
            raise DomainXMLError('Error locating machine OS type XML node')
        domain_type = tree.get('type')
        type = type_nodes[0].text
        arch = type_nodes[0].get('arch')
        machine = type_nodes[0].get('machine')

        # Find a suitable emulator
        for guest in caps.xpath('/capabilities/guest'):
            # Check type
            type_nodes = guest.xpath('os_type')
            if len(type_nodes) != 1:
                continue
            if type_nodes[0].text != type:
                continue

            # Check architectures
            for arch_node in guest.xpath('arch'):
                if arch_node.get('name') != arch:
                    continue

                # Check supported machines
                for machine_node in arch_node.xpath('machine'):
                    if machine_node.text == machine:
                        # Check domain types
                        for domain_node in arch_node.xpath('domain'):
                            if domain_node.get('type') == domain_type:
                                # Found a match!
                                emulator_nodes = domain_node.xpath('emulator')
                                if len(emulator_nodes) != 1:
                                    continue
                                return emulator_nodes[0].text

        # Failed.
        raise DomainXMLError('No suitable emulator for %s/%s/%s' %
                (type, arch, machine))
