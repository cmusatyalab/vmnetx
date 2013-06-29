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
import sys
import threading

from ...util import ErrorBuffer
from .. import AbstractController, Statistic
from .execute import Machine, MachineMetadata
from .monitor import (ChunkMapMonitor, LineStreamMonitor,
        LoadProgressMonitor, StatMonitor)

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
        self._machine = None
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
        self._machine = Machine(self._metadata, use_spice=self._want_spice)

        # Load configuration
        self.vm_name = self._machine.name
        self.have_memory = self._machine.memory_path is not None
        self.use_spice = self._machine.use_spice
        self.viewer_password = self._machine.viewer_password
        self.max_mouse_rate = self._metadata.domain_xml.max_mouse_rate

        # Set chunk size
        path = os.path.join(self._machine.disk_path, 'stats', 'chunk_size')
        with open(path) as fh:
            self.disk_chunk_size = int(fh.readline().strip())

        # Create monitors
        for name in self.STATS:
            stat = Statistic(name)
            self.disk_stats[name] = stat
            self._monitors.append(StatMonitor(stat, self._machine.disk_path,
                    name))
        self._monitors.append(ChunkMapMonitor(self.disk_chunks,
                self._machine.disk_path))
        log_monitor = LineStreamMonitor(self._machine.log_path)
        log_monitor.connect('line-emitted', self._vmnetfs_log)
        self._monitors.append(log_monitor)
        if self.have_memory:
            self._load_monitor = LoadProgressMonitor(self._machine.memory_path)
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
                self._machine.start_vm(not have_memory)
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
            self.viewer_address = self._machine.viewer_listen_address
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
        if self._machine is not None:
            self._machine.stop_vm()
        self.have_memory = False

    def shutdown(self):
        for monitor in self._monitors:
            monitor.close()
        self.stop_vm()
        if self._machine is not None:
            self._machine.close()
gobject.type_register(LocalController)
