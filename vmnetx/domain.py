#
# vmnetx.domain - Handling of libvirt domain XML
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

from collections import namedtuple
from lxml import etree
from lxml.builder import ElementMaker
import os
import random
import subprocess
from tempfile import NamedTemporaryFile
import uuid

from .system import schemadir
from .util import DetailException

# vmnetx-specific metadata extensions
NS = 'http://olivearchive.org/xmlns/vmnetx/domain-metadata'
NSP = '{' + NS + '}'

SAFE_SCHEMA_PATH = os.path.join(schemadir, 'domain.xsd')
STRICT_SCHEMA_PATH = os.path.join(schemadir, 'libvirt', 'domain.rng')

# We want these to be public attributes
# pylint: disable=invalid-name
safe_schema = etree.XMLSchema(etree.parse(SAFE_SCHEMA_PATH))
strict_schema = etree.RelaxNG(file=STRICT_SCHEMA_PATH)
# pylint: enable=invalid-name


class DomainXMLError(DetailException):
    pass


_Emulator = namedtuple('_Emulator', ('os_type', 'domain_type', 'arch',
        'machine', 'canonical_machine', 'path'))


class DomainXML(object):
    # Do not validate domain XML against any libvirt schema
    VALIDATE_NONE = 0
    # Validate domain XML against schema from installed libvirt
    VALIDATE_NORMAL = 1
    # Validate domain XML against schema from oldest supported libvirt
    VALIDATE_STRICT = 2

    def __init__(self, xml, validate=VALIDATE_NORMAL, safe=True):
        self.xml = xml
        self._validate(mode=validate, safe=safe)

        # Get disk path and type
        tree = etree.fromstring(xml)
        in_disk = self._xpath_one(tree, '/domain/devices/disk[@device="disk"]')
        self.disk_path = self._xpath_one(in_disk, 'source/@file')
        self.disk_type = self._xpath_one(in_disk, 'driver/@type')

        # Extract vmnetx-specific metadata
        meta = self._xpath_opt(tree, '/domain/metadata/v:vmnetx')
        self.max_mouse_rate = self._xpath_opt(meta, 'v:limit_mouse_rate/@hz',
                int)

        # Extract runtime settings, if present
        self.viewer_host = self._xpath_opt(tree,
                '/domain/devices/graphics/@listen')
        self.viewer_port = self._xpath_opt(tree,
                '/domain/devices/graphics/@port', int)

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

    def _validate(self, mode, safe=True):
        # Parse XML
        try:
            tree = etree.fromstring(self.xml)
        except etree.XMLSyntaxError, e:
            raise DomainXMLError('Domain XML does not parse', str(e))

        if safe:
            # Ensure XML contains no prohibited configuration
            try:
                safe_schema.assertValid(tree)
            except etree.DocumentInvalid, e:
                raise DomainXMLError('Domain XML contains prohibited elements',
                        str(e))

        if mode == self.VALIDATE_STRICT:
            # Validate against schema from minimum supported libvirt
            # (in case our schema is accidentally more permissive than the
            # libvirt schema)
            try:
                strict_schema.assertValid(tree)
            except etree.DocumentInvalid, e:
                raise DomainXMLError(
                        'Domain XML unsupported by oldest supported libvirt',
                        str(e))
        elif mode == self.VALIDATE_NORMAL:
            # Validate against schema from installed libvirt
            with NamedTemporaryFile(prefix='vmnetx-xml-') as fh:
                fh.write(self.xml)
                fh.flush()

                proc = subprocess.Popen(['virt-xml-validate', fh.name,
                        'domain'], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                out, err = proc.communicate()
                if proc.returncode:
                    raise DomainXMLError(
                            'Domain XML unsupported by installed libvirt',
                            (out.strip() + '\n' + err.strip()).strip())

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
        return type(self)(self._to_xml(tree), validate=self.VALIDATE_STRICT)

    def detect_emulator(self, conn):
        '''Return the emulator path that we should use for this domain XML.
        (*Not* the one actually specified in the XML document.)'''
        return self._get_emulator_for_domain(conn,
                etree.fromstring(self.xml)).path

    def get_for_execution(self, name, emulator, disk_image_path,
            viewer_password):
        # Parse XML
        tree = etree.fromstring(self.xml)

        # Ensure machine name and UUID are unique
        self._xpath_one(tree, '/domain/name').text = name
        self._xpath_one(tree, '/domain/uuid').text = str(uuid.uuid4())

        # Update path to emulator
        self._xpath_one(tree, '/domain/devices/emulator').text = emulator

        # Update path to hard disk
        self._xpath_one(tree,
                '/domain/devices/disk[@device="disk"]/source').set('file',
                disk_image_path)

        # Remove graphics declarations
        # (legacy VMs may specify VNC graphics)
        devices_node = self._xpath_one(tree, '/domain/devices')
        for node in tree.xpath('/domain/devices/graphics'):
            devices_node.remove(node)

        # Add new graphics declaration
        graphics_node = etree.SubElement(devices_node, 'graphics')
        graphics_node.set('type', 'spice')
        # Disable clipboard sharing for safety
        clipboard_node = etree.SubElement(graphics_node, 'clipboard')
        clipboard_node.set('copypaste', 'no')
        graphics_node.set('autoport', 'yes')
        graphics_node.set('passwd', viewer_password)

        # Configure RTC to avoid long delays during restore
        clock_node = self._xpath_opt(tree, '/domain/clock')
        if clock_node is None:
            domain_node = self._xpath_one(tree, '/domain')
            clock_node = etree.SubElement(domain_node, 'clock')
            clock_node.set('offset', 'localtime')
        if self._xpath_opt(tree,
                '/domain/clock/timer[@name="rtc"][@track="guest"]') is None:
            timer_node = etree.SubElement(clock_node, 'timer')
            timer_node.set('name', 'rtc')
            timer_node.set('track', 'guest')

        # Return new instance
        return type(self)(self._to_xml(tree), safe=False)

    @classmethod
    def get_template(cls, conn, name, disk_path, disk_type, memory_mb,
            use_64bit=False):
        rand = random.SystemRandom()
        mac_address = ':'.join(['02'] + ['%02x' % rand.randint(0, 255)
                for _ in range(5)])
        arch = 'x86_64' if use_64bit else 'i686'
        e = ElementMaker(nsmap={'v': NS})
        tree = e.domain(
            e.name(name),
            e.uuid(str(uuid.uuid4())),
            e.memory(
                str(memory_mb),
                unit='MiB',
            ),
            e.vcpu('1'),
            e.os(
                e.type(
                    'hvm',
                    arch=arch,
                    machine='pc',
                ),
                e.boot(
                    dev='hd',
                ),
            ),
            e.cpu(
                e.model('kvm64' if use_64bit else 'qemu32'),
                e.topology(
                    sockets='1',
                    cores='1',
                    threads='1',
                ),
                match='exact',
            ),
            e.features(
                e.acpi(),
                e.apic(),
                e.pae(),
            ),
            e.clock(
                e.timer(
                    name='rtc',
                    track='guest',
                ),
                offset='localtime',
            ),
            e.devices(
                e.emulator(
                    cls._get_emulator(conn, 'hvm', 'kvm', arch, 'pc').path
                ),
                e.disk(
                    e.driver(
                        name='qemu',
                        type=disk_type,
                    ),
                    e.source(
                        file=disk_path,
                    ),
                    e.target(
                        dev='hda',
                        bus='ide',
                    ),
                    e.address(
                        type='drive',
                        controller='0',
                        bus='0',
                        unit='0',
                    ),
                    type='file',
                    device='disk',
                ),
                e.controller(
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x01',
                        function='0x2',
                    ),
                    type='usb',
                    index='0',
                ),
                e.controller(
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x01',
                        function='0x1',
                    ),
                    type='ide',
                    index='0',
                ),
                e.interface(
                    e.mac(
                        address=mac_address,
                    ),
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x03',
                        function='0x0',
                    ),
                    e.model(
                        type='e1000',
                    ),
                    type='user',
                ),
                e.input(
                    type='mouse',
                    bus='ps2',
                ),
                e.graphics(
                    type='spice',
                    autoport='yes',
                ),
                e.sound(
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x04',
                        function='0x0',
                    ),
                    model='ac97',
                ),
                e.video(
                    e.model(
                        type='qxl',
                        vram='65536',
                        heads='1',
                    ),
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x02',
                        function='0x0',
                    ),
                ),
                e.memballoon(
                    e.address(
                        type='pci',
                        domain='0x0000',
                        bus='0x00',
                        slot='0x06',
                        function='0x0',
                    ),
                    model='virtio',
                ),
            ),
            type='kvm',
        )
        return cls(cls._to_xml(tree), validate=cls.VALIDATE_STRICT)

    @classmethod
    def _try_remove_attr(cls, tree, xpath, attr, required_value):
        for el in tree.xpath(xpath):
            if el.get(attr) == required_value:
                del el.attrib[attr]

    @classmethod
    def make_backward_compatible(cls, conn, xml):
        # Remove backward-incompatible default attributes which libvirt adds
        # to domain XML during defineXML(), and return new XML.  Any
        # non-default attributes will be left alone and will probably go on
        # to cause a validation error.  This is a class method because of
        # the unusual requirement for a libvirt connection.

        try:
            tree = etree.fromstring(xml)
        except etree.XMLSyntaxError, e:
            raise DomainXMLError('Domain XML does not parse', str(e))

        # Deprecated legacy syntax
        cls._try_remove_attr(tree, '/domain/devices/graphics', 'port', '-1')

        # >= 1.0.5
        for el in tree.xpath('/domain/devices/controller[@type="pci"]' +
                '[@index="0"][@model="pci-root"]'):
            if not el.getchildren():
                el.getparent().remove(el)

        # >= 1.0.2
        for el in tree.xpath('/domain/devices/video/model'):
            if 'ram' in el.attrib and el.get('ram') == el.get('vram'):
                del el.attrib['ram']

        emulator = cls._get_emulator_for_domain(conn, tree)
        if (emulator.os_type == 'hvm' and emulator.domain_type == 'kvm' and
                emulator.arch in ('i686', 'x86_64') and
                emulator.machine == 'pc'):
            el = cls._xpath_one(tree, '/domain/os/type')
            if el.get('machine') == emulator.canonical_machine:
                el.set('machine', emulator.machine)

        return cls._to_xml(tree)

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

        # If the host is 32-bit and the VM is 64-bit, fail and return a nice
        # message explaining the problem.  The capabilities XML on a 32-bit
        # system should really tell us that there's no emulator for 64-bit
        # KVM, but at least on Ubuntu Trusty it instead provides one that
        # doesn't work.
        host_arch = cls._xpath_one(caps, '/capabilities/host/cpu/arch').text
        if arch == 'x86_64' and host_arch != 'x86_64':
            raise DomainXMLError('This is a 64-bit virtual machine, but ' +
                    'your system cannot run 64-bit virtual machines.')

        candidate = None
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
                    canonical_machine = machine_node.get('canonical')
                    if (machine_node.text == machine or
                            canonical_machine == machine):
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
                                emulator = _Emulator(os_type, domain_type,
                                        arch, machine_node.text,
                                        canonical_machine or machine_node.text,
                                        emulator_nodes[0].text)
                                if canonical_machine is not None:
                                    return emulator
                                else:
                                    # Save candidate, wait for one with
                                    # "canonical" attribute
                                    candidate = emulator

        # Return saved candidate, if any
        if candidate is not None:
            return candidate

        # Failed.
        raise DomainXMLError('No suitable emulator for %s/%s/%s/%s' %
                (os_type, domain_type, arch, machine))
