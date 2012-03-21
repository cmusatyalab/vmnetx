#
# vmnetx.manifest - Handling of .netx files
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

import collections
from lxml import etree
from lxml.builder import ElementMaker
import os

NS = 'http://olivearchive.org/xmlns/vmnetx/manifest'
NSP = '{' + NS + '}'
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'manifest.xsd')

# We want this to be a public attribute
# pylint: disable=C0103
schema = etree.XMLSchema(etree.parse(SCHEMA_PATH))
# pylint: enable=C0103


class ManifestError(Exception):
    pass


# This is a class, even if pylint doesn't think so
# pylint: disable=C0103
ReferenceInfo = collections.namedtuple('ReferenceInfo', ('location', 'size'))
# pylint: enable=C0103


class Manifest(object):
    def __init__(self, name=None, domain=None, disk=None, memory=None,
            xml=None):
        if xml:
            # Save passed parameters
            self.xml = xml

            # Parse XML
            try:
                tree = etree.fromstring(xml, etree.XMLParser(schema=schema))
                self.name = tree.get('name')
                self.domain = self._make_refinfo(tree.find(NSP + 'domain'))
                self.disk = self._make_refinfo(tree.find(NSP + 'disk'))
                self.memory = self._make_refinfo(tree.find(NSP + 'memory'))
            except etree.XMLSyntaxError, e:
                raise ManifestError(str(e))
        else:
            # Save passed parameters; memory is optional
            assert name and domain and disk
            self.name = name
            self.domain = domain
            self.disk = disk
            self.memory = memory

            # Generate XML
            e = ElementMaker(namespace=NS, nsmap={None: NS})
            tree = e.image(
                e.domain(location=domain.location),
                e.disk(location=disk.location, size=str(disk.size)),
                name=name,
            )
            if memory:
                tree.append(e.memory(location=memory.location,
                        size=str(memory.size)))
            schema.assertValid(tree)
            self.xml = etree.tostring(tree, encoding='UTF-8',
                    pretty_print=True, xml_declaration=True)

    @staticmethod
    def _make_refinfo(element):
        size = element.get('size')
        if size is not None:
            size = int(size)
        return ReferenceInfo(location=element.get('location'), size=size)
