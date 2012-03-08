#
# vmnetx - Virtual machine network execution
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
import libvirt
import threading

from vmnetx.execute import Machine as _Machine
from vmnetx.view import (VMWindow as _VMWindow,
        LoadProgressWindow as _LoadProgressWindow)

# For importers
from vmnetx.system import __version__

class VMNetXApp(object):
    def __init__(self, manifest_file):
        gobject.threads_init()
        self._machine = _Machine(manifest_file)
        self._wind = _VMWindow(self._machine.name,
                self._machine.vnc_listen_address)
        self._loading = None

    def run(self):
        self._wind.show_all()

        # Load memory image in the background
        threading.Thread(name='vmnetx-startup', target=self._startup).start()

        # Now it's safe to access vmnetfs stats
        self._loading = _LoadProgressWindow(self._machine.memory_path,
                self._wind)
        self._loading.show_all()

        gtk.main()

        # Shut down
        self._wind.destroy()
        self._machine.stop()

    def _startup(self):
        # Thread function.  Load the memory image, then connect the VNC
        # viewer.
        try:
            self._machine.start()
        finally:
            gobject.idle_add(self._startup_done)

    def _startup_done(self):
        # Runs in UI thread
        self._wind.connect_vnc()
        self._loading.destroy()


assert(libvirt.getVersion() >= 9004) # 0.9.4
