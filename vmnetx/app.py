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
import threading

from vmnetx.execute import Machine
from vmnetx.view import VMWindow, LoadProgressWindow, ErrorWindow, ErrorBuffer
from vmnetx.status.monitor import ImageMonitor, LoadProgressMonitor

class VMNetXApp(object):
    def __init__(self, manifest_file):
        gobject.threads_init()
        # Starts vmnetfs
        self._machine = Machine(manifest_file)
        self._wind = None
        self._load_monitor = None
        self._load_window = None
        self._startup_cancelled = False

    def run(self):
        try:
            # Create monitors
            self._load_monitor = LoadProgressMonitor(self._machine.memory_path)
            disk_monitor = ImageMonitor(self._machine.disk_path)

            # Show main window
            self._wind = VMWindow(self._machine.name,
                    self._machine.vnc_listen_address, disk_monitor)
            self._wind.connect('vnc-disconnect', gtk.main_quit)
            self._wind.connect('user-quit', gtk.main_quit)
            self._wind.show_all()

            # Show loading window
            self._load_window = LoadProgressWindow(self._load_monitor,
                    self._wind)
            self._load_window.connect('user-cancel', self._startup_cancel)
            self._load_window.show_all()

            # Load memory image in the background
            threading.Thread(name='vmnetx-startup',
                    target=self._startup).start()

            # Run main loop
            gtk.main()
        except Exception:
            # Show error window with exception
            ErrorWindow(self._wind).run()
        finally:
            # Shut down
            self._wind.destroy()
            disk_monitor.close()
            self._machine.stop_vm()
            self._machine.close()

    def _startup(self):
        # Thread function.  Load the memory image, then connect the VNC
        # viewer.
        try:
            self._machine.start_vm()
        except Exception, e:
            gobject.idle_add(self._startup_error, ErrorBuffer())
        else:
            gobject.idle_add(self._startup_done)

    def _startup_cancel(self, _obj):
        if not self._startup_cancelled:
            self._startup_cancelled = True
            threading.Thread(name='vmnetx-startup-cancel',
                    target=self._machine.stop_vm).start()
            self._wind.hide()

    def _startup_done(self):
        # Runs in UI thread
        self._wind.connect_vnc()
        self._load_window.destroy()
        self._load_monitor.close()

    def _startup_error(self, error):
        # Runs in UI thread
        self._load_window.destroy()
        self._load_monitor.close()
        if not self._startup_cancelled:
            ErrorWindow(self._wind, error).run()
        gtk.main_quit()
