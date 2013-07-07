#
# vmnetx.controller.local - Execution of a VM with libvirt
#
# Copyright (C) 2008-2013 Carnegie Mellon University
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

import base64
from calendar import timegm
import dbus
import gobject
import grp
# pylint doesn't understand hashlib.sha256
# pylint: disable=E0611
from hashlib import sha256
# pylint: enable=E0611
import json
import libvirt
import logging
from lxml.builder import ElementMaker
import os
import pipes
import pwd
import subprocess
import sys
import threading
from urlparse import urlsplit, urlunsplit
import uuid
from wsgiref.handlers import format_date_time as format_rfc1123_date

from ...domain import DomainXML
from ...package import Package
from ...util import ErrorBuffer, ensure_dir, get_cache_dir
from .. import Controller, MachineExecutionError, Statistic
from .monitor import (ChunkMapMonitor, LineStreamMonitor,
        LoadProgressMonitor, StatMonitor)
from .virtevent import LibvirtEventImpl
from .vmnetfs import VMNetFS, NS as VMNETFS_NS

_log = logging.getLogger(__name__)

# Check libvirt version
assert(libvirt.getVersion() >= 9008) # 0.9.8

# Squash redundant reporting of libvirt errors to stderr.  This modifies
# global state, since the Python bindings don't provide a way to do this
# per-connection.
libvirt.registerErrorHandler(lambda _ctx, _error: None, None)

# Enable libvirt event reporting.  Also modifies global state.
LibvirtEventImpl().register()


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


