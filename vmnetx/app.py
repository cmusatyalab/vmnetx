#
# vmnetx.app - VMNetX GUI application
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

import gobject
import gtk
import json
import os
import signal
from tempfile import NamedTemporaryFile
import threading

from vmnetx.execute import Machine, MachineMetadata, NeedAuthentication
from vmnetx.view import (VMWindow, LoadProgressWindow, PasswordWindow,
        ErrorWindow, ErrorBuffer)
from vmnetx.status.monitor import ImageMonitor, LoadProgressMonitor

class _UsernameCache(object):
    def __init__(self):
        self._configdir = os.path.expanduser('~/.vmnetx')
        if not os.path.exists(self._configdir):
            os.makedirs(self._configdir)
        self._path = os.path.join(self._configdir, 'usernames')

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
        with NamedTemporaryFile(dir=self._configdir, delete=False) as fh:
            json.dump(map, fh)
            fh.write('\n')
        os.rename(fh.name, self._path)


class VMNetXApp(object):
    def __init__(self, manifest_file):
        gobject.threads_init()
        self._username_cache = _UsernameCache()
        self._manifest_file = manifest_file
        self._machine = None
        self._have_memory = False
        self._wind = None
        self._load_monitor = None
        self._load_window = None
        self._startup_cancelled = False

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def run(self):
        disk_monitor = None
        try:
            # Attempt to catch SIGTERM.  This is dubious, but not more so
            # than the default handling of SIGINT.
            signal.signal(signal.SIGTERM, self._signal)

            # Fetch and parse metadata
            pw_wind = scheme = username = password = None
            while True:
                try:
                    metadata = MachineMetadata(self._manifest_file, scheme,
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
            self._machine = Machine(metadata)
            self._have_memory = self._machine.memory_path is not None

            # Create monitors
            disk_monitor = ImageMonitor(self._machine.disk_path)
            if self._have_memory:
                self._load_monitor = LoadProgressMonitor(
                        self._machine.memory_path)

            # Show main window
            self._wind = VMWindow(self._machine.name,
                    self._machine.vnc_listen_address, disk_monitor)
            self._wind.connect('vnc-disconnect', self._restart)
            self._wind.connect('user-restart', self._user_restart)
            self._wind.connect('user-quit', self._shutdown)
            self._wind.show_all()

            # Show loading window
            if self._have_memory:
                self._load_window = LoadProgressWindow(self._load_monitor,
                        self._wind)
                self._load_window.connect('user-cancel', self._startup_cancel)
                self._load_window.show_all()

            # Load memory image in the background
            self._start_vm()

            # Run main loop
            gtk.main()
        except KeyboardInterrupt:
            pass
        except:
            # Show error window with exception
            ErrorWindow(self._wind).run()
        finally:
            # Shut down
            if self._wind is not None:
                self._wind.destroy()
            if disk_monitor is not None:
                disk_monitor.close()
            if self._machine is not None:
                self._machine.stop_vm()
                self._machine.close()
    # pylint: enable=W0702

    def _signal(self, _signum, _frame):
        raise KeyboardInterrupt

    def _start_vm(self, cold=False):
        threading.Thread(name='vmnetx-startup', target=self._startup,
                kwargs={'cold': cold}).start()

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def _startup(self, cold):
        # Thread function.  Load the memory image, then connect the VNC
        # viewer.
        try:
            self._machine.start_vm(cold)
        except:
            if cold:
                gobject.idle_add(self._startup_error, ErrorBuffer())
            else:
                gobject.idle_add(self._startup_memory_error)
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
        self._wind.connect_vnc()
        if self._have_memory:
            self._load_window.destroy()
            self._load_monitor.close()

    def _startup_memory_error(self):
        # Runs in UI thread
        if self._startup_cancelled:
            self._startup_error()
        else:
            self._wind.add_warning('dialog-warning',
                    'The memory image could not be loaded.')
            self._start_vm(cold=True)

    def _startup_error(self, error=None):
        # Runs in UI thread
        if self._have_memory:
            self._load_window.destroy()
            self._load_monitor.close()
        if error is not None:
            ew = ErrorWindow(self._wind, error)
            ew.run()
            ew.destroy()
        self._shutdown()

    def _user_restart(self, _obj):
        # Just terminate the VM; the vnc-disconnect handler will restart it
        self._machine.stop_vm()

    def _restart(self, _obj):
        self._machine.stop_vm()
        self._start_vm(cold=True)

    def _shutdown(self, _obj=None):
        self._wind.show_activity(False)
        self._wind.hide()
        gobject.idle_add(gtk.main_quit)
