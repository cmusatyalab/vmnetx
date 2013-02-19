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
import os
import re
import requests
import tempfile
from urlparse import urlsplit
import uuid

try:
    from selinux import chcon
except ImportError:
    def chcon(*_args, **_kwargs):
        pass

from vmnetx.domain import DomainXML
from vmnetx.manifest import Manifest
from vmnetx.vmnetfs import VMNetFS

SOCKET_DIR_CONTEXT = 'unconfined_u:object_r:virt_home_t:s0'

class NeedAuthentication(Exception):
    def __init__(self, host, realm, scheme):
        Exception.__init__(self, 'Authentication required')
        self.host = host
        self.realm = realm
        self.scheme = scheme


class MachineExecutionError(Exception):
    pass


class _ReferencedObject(object):
    def __init__(self, info, chunk_size=131072):
        self.url = info.location
        self.size = info.size
        self.chunk_size = chunk_size

        basepath = os.path.expanduser(os.path.join('~', '.vmnetx', 'cache'))
        # Exclude query string from cache path
        parsed_url = urlsplit(self.url)
        self.cache = os.path.realpath(os.path.join(basepath, str(chunk_size),
                parsed_url.scheme, parsed_url.netloc,
                parsed_url.path.lstrip('/')))
        # Ensure a crafted URL can't escape the cache directory
        if not self.cache.startswith(basepath):
            raise MachineExecutionError('Invalid object URL')

        self.vmnetfs_args = [self.url, self.cache, str(self.size),
                str(self.chunk_size)]


class MachineMetadata(object):
    # pylint doesn't understand named tuples, and is confused by the return
    # type of requests.get()
    # pylint: disable=E1101,E1103
    def __init__(self, manifest_path, scheme=None, username=None,
            password=None):
        # Parse manifest
        with open(manifest_path) as fh:
            manifest = Manifest(xml=fh.read())
        self.name = manifest.name
        self.have_memory = manifest.memory is not None

        # Fetch and validate domain XML
        domain = _ReferencedObject(manifest.domain)
        try:
            if scheme == 'Basic':
                auth = (username, password)
            elif scheme == 'Digest':
                auth = requests.auth.HTTPDigestAuth(username, password)
            elif scheme is None:
                auth = None
            else:
                raise ValueError('Unknown authentication scheme')
            resp = requests.get(domain.url, auth=auth)
            if resp.status_code == 401:
                # Assumes a single challenge.
                scheme, parameters = resp.headers['WWW-Authenticate'].split(
                        None, 1)
                if scheme != 'Basic' and scheme != 'Digest':
                    raise MachineExecutionError('Server requested unknown ' +
                            'authentication scheme: %s' % scheme)
                host = urlsplit(domain.url).netloc
                for param in parameters.split(', '):
                    match = re.match('^realm=\"([^"]*)\"$', param)
                    if match:
                        raise NeedAuthentication(host, match.group(1), scheme)
                raise MachineExecutionError('Unknown authentication realm')
            resp.raise_for_status()
            self.domain_xml = DomainXML(resp.content)
        except requests.exceptions.RequestException, e:
            raise MachineExecutionError(str(e))

        # Create vmnetfs arguments
        self.vmnetfs_args = [username or '', password or ''] + \
                _ReferencedObject(manifest.disk).vmnetfs_args
        if self.have_memory:
            self.vmnetfs_args.extend(_ReferencedObject(manifest.memory).
                    vmnetfs_args)
    # pylint: enable=E1101,E1103


class Machine(object):
    def __init__(self, metadata):
        self.name = metadata.name
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._vnc_socket_dir = tempfile.mkdtemp(prefix='vmnetx-socket-')
        self.vnc_listen_address = os.path.join(self._vnc_socket_dir, 'vnc')

        # Fix socket dir SELinux context
        try:
            chcon(self._vnc_socket_dir, SOCKET_DIR_CONTEXT)
        except OSError:
            pass

        # Start vmnetfs
        self._fs = VMNetFS(metadata.vmnetfs_args)
        self._fs.start()
        self.disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(self.disk_path, 'image')
        if metadata.have_memory:
            self.memory_path = os.path.join(self._fs.mountpoint, 'memory')
            self._memory_image_path = os.path.join(self.memory_path, 'image')
        else:
            self.memory_path = self._memory_image_path = None

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')

        # Get execution domain XML
        self._domain_xml = metadata.domain_xml.get_for_execution(
                self._conn, self._domain_name, disk_image_path,
                self.vnc_listen_address).xml

    def start_vm(self, cold=False):
        try:
            if not cold and self._memory_image_path is not None:
                # Does not return domain handle
                # Does not allow autodestroy
                self._conn.restoreFlags(self._memory_image_path,
                        self._domain_xml, libvirt.VIR_DOMAIN_SAVE_RUNNING)
            else:
                self._conn.createXML(self._domain_xml,
                        libvirt.VIR_DOMAIN_START_AUTODESTROY)
        except libvirt.libvirtError, e:
            raise MachineExecutionError(str(e))

    def stop_vm(self):
        try:
            self._conn.lookupByName(self._domain_name).destroy()
        except libvirt.libvirtError:
            # Assume that the VM did not exist or was already dying
            pass

    def close(self):
        # Close libvirt connection
        self._conn.close()

        # Delete VNC socket
        try:
            os.unlink(self.vnc_listen_address)
        except OSError:
            pass
        try:
            os.rmdir(self._vnc_socket_dir)
        except OSError:
            pass

        # Terminate vmnetfs
        self._fs.terminate()
