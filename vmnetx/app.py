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
import glib
import gobject
import gtk
import json
import logging
import os
import signal
from tempfile import NamedTemporaryFile

from vmnetx.controller.local import LocalController
from vmnetx.package import NeedAuthentication
from vmnetx.system import __version__
from vmnetx.util import get_cache_dir
from vmnetx.view import (VMWindow, LoadProgressWindow, PasswordWindow,
        SaveMediaWindow, ErrorWindow, FatalErrorWindow, IgnorableErrorWindow,
        have_spice_viewer)

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
    RESUME_CHECK_DELAY = 1000  # ms

    def __init__(self, package_ref):
        gobject.threads_init()
        self._username_cache = _UsernameCache()
        self._controller = LocalController(package_ref, have_spice_viewer)
        self._package_ref = package_ref
        self._machine = None
        self._wind = None
        self._load_window = None
        self._io_failed = False

        self._controller.connect('startup-progress', self._startup_progress)
        self._controller.connect('startup-complete', self._startup_done)
        self._controller.connect('startup-cancelled', self._startup_cancelled)
        self._controller.connect('startup-rejected-memory',
                self._startup_rejected_memory)
        self._controller.connect('startup-failed', self._startup_error)

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def run(self):
        try:
            # Attempt to catch SIGTERM.  This is dubious, but not more so
            # than the default handling of SIGINT.
            signal.signal(signal.SIGTERM, self._signal)

            # Fetch and parse metadata
            pw_wind = None
            while True:
                try:
                    self._controller.initialize()
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
                    self._controller.scheme = e.scheme
                    self._controller.username = pw_wind.username
                    self._controller.password = pw_wind.password
                    self._username_cache.put(e.host, e.realm, username)
                else:
                    if pw_wind is not None:
                        pw_wind.destroy()
                    break

            # Show main window
            self._wind = VMWindow(self._controller.machine.name,
                    disk_stats=self._controller.disk_stats,
                    disk_chunks=self._controller.disk_chunks,
                    disk_chunk_size=self._controller.disk_chunk_size,
                    use_spice=self._controller.machine.using_spice,
                    max_mouse_rate=self._controller.metadata.domain_xml.max_mouse_rate)
            self._wind.connect('viewer-connect', self._connect)
            self._wind.connect('viewer-disconnect', self._restart)
            self._wind.connect('user-restart', self._user_restart)
            self._wind.connect('user-quit', self._shutdown)
            self._wind.connect('user-screenshot', self._screenshot)
            self._wind.show_all()
            self._controller.disk_stats['io_errors'].connect('stat-changed',
                    self._io_error)

            # Start logging
            logging.getLogger().setLevel(logging.INFO)
            _log.info('VMNetX %s starting at %s', __version__,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            _log.info('SPICE viewer %s, qemu support %s',
                    'available' if have_spice_viewer else 'unavailable',
                    'available' if self._controller.machine.have_spice else 'unavailable')

            # Load memory image
            self._controller.start_vm()

            # Run main loop
            gtk.main()
        except (KeyboardInterrupt, SystemExit):
            pass
        except:
            # Show error window with exception
            FatalErrorWindow(self._wind).run()
        finally:
            # Shut down
            if self._wind is not None:
                self._wind.destroy()
            self._controller.shutdown()
            logging.shutdown()
    # pylint: enable=W0702

    def _signal(self, _signum, _frame):
        raise KeyboardInterrupt

    def _startup_progress(self, _obj, count, total):
        if self._load_window is None:
            self._load_window = LoadProgressWindow(self._wind)
            self._load_window.connect('user-cancel', self._startup_cancel)
            self._load_window.show_all()
        self._load_window.progress(count, total)

    def _startup_cancel(self, _obj):
        self._controller.startup_cancel()
        self._wind.hide()

    def _destroy_load_window(self):
        if self._load_window is not None:
            self._load_window.destroy()
            self._load_window = None

    def _startup_done(self, _obj):
        self._wind.connect_viewer(self._controller.machine.viewer_listen_address,
                self._controller.machine.viewer_password)
        self._destroy_load_window()

    def _startup_cancelled(self, _obj):
        self._destroy_load_window()
        self._shutdown()

    def _startup_rejected_memory(self, _obj):
        self._destroy_load_window()
        self._warn_bad_memory()

    def _startup_error(self, _obj, error):
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
        if not self._controller.have_memory:
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
            self._controller.machine.stop_vm()
    # pylint: enable=W1401

    def _io_error(self, _monitor, _name, _value):
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

    def _user_restart(self, _obj):
        # Just terminate the VM; the viewer-disconnect handler will restart it
        self._controller.stop_vm()

    def _restart(self, _obj):
        self._controller.stop_vm()
        self._controller.start_vm()

    def _shutdown(self, _obj=None):
        self._wind.show_activity(False)
        self._wind.show_log(False)
        self._wind.hide()
        gobject.idle_add(gtk.main_quit)

    def _screenshot(self, _obj, pixbuf):
        sw = SaveMediaWindow(self._wind, 'Save Screenshot',
                self._controller.machine.name + '.png', pixbuf)
        if sw.run() == gtk.RESPONSE_OK:
            try:
                pixbuf.save(sw.get_filename(), 'png')
            except gobject.GError, e:
                ew = ErrorWindow(self._wind, str(e))
                ew.run()
                ew.destroy()
        sw.destroy()
