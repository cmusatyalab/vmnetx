#
# vmnetx.app - VMNetX GUI application
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

from datetime import datetime
import dbus
import glib
import gobject
import grp
import gtk
import json
import logging
import os
import pipes
import pwd
import signal
import sys
from tempfile import NamedTemporaryFile
import threading

from vmnetx.execute import Machine, MachineMetadata
from vmnetx.package import NeedAuthentication
from vmnetx.system import __version__
from vmnetx.util import get_cache_dir
from vmnetx.view import (VMWindow, LoadProgressWindow, PasswordWindow,
        SaveMediaWindow, ErrorWindow, FatalErrorWindow, IgnorableErrorWindow,
        ErrorBuffer, have_spice_viewer)
from vmnetx.status.monitor import (ImageMonitor, LoadProgressMonitor,
        LineStreamMonitor)

_log = logging.getLogger(__name__)

class _UsernameCache(object):
    def __init__(self):
        self._cachedir = get_cache_dir()
        self._path = os.path.join(self._cachedir, 'usernames')

    def _load(self):
        try:
            with open(self._path) as fh:
                return json.load(fh)
        except IOError:
            return {}

    def get(self, host, realm):
        try:
            return self._load()[host][realm]
        except KeyError:
            return None

    def put(self, host, realm, username):
        map = self._load()
        map.setdefault(host, {})[realm] = username
        with NamedTemporaryFile(dir=self._cachedir, delete=False) as fh:
            json.dump(map, fh)
            fh.write('\n')
        os.rename(fh.name, self._path)


