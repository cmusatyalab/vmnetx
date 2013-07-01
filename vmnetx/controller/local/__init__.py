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

import dbus
import gobject
import grp
import libvirt
import logging
import os
import pipes
import pwd
import subprocess
import sys
import threading
import uuid

from ...domain import DomainXML
from ...util import ErrorBuffer
from .. import AbstractController, MachineExecutionError, Statistic
from .execute import MachineMetadata
from .monitor import (ChunkMapMonitor, LineStreamMonitor,
        LoadProgressMonitor, StatMonitor)
from .vmnetfs import VMNetFS

_log = logging.getLogger(__name__)

# Check libvirt version
assert(libvirt.getVersion() >= 9008) # 0.9.8

# Squash redundant reporting of libvirt errors to stderr.  This modifies
# global state, since the Python bindings don't provide a way to do this
# per-connection.
libvirt.registerErrorHandler(lambda _ctx, _error: None, None)


class LocalController(AbstractController):
    AUTHORIZER_NAME = 'org.olivearchive.VMNetX.Authorizer'
    AUTHORIZER_PATH = '/org/olivearchive/VMNetX/Authorizer'
    AUTHORIZER_IFACE = 'org.olivearchive.VMNetX.Authorizer'
    STATS = ('bytes_read', 'bytes_written', 'chunk_dirties', 'chunk_fetches',
            'io_errors')

    def __init__(self, package_ref, use_spice):
        AbstractController.__init__(self)
        self._package_ref = package_ref
        self._want_spice = use_spice
        self._metadata = None
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._memory_image_path = None
        self._fs = None
        self._conn = None
        self._domain_xml = None
        self._startup_cancelled = False
        self._monitors = []
        self._load_monitor = None

    # Should be called before we open any windows, since we may re-exec
    # the whole program if we need to update the group list.
    def initialize(self):
        # Verify authorization to mount a FUSE filesystem
        self._ensure_permissions()

        # Authenticate and fetch metadata
        self._metadata = MachineMetadata(self._package_ref, self.scheme,
                self.username, self.password)

        # Start vmnetfs
        self._fs = VMNetFS(self._metadata.vmnetfs_config)
        self._fs.start()
        log_path = os.path.join(self._fs.mountpoint, 'log')
        disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(disk_path, 'image')
        if self._metadata.package.memory:
            memory_path = os.path.join(self._fs.mountpoint, 'memory')
            self._memory_image_path = os.path.join(memory_path, 'image')
        else:
            memory_path = self._memory_image_path = None

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')

        # Get emulator path
        emulator = self._metadata.domain_xml.detect_emulator(self._conn)

        # Detect SPICE support
        self.use_spice = self._want_spice and self._spice_is_usable(emulator)

        # Get execution domain XML
        self._domain_xml = self._metadata.domain_xml.get_for_execution(
                self._domain_name, emulator, disk_image_path,
                self.viewer_password, use_spice=self.use_spice).xml

        # Set configuration
        self.vm_name = self._metadata.package.name
        self.have_memory = memory_path is not None
        self.max_mouse_rate = self._metadata.domain_xml.max_mouse_rate

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
                self.viewer_address = (
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
                    target=self._machine.stop_vm).start()

    def stop_vm(self):
        if self._conn is not None:
            try:
                self._conn.lookupByName(self._domain_name).destroy()
            except libvirt.libvirtError:
                # Assume that the VM did not exist or was already dying
                pass
            self.viewer_address = None
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
