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

import errno
import gobject
import os
import sys
import traceback

class DetailException(Exception):
    def __init__(self, msg, detail=None):
        Exception.__init__(self, msg)
        if detail:
            self.detail = detail


class ErrorBuffer(gobject.GObject):
    def __init__(self):
        gobject.GObject.__init__(self)
        exception = sys.exc_info()[1]
        detail = getattr(exception, 'detail', None)
        tb = traceback.format_exc()
        self.exception = str(exception)
        if detail:
            self.detail = detail + '\n\n' + tb
        else:
            self.detail = tb
gobject.type_register(ErrorBuffer)


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


def get_cache_dir():
    base = os.environ.get('XDG_CACHE_HOME')
    if not base:
        base = os.path.join(os.environ['HOME'], '.cache')
    path = os.path.join(base, 'vmnetx')
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def get_temp_dir():
    path = os.environ.get('XDG_RUNTIME_DIR')
    if path:
        return path
    else:
        return '/tmp'


def ensure_dir(path):
    try:
        os.makedirs(path)
    except OSError, e:
        if e.errno == errno.EEXIST and os.path.isdir(path):
            return
        raise
