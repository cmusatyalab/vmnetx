#
# vmnetx.package - Handling of .nxpk files
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
import struct
import zipfile

from .source import SourceError, SourceRange
from .system import schemadir
from .util import DetailException

NS = 'http://olivearchive.org/xmlns/vmnetx/package'
NSP = '{' + NS + '}'
SCHEMA_PATH = os.path.join(schemadir, 'package.xsd')

MANIFEST_FILENAME = 'vmnetx-package.xml'
DOMAIN_FILENAME = 'domain.xml'
DISK_FILENAME = 'disk.img'
MEMORY_FILENAME = 'memory.img'


# We want this to be a public attribute
# pylint: disable=invalid-name
schema = etree.XMLSchema(etree.parse(SCHEMA_PATH))
# pylint: enable=invalid-name


class BadPackageError(DetailException):
    pass


class _PackageMember(SourceRange):
    def __init__(self, zip, path, load_data=False):
        source = zip.fp
        try:
            info = zip.getinfo(path)
        except KeyError:
            raise BadPackageError('Path "%s" missing from package' % path)
        # ZipInfo.extra is the extra field from the central directory file
        # header, which may be different from the extra field in the local
        # file header.  So we need to read the local file header to determine
        # its size.
        header_fmt = '<4s5H3I2H'
        header_len = struct.calcsize(header_fmt)
        source.seek(info.header_offset)
        magic, _, flags, compression, _, _, _, _, _, name_len, extra_len = \
                struct.unpack(header_fmt, source.read(header_len))
        if magic != zipfile.stringFileHeader:
            raise BadPackageError('Member "%s" has invalid header' % path)
        if compression != zipfile.ZIP_STORED:
            raise BadPackageError('Member "%s" is compressed' % path)
        if flags & 0x1:
            raise BadPackageError('Member "%s" is encrypted' % path)
        SourceRange.__init__(self, source,
                info.header_offset + header_len + name_len + extra_len,
                info.file_size, load_data)


class Package(object):
    def __init__(self, source):
        self.url = source.url

        try:
            zip = zipfile.ZipFile(source, 'r')

            # Parse manifest
            if MANIFEST_FILENAME not in zip.namelist():
                raise BadPackageError('Package does not contain manifest')
            xml = zip.read(MANIFEST_FILENAME)
            tree = etree.fromstring(xml, etree.XMLParser(schema=schema))

            # Create attributes
            self.name = tree.get('name')
            self.domain = _PackageMember(zip,
                    tree.find(NSP + 'domain').get('path'), True)
            self.disk = _PackageMember(zip,
                    tree.find(NSP + 'disk').get('path'))
            memory = tree.find(NSP + 'memory')
            if memory is not None:
                self.memory = _PackageMember(zip, memory.get('path'))
            else:
                self.memory = None
        except etree.XMLSyntaxError, e:
            raise BadPackageError('Manifest XML does not validate', str(e))
        except (zipfile.BadZipfile, SourceError), e:
            raise BadPackageError(str(e))

    @classmethod
    def create(cls, out, name, domain_xml, disk_path, memory_path=None):
        # Generate manifest XML
        e = ElementMaker(namespace=NS, nsmap={None: NS})
        tree = e.image(
            e.domain(path=DOMAIN_FILENAME),
            e.disk(path=DISK_FILENAME),
            name=name,
        )
        if memory_path:
            tree.append(e.memory(path=MEMORY_FILENAME))
        schema.assertValid(tree)
        xml = etree.tostring(tree, encoding='UTF-8', pretty_print=True,
                xml_declaration=True)

        # Write package
        zip = zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED, True)
        zip.comment = 'VMNetX package'
        zip.writestr(MANIFEST_FILENAME, xml)
        zip.writestr(DOMAIN_FILENAME, domain_xml)
        if memory_path is not None:
            zip.write(memory_path, MEMORY_FILENAME)
        zip.write(disk_path, DISK_FILENAME)
        zip.close()
