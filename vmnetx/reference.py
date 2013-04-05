#
# vmnetx.reference - Handling of .netx files
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

from lxml import etree
from lxml.builder import ElementMaker
import os

from vmnetx.util import DetailException


NS = 'http://olivearchive.org/xmlns/vmnetx/reference'
NSP = '{' + NS + '}'
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema',
        'reference.xsd')

# We want this to be a public attribute
# pylint: disable=C0103
schema = etree.XMLSchema(etree.parse(SCHEMA_PATH))
# pylint: enable=C0103


class BadReferenceError(DetailException):
    pass


class PackageReference(object):
    def __init__(self, url):
        self.url = url

        # Generate XML
        e = ElementMaker(namespace=NS, nsmap={None: NS})
        tree = e.reference(
            e.url(self.url),
        )
        try:
            schema.assertValid(tree)
        except etree.DocumentInvalid, e:
            raise BadReferenceError(
                    'Generated XML does not validate (bad URL?)', str(e))
        self.xml = etree.tostring(tree, encoding='UTF-8', pretty_print=True,
                xml_declaration=True)

    @classmethod
    def parse(cls, path):
        try:
            tree = etree.parse(path, etree.XMLParser(schema=schema)).getroot()
            return cls(url=tree.get('url'))
        except IOError, e:
            raise BadReferenceError(str(e))
        except etree.XMLSyntaxError, e:
            raise BadReferenceError('Reference XML does not validate', str(e))
