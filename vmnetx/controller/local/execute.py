#
# vmnetx.controller.local.execute - Execution of a VM with libvirt
#
# Copyright (C) 2011-2013 Carnegie Mellon University
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

from calendar import timegm
import json
# pylint doesn't understand hashlib.sha256
# pylint: disable=E0611
from hashlib import sha256
# pylint: enable=E0611
from lxml.builder import ElementMaker
import os
from urlparse import urlsplit, urlunsplit
from wsgiref.handlers import format_date_time as format_rfc1123_date

from ...domain import DomainXML
from ...package import Package
from ...reference import PackageReference, BadReferenceError
from ...util import ensure_dir, get_cache_dir
from .vmnetfs import NS as VMNETFS_NS

class _ReferencedObject(object):
    # pylint doesn't understand named tuples
    # pylint: disable=E1103
    def __init__(self, label, info, username=None, password=None,
            chunk_size=131072):
        self.label = label
        self.username = username
        self.password = password
        self.cookies = info.cookies
        self.url = info.url
        self.offset = info.offset
        self.size = info.size
        self.chunk_size = chunk_size
        self.etag = info.etag
        self.last_modified = info.last_modified

        parsed_url = urlsplit(self.url)
        self._cache_info = json.dumps({
            # Exclude query string from cache path
            'url': urlunsplit((parsed_url.scheme, parsed_url.netloc,
                    parsed_url.path, '', '')),
            'etag': self.etag,
            'last-modified': self.last_modified.isoformat()
                    if self.last_modified else None,
        }, indent=2, sort_keys=True)
        self._urlpath = os.path.join(get_cache_dir(), 'chunks',
                sha256(self._cache_info).hexdigest())
        # Hash collisions will allow cache poisoning!
        self.cache = os.path.join(self._urlpath, label, str(chunk_size))
    # pylint: enable=E1103

    # We must access Cookie._rest to perform case-insensitive lookup of
    # the HttpOnly attribute
    # pylint: disable=W0212
    @property
    def vmnetfs_config(self):
        # Write URL and validators into file for ease of debugging.
        # Defer creation of cache directory until needed.
        ensure_dir(self._urlpath)
        info_file = os.path.join(self._urlpath, 'info')
        if not os.path.exists(info_file):
            with open(info_file, 'w') as fh:
                fh.write(self._cache_info)

        # Return XML image element
        e = ElementMaker(namespace=VMNETFS_NS, nsmap={None: VMNETFS_NS})
        origin = e.origin(
            e.url(self.url),
            e.offset(str(self.offset)),
        )
        if self.last_modified or self.etag:
            validators = e.validators()
            if self.last_modified:
                validators.append(e('last-modified',
                        str(timegm(self.last_modified.utctimetuple()))))
            if self.etag:
                validators.append(e.etag(self.etag))
            origin.append(validators)
        if self.username and self.password:
            credentials = e.credentials(
                e.username(self.username),
                e.password(self.password),
            )
            origin.append(credentials)
        if self.cookies:
            cookies = e.cookies()
            for cookie in self.cookies:
                c = '%s=%s; Domain=%s; Path=%s' % (cookie.name, cookie.value,
                        cookie.domain, cookie.path)
                if cookie.expires:
                    c += '; Expires=%s' % format_rfc1123_date(cookie.expires)
                if cookie.secure:
                    c += '; Secure'
                if 'httponly' in [k.lower() for k in cookie._rest]:
                    c += '; HttpOnly'
                cookies.append(e.cookie(c))
            origin.append(cookies)
        return e.image(
            e.name(self.label),
            e.size(str(self.size)),
            origin,
            e.cache(
                e.path(self.cache),
                e('chunk-size', str(self.chunk_size)),
            ),
        )
    # pylint: enable=W0212


class MachineMetadata(object):
    # pylint doesn't understand named tuples
    # pylint: disable=E1103
    def __init__(self, package_ref, scheme=None, username=None, password=None):
        # Convert package_ref to package URL
        url = package_ref
        parsed = urlsplit(url)
        if parsed.scheme == '':
            # Local file path.  Try to parse the file as a package reference.
            try:
                url = PackageReference.parse(parsed.path).url
            except BadReferenceError:
                # Failed.  Assume it's a package.
                url = urlunsplit(('file', '', os.path.abspath(parsed.path),
                        '', ''))

        # Load package
        self.package = Package(url, scheme=scheme, username=username,
                password=password)

        # Validate domain XML
        self.domain_xml = DomainXML(self.package.domain.data)

        # Create vmnetfs config
        e = ElementMaker(namespace=VMNETFS_NS, nsmap={None: VMNETFS_NS})
        self.vmnetfs_config = e.config()
        self.vmnetfs_config.append(_ReferencedObject('disk',
                self.package.disk, username=username,
                password=password).vmnetfs_config)
        if self.package.memory:
            self.vmnetfs_config.append(_ReferencedObject('memory',
                    self.package.memory, username=username,
                    password=password).vmnetfs_config)
    # pylint: enable=E1103