class LocalController(Controller):
    AUTHORIZER_NAME = 'org.olivearchive.VMNetX.Authorizer'
    AUTHORIZER_PATH = '/org/olivearchive/VMNetX/Authorizer'
    AUTHORIZER_IFACE = 'org.olivearchive.VMNetX.Authorizer'
    STATS = ('bytes_read', 'bytes_written', 'chunk_dirties', 'chunk_fetches',
            'io_errors')

    def __init__(self, url, use_spice):
        Controller.__init__(self)
        self._url = url
        self._want_spice = use_spice
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._package = None
        self._memory_image_path = None
        self._fs = None
        self._conn = None
        self._domain_xml = None
        self._startup_cancelled = False
        self._viewer_address = None
        self._monitors = []
        self._load_monitor = None

    # Should be called before we open any windows, since we may re-exec
    # the whole program if we need to update the group list.
    def initialize(self):
        # Verify authorization to mount a FUSE filesystem
        self._ensure_permissions()

        # Load package
        package = Package(self._url, scheme=self.scheme,
                username=self.username, password=self.password)

        # Validate domain XML
        domain_xml = DomainXML(package.domain.data)

        # Create vmnetfs config
        e = ElementMaker(namespace=VMNETFS_NS, nsmap={None: VMNETFS_NS})
        vmnetfs_config = e.config()
        vmnetfs_config.append(_ReferencedObject('disk', package.disk,
                username=self.username, password=self.password).vmnetfs_config)
        if package.memory:
            vmnetfs_config.append(_ReferencedObject('memory', package.memory,
                    username=self.username,
                    password=self.password).vmnetfs_config)

        # Start vmnetfs
        self._fs = VMNetFS(vmnetfs_config)
        self._fs.start()
        log_path = os.path.join(self._fs.mountpoint, 'log')
        disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(disk_path, 'image')
        if package.memory:
            memory_path = os.path.join(self._fs.mountpoint, 'memory')
            self._memory_image_path = os.path.join(memory_path, 'image')
        else:
            memory_path = self._memory_image_path = None

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')
        self._conn.domainEventRegisterAny(None,
                libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self._lifecycle_event,
                None)

        # Get emulator path
        emulator = domain_xml.detect_emulator(self._conn)

        # Detect SPICE support
        self.use_spice = self._want_spice and self._spice_is_usable(emulator)
        # VNC limits passwords to 8 characters
        self.viewer_password = base64.urlsafe_b64encode(os.urandom(
                15 if self.use_spice else 6))

        # Get execution domain XML
        self._domain_xml = domain_xml.get_for_execution(self._domain_name,
                emulator, disk_image_path, self.viewer_password,
                use_spice=self.use_spice).xml

        # Set configuration
        self.vm_name = package.name
        self.have_memory = memory_path is not None
        self.max_mouse_rate = domain_xml.max_mouse_rate

        # Set chunk size
        path = os.path.join(disk_path, 'stats', 'chunk_size')
        with open(path) as fh:
            self.disk_chunk_size = int(fh.readline().strip())

        # Create monitors
        for name in self.STATS:
            stat = Statistic(name)
            self.disk_stats[name] = stat
            self._monitors.append(StatMonitor(stat, disk_path, name))
        self._monitors.append(ChunkMapMonitor(self.disk_chunks, disk_path))
        log_monitor = LineStreamMonitor(log_path)
        log_monitor.connect('line-emitted', self._vmnetfs_log)
        self._monitors.append(log_monitor)
        if self.have_memory:
            self._load_monitor = LoadProgressMonitor(memory_path)
            self._load_monitor.connect('progress', self._load_progress)

    def _ensure_permissions(self):
        try:
            obj = dbus.SystemBus().get_object(self.AUTHORIZER_NAME,
                    self.AUTHORIZER_PATH)
            # We would like an infinite timeout, but dbus-python won't allow
            # it.  Pass the longest timeout dbus-python will accept.
            groups = obj.EnableFUSEAccess(dbus_interface=self.AUTHORIZER_IFACE,
                    timeout=2147483)
        except dbus.exceptions.DBusException, e:
            # dbus-python exception handling is problematic.
            if 'Authorization failed' in str(e):
                # The user knows this already; don't show a FatalErrorWindow.
                sys.exit(1)
            else:
                # If we can't contact the authorizer (perhaps because D-Bus
                # wasn't configured correctly), proceed as though we have
                # sufficient permission, and possibly fail later.  This
                # avoids unnecessary failures in the common case.
                return

        if groups:
            # Make sure all of the named groups are in our supplementary
            # group list, which will not be true if EnableFUSEAccess() just
            # added us to those groups (or if it did so earlier in this
            # login session).  We have to do this one group at a time, and
            # then restore our primary group afterward.
            def switch_group(group):
                cmd = ' '.join(pipes.quote(a) for a in
                        [sys.executable] + sys.argv)
                os.execlp('sg', 'sg', group, '-c', cmd)
            cur_gids = os.getgroups()
            for group in groups:
                if grp.getgrnam(group).gr_gid not in cur_gids:
                    switch_group(group)
            primary_gid = pwd.getpwuid(os.getuid()).pw_gid
            if os.getgid() != primary_gid:
                switch_group(grp.getgrgid(primary_gid).gr_name)

    def _spice_is_usable(self, emulator):
        '''Determine whether emulator supports SPICE.'''
        proc = subprocess.Popen([emulator, '-spice', 'foo'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True)
        out, err = proc.communicate()
        out += err
        if 'invalid option' in out or 'spice is not supported' in out:
            # qemu is too old to support SPICE, or SPICE is not compiled in
            return False
        return True

    def _vmnetfs_log(self, _monitor, line):
        _log.warning('%s', line)

    def start_vm(self):
        if self.have_memory:
            self.emit('startup-progress', 0, self._load_monitor.chunks)
        threading.Thread(name='vmnetx-startup', target=self._startup).start()

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def _startup(self):
        # Thread function.
        try:
            have_memory = self.have_memory
            try:
                if have_memory:
                    # Does not return domain handle
                    # Does not allow autodestroy
                    self._conn.restoreFlags(self._memory_image_path,
                            self._domain_xml,
                            libvirt.VIR_DOMAIN_SAVE_RUNNING)
                    domain = self._conn.lookupByName(self._domain_name)
                else:
                    domain = self._conn.createXML(self._domain_xml,
                            libvirt.VIR_DOMAIN_START_AUTODESTROY)

                # Get viewer socket address
                domain_xml = DomainXML(domain.XMLDesc(0),
                        validate=DomainXML.VALIDATE_NONE, safe=False)
                self._viewer_address = (
                    domain_xml.viewer_host or '127.0.0.1',
                    domain_xml.viewer_port
                )
            except libvirt.libvirtError, e:
                raise MachineExecutionError(str(e))
            finally:
                if have_memory:
                    gobject.idle_add(self._load_monitor.close)
        except:
            if self._startup_cancelled:
                gobject.idle_add(self.emit, 'startup-cancelled')
            elif self.have_memory:
                self.have_memory = False
                gobject.idle_add(self.emit, 'startup-rejected-memory')
                # Retry without memory image
                self._startup()
            else:
                gobject.idle_add(self.emit, 'startup-failed', ErrorBuffer())
        else:
            gobject.idle_add(self.emit, 'startup-complete')
    # pylint: enable=W0702

    def _load_progress(self, _obj, count, total):
        if self.have_memory and not self._startup_cancelled:
            self.emit('startup-progress', count, total)

    def startup_cancel(self):
        if not self._startup_cancelled:
            self._startup_cancelled = True
            threading.Thread(name='vmnetx-startup-cancel',
                    target=self.stop_vm).start()

    def connect_viewer(self, data):
        def done(fd=None, error=None):
            assert error is not None or fd is not None
            if error is not None:
                self.emit('viewer-connection-failed', error, data)
            else:
                self.emit('viewer-connection-open', fd, data)

        if self._viewer_address is None:
            done(error='No viewer address')
            return
        self._connect_socket(self._viewer_address, done)

    def _lifecycle_event(self, _conn, domain, event, _detail, _data):
        if domain.name() == self._domain_name:
            if event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
                self.emit('vm-stopped')

    def stop_vm(self):
        if self._conn is not None:
            try:
                self._conn.lookupByName(self._domain_name).destroy()
            except libvirt.libvirtError:
                # Assume that the VM did not exist or was already dying
                pass
            self._viewer_address = None
        self.have_memory = False

    def shutdown(self):
        for monitor in self._monitors:
            monitor.close()
        self.stop_vm()
        # Close libvirt connection
        if self._conn is not None:
            self._conn.close()
        # Terminate vmnetfs
        if self._fs is not None:
            self._fs.terminate()
gobject.type_register(LocalController)