class VMNetXApp(object):
    AUTHORIZER_NAME = 'org.olivearchive.VMNetX.Authorizer'
    AUTHORIZER_PATH = '/org/olivearchive/VMNetX/Authorizer'
    AUTHORIZER_IFACE = 'org.olivearchive.VMNetX.Authorizer'
    RESUME_CHECK_DELAY = 1000  # ms

    def __init__(self, package_ref):
        gobject.threads_init()
        self._username_cache = _UsernameCache()
        self._package_ref = package_ref
        self._machine = None
        self._have_memory = False
        self._wind = None
        self._load_monitor = None
        self._load_window = None
        self._startup_cancelled = False
        self._io_failed = False

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def run(self):
        log_monitor = disk_monitor = None
        try:
            # Attempt to catch SIGTERM.  This is dubious, but not more so
            # than the default handling of SIGINT.
            signal.signal(signal.SIGTERM, self._signal)

            # Verify authorization to mount a FUSE filesystem
            self._ensure_permissions()

            # Fetch and parse metadata
            pw_wind = scheme = username = password = None
            while True:
                try:
                    metadata = MachineMetadata(self._package_ref, scheme,
                            username, password)
                except NeedAuthentication, e:
                    if pw_wind is None:
                        username = self._username_cache.get(e.host, e.realm)
                        pw_wind = PasswordWindow(e.host, e.realm)
                        if username is not None:
                            # Sets focus to password box as a side effect
                            pw_wind.username = username
                    else:
                        pw_wind.fail()
                    if pw_wind.run() != gtk.RESPONSE_OK:
                        pw_wind.destroy()
                        raise KeyboardInterrupt
                    scheme = e.scheme
                    username = pw_wind.username
                    password = pw_wind.password
                    self._username_cache.put(e.host, e.realm, username)
                else:
                    if pw_wind is not None:
                        pw_wind.destroy()
                    break

            # Start vmnetfs
            self._machine = Machine(metadata, use_spice=have_spice_viewer)
            self._have_memory = self._machine.memory_path is not None

            # Create monitors
            log_monitor = LineStreamMonitor(self._machine.log_path)
            log_monitor.connect('line-emitted', self._vmnetfs_log)
            disk_monitor = ImageMonitor(self._machine.disk_path)
            if self._have_memory:
                self._load_monitor = LoadProgressMonitor(
                        self._machine.memory_path)

            # Show main window
            self._wind = VMWindow(self._machine.name, disk_monitor,
                    use_spice=self._machine.using_spice,
                    max_mouse_rate=metadata.domain_xml.max_mouse_rate)
            self._wind.connect('viewer-connect', self._connect)
            self._wind.connect('viewer-disconnect', self._restart)
            self._wind.connect('user-restart', self._user_restart)
            self._wind.connect('user-quit', self._shutdown)
            self._wind.connect('user-screenshot', self._screenshot)
            self._wind.show_all()
            disk_monitor.stats['io_errors'].connect('stat-changed',
                    self._io_error)

            # Show loading window
            if self._have_memory:
                self._load_window = LoadProgressWindow(self._load_monitor,
                        self._wind)
                self._load_window.connect('user-cancel', self._startup_cancel)
                self._load_window.show_all()

            # Start logging
            logging.getLogger().setLevel(logging.INFO)
            _log.info('VMNetX %s starting at %s', __version__,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            _log.info('SPICE viewer %s, qemu support %s',
                    'available' if have_spice_viewer else 'unavailable',
                    'available' if self._machine.have_spice else 'unavailable')

            # Load memory image in the background
            self._start_vm()

            # Run main loop
            gtk.main()
        except (KeyboardInterrupt, SystemExit):
            pass
        except:
            # Show error window with exception
            FatalErrorWindow(self._wind).run()
        finally:
            # Shut down
            logging.shutdown()
            if self._wind is not None:
                self._wind.destroy()
            if disk_monitor is not None:
                disk_monitor.close()
            if log_monitor is not None:
                log_monitor.close()
            if self._machine is not None:
                self._machine.stop_vm()
                self._machine.close()
    # pylint: enable=W0702

    def _signal(self, _signum, _frame):
        raise KeyboardInterrupt

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

    def _start_vm(self):
        threading.Thread(name='vmnetx-startup', target=self._startup).start()

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def _startup(self):
        # Thread function.  Load the memory image, then connect the display
        # viewer.
        try:
            self._machine.start_vm(not self._have_memory)
        except:
            gobject.idle_add(self._startup_error, ErrorBuffer())
        else:
            gobject.idle_add(self._startup_done)
    # pylint: enable=W0702

    def _startup_cancel(self, _obj):
        if not self._startup_cancelled:
            self._startup_cancelled = True
            threading.Thread(name='vmnetx-startup-cancel',
                    target=self._machine.stop_vm).start()
            self._wind.hide()

    def _startup_done(self):
        # Runs in UI thread
        self._wind.connect_viewer(self._machine.viewer_listen_address,
                self._machine.viewer_password)
        if self._have_memory:
            self._load_window.destroy()
            self._load_monitor.close()

    def _startup_error(self, error=None):
        # Runs in UI thread
        if self._have_memory:
            self._load_window.destroy()
            self._load_monitor.close()
            self._have_memory = False
            if not self._startup_cancelled:
                # Try again without memory image
                self._warn_bad_memory()
                self._start_vm()
                return
        if error is not None and not self._startup_cancelled:
            ew = FatalErrorWindow(self._wind, error)
            ew.run()
            ew.destroy()
        self._shutdown()

    def _warn_bad_memory(self):
        self._wind.add_warning('dialog-warning',
                'The memory image could not be loaded.')

    def _connect(self, _obj):
        glib.timeout_add(self.RESUME_CHECK_DELAY,
                self._startup_check_screenshot)

    # pylint doesn't like '\0'
    # pylint: disable=W1401
    def _startup_check_screenshot(self):
        # If qemu doesn't like a memory image, it may sit and spin rather
        # than failing properly.  Recover from this case.
        if not self._have_memory:
            return
        img = self._wind.take_screenshot()
        if img is None:
            return
        black = ('\0' * (img.get_n_channels() * img.get_width() *
                img.get_height() * img.get_bits_per_sample() // 8))
        if img.get_pixels() == black:
            _log.warning('Detected black screen; assuming bad memory image')
            self._warn_bad_memory()
            # Terminate the VM; the viewer-disconnect handler will restart it
            self._machine.stop_vm()
    # pylint: enable=W1401

    def _io_error(self, _monitor, _name, _value):
        # Runs in UI thread
        if not self._io_failed:
            self._io_failed = True
            self._wind.add_warning('dialog-error',
                    'Unable to download disk chunks. ' +
                    'The guest may experience errors.')
            ew = IgnorableErrorWindow(self._wind,
                    'Unable to download disk chunks.\n\nYou may continue, ' +
                    'but the guest will likely encounter unrecoverable ' +
                    'errors.')
            response = ew.run()
            ew.destroy()
            if response == gtk.RESPONSE_OK:
                self._shutdown()

    def _vmnetfs_log(self, _monitor, line):
        _log.warning('%s', line)

    def _user_restart(self, _obj):
        # Just terminate the VM; the viewer-disconnect handler will restart it
        self._machine.stop_vm()

    def _restart(self, _obj):
        self._machine.stop_vm()
        self._have_memory = False
        self._start_vm()

    def _shutdown(self, _obj=None):
        self._wind.show_activity(False)
        self._wind.show_log(False)
        self._wind.hide()
        gobject.idle_add(gtk.main_quit)

    def _screenshot(self, _obj, pixbuf):
        sw = SaveMediaWindow(self._wind, 'Save Screenshot',
                self._machine.name + '.png', pixbuf)
        if sw.run() == gtk.RESPONSE_OK:
            try:
                pixbuf.save(sw.get_filename(), 'png')
            except gobject.GError, e:
                ew = ErrorWindow(self._wind, str(e))
                ew.run()
                ew.destroy()
        sw.destroy()
