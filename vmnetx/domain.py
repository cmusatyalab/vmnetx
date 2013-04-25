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

# vmnetx-specific metadata extensions
NS = 'http://olivearchive.org/xmlns/vmnetx/domain-metadata'
NSP = '{' + NS + '}'

SAFE_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema',
        'domain.xsd')
STRICT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema',
        'libvirt', 'domain.rng')

# We want these to be public attributes
# pylint: disable=C0103
safe_schema = etree.XMLSchema(etree.parse(SAFE_SCHEMA_PATH))
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
        in_disk = self._xpath_one(tree, '/domain/devices/disk[@device="disk"]')
        self.disk_path = self._xpath_one(in_disk, 'source/@file')
        self.disk_type = self._xpath_one(in_disk, 'driver/@type')

        # Extract vmnetx-specific metadata
        meta = self._xpath_opt(tree, '/domain/metadata/v:vmnetx')
        self.max_mouse_rate = self._xpath_opt(meta, 'v:limit_mouse_rate/@hz',
                int)

    @classmethod
    def _xpath_opt(cls, tree, xpath, converter=lambda v: v):
        '''Expect zero or one results.  Return None in the former case.'''
        if tree is None:
            return None
        result = tree.xpath(xpath, namespaces={'v': NS})
        if len(result) == 0:
            return None
        if len(result) > 1:
            raise DomainXMLError('Query "%s" returned multiple results' %
                    xpath)
        return converter(result[0])

    @classmethod
    def _xpath_one(cls, tree, xpath, converter=lambda v: v):
        '''Expect exactly one result.'''
        ret = cls._xpath_opt(tree, xpath, converter)
        if ret is None:
            raise DomainXMLError('Query "%s" returned no results' % xpath)
        return ret

    @classmethod
    def _to_xml(cls, tree):
        return etree.tostring(tree, pretty_print=True, encoding='UTF-8',
                xml_declaration=True)

    @classmethod
    def _remove_metadata(cls, tree):
        # Strip <metadata> element, which is not supported by libvirt < 0.9.10
        # and is not meant for libvirt anyway
        metadata = cls._xpath_opt(tree, '/domain/metadata')
        if metadata is not None:
            metadata.getparent().remove(metadata)

    # pylint is confused by Popen.returncode
    # pylint: disable=E1101
    def _validate(self, strict=False):
        # Parse XML
        try:
            tree = etree.fromstring(self.xml)
        except etree.XMLSyntaxError, e:
            raise DomainXMLError('Domain XML does not parse', str(e))

        # Ensure XML contains no prohibited configuration
        try:
            safe_schema.assertValid(tree)
        except etree.DocumentInvalid, e:
            raise DomainXMLError('Domain XML contains prohibited elements',
                    str(e))

        # Strip <metadata> element before validating against libvirt schema
        self._remove_metadata(tree)
        xml = self._to_xml(tree)

        if strict:
            # Validate against schema from minimum supported libvirt
            # (in case our schema is accidentally more permissive than the
            # libvirt schema)
            try:
                strict_schema.assertValid(tree)
            except etree.DocumentInvalid, e:
                raise DomainXMLError(
                        'Domain XML unsupported by oldest supported libvirt',
                        str(e))
        else:
            # Validate against schema from installed libvirt
            with NamedTemporaryFile(prefix='vmnetx-xml-') as fh:
                fh.write(xml)
                fh.flush()

                proc = subprocess.Popen(['virt-xml-validate', fh.name,
                        'domain'], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                out, err = proc.communicate()
                if proc.returncode:
                    raise DomainXMLError(
                            'Domain XML unsupported by installed libvirt',
                            (out.strip() + '\n' + err.strip()).strip())
    # pylint: enable=E1101

    def get_for_storage(self, disk_type='qcow2', keep_uuid=False):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Substitute generic VM name
        self._xpath_one(tree, '/domain/name').text = 'machine'

        # Regenerate UUID
        if not keep_uuid:
            self._xpath_one(tree, '/domain/uuid').text = str(uuid.uuid4())

        # Remove path information from disk image
        self._xpath_one(tree, '/domain/devices/disk/source').set('file',
                '/disk.img')

        # Update disk driver
        self._xpath_one(tree,
                '/domain/devices/disk[@device="disk"]/driver').set('type',
                disk_type)

        # Return new instance
        return type(self)(self._to_xml(tree))

    def get_for_execution(self, conn, name, disk_image_path,
            vnc_listen_address):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Remove metadata element
        self._remove_metadata(tree)

        # Ensure machine name is unique
        self._xpath_one(tree, '/domain/name').text = name

        # Update path to emulator
        self._xpath_one(tree, '/domain/devices/emulator').text = \
                self._get_emulator_for_domain(conn, tree)

        # Update path to hard disk
        self._xpath_one(tree,
                '/domain/devices/disk[@device="disk"]/source').set('file',
                disk_image_path)

        # Remove graphics declarations
        devices_node = self._xpath_one(tree, '/domain/devices')
        for node in tree.xpath('/domain/devices/graphics'):
            devices_node.remove(node)

        # Add new graphics declaration
        graphics_node = etree.SubElement(devices_node, 'graphics')
        graphics_node.set('type', 'vnc')
        graphics_node.set('socket', vnc_listen_address)

        # Return new instance
        return type(self)(self._to_xml(tree))

    @classmethod
    def _get_emulator_for_domain(cls, conn, tree):
        # Get desired emulator properties
        type_node = cls._xpath_one(tree, '/domain/os/type')
        domain_type = tree.get('type')
        os_type = type_node.text
        arch = type_node.get('arch')
        machine = type_node.get('machine')

        # Find a suitable emulator
        return cls._get_emulator(conn, os_type, domain_type, arch, machine)

    @classmethod
    def _get_emulator(cls, conn, os_type, domain_type, arch, machine):
        caps = etree.fromstring(conn.getCapabilities())

        for guest in caps.xpath('/capabilities/guest'):
            # Check type
            type_node = cls._xpath_opt(guest, 'os_type')
            if type_node is None or type_node.text != os_type:
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
                                if len(emulator_nodes) == 0:
                                    emulator_nodes = arch_node.xpath(
                                            'emulator')
                                if len(emulator_nodes) != 1:
                                    continue
                                return emulator_nodes[0].text

        # Failed.
        raise DomainXMLError('No suitable emulator for %s/%s/%s/%s' %
                (os_type, domain_type, arch, machine))
