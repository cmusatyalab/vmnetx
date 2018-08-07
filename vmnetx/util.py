#
# vmnetx.util - Utilities
#
# Copyright (C) 2013 Carnegie Mellon University
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

import os
import socket
import subprocess
import sys
import traceback
import webbrowser

import gi
gi.require_version('GObject', '2.0')
from gi.repository import GObject

from .system import __version__

# Compatibility wrappers
# We don't use the imports ourselves and we don't control their names
# pylint: disable=unused-import,invalid-name
if sys.platform == 'win32':
    from .win32 import dup, socketpair, get_local_appdata_dir
else:
    dup = os.dup
    socketpair = socket.socketpair
# pylint: enable=unused-import,invalid-name


class DetailException(Exception):
    def __init__(self, msg, detail=None):
        Exception.__init__(self, msg)
        if detail:
            self.detail = detail


class NeedAuthentication(Exception):
    def __init__(self, host, realm, scheme):
        Exception.__init__(self, 'Authentication required')
        self.host = host
        self.realm = realm
        self.scheme = scheme


class ErrorBuffer(GObject.GObject):
    def __init__(self, message=None):
        GObject.GObject.__init__(self)
        exception = sys.exc_info()[1]
        if exception is not None:
            self.exception = str(exception)
            tb = traceback.format_exc()
            detail = getattr(exception, 'detail', None)
            if detail:
                self.detail = detail + '\n\n' + tb
            else:
                self.detail = tb
        else:
            self.exception = message
            self.detail = ''
GObject.type_register(ErrorBuffer)


class RangeConsolidator(object):
    def __init__(self, callback):
        self._callback = callback
        self._first = None
        self._last = None

    def __enter__(self):
        return self

    def emit(self, value):
        if self._last == value - 1:
            self._last = value
        else:
            if self._first is not None:
                self._callback(self._first, self._last)
            self._first = self._last = value

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        if self._first is not None:
            self._callback(self._first, self._last)
        return False


class BackoffTimer(GObject.GObject):
    __gsignals__ = {
        'attempt': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, schedule=(1000, 2000, 5000, 10000)):
        # schedule is in ms
        GObject.GObject.__init__(self)
        self._schedule = schedule
        self._schedule_index = None
        self._timer = None

    def attempt(self):
        '''Trigger a connection attempt, either immediately or after a
        delay.'''
        if self._timer is not None:
            return
        if self._schedule_index is None:
            self._schedule_index = 0
            self._timer = GObject.idle_add(self._attempt)
        else:
            timeout = self._schedule[self._schedule_index]
            self._schedule_index = min(self._schedule_index + 1,
                    len(self._schedule) - 1)
            self._timer = GObject.timeout_add(timeout, self._attempt)

    def _attempt(self):
        self._timer = None
        self.emit('attempt')
        return False

    def reset(self):
        '''Reset timer, because a connection succeeded or because we have
        an explicit connection request.'''
        self._schedule_index = None
        if self._timer is not None:
            GObject.source_remove(self._timer)
            self._timer = None
GObject.type_register(BackoffTimer)


def get_cache_dir():
    if sys.platform == 'win32':
        path = os.path.join(get_local_appdata_dir(), 'VMNetX', 'Cache')
    else:
        base = os.environ.get('XDG_CACHE_HOME')
        if not base:
            base = os.path.join(os.environ['HOME'], '.cache')
        path = os.path.join(base, 'vmnetx')

    if not os.path.exists(path):
        os.makedirs(path)
    return path


def ensure_dir(path):
    # Not atomic, but avoids hardcoding errno values for Windows
    if not os.path.isdir(path):
        os.makedirs(path)


# WindowsError isn't undefined on Windows
# pylint: disable=undefined-variable
def rename(old, new):
    if sys.platform == 'win32':
        # os.rename() cannot clobber existing file.  Not atomic!
        try:
            os.unlink(new)
        except WindowsError:
            pass
    os.rename(old, new)
# pylint: enable=undefined-variable


def open_browser(url):
    if sys.platform == 'win32':
        # ShellExecute() hangs the process.  Hacky workaround.
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        subprocess.call(['start', url], shell=True, startupinfo=si)
    else:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass


def setup_libvirt():
    import libvirt

    # Check libvirt version
    assert libvirt.getVersion() >= 9012  # 0.9.12

    # Squash redundant reporting of libvirt errors to stderr.  This modifies
    # global state, since the Python bindings don't provide a way to do this
    # per-connection.
    libvirt.registerErrorHandler(lambda _ctx, _error: None, None)


def get_requests_session():
    import requests
    session = requests.Session()
    if hasattr(requests.utils, 'default_user_agent'):
        session.headers['User-Agent'] = 'vmnetx/%s %s' % (
                __version__, requests.utils.default_user_agent())
    else:
        # requests < 0.13.3
        session.headers['User-Agent'] = 'vmnetx/%s python-requests/%s' % (
                __version__, requests.__version__)
    return session
