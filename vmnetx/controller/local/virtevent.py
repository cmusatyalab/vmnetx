#
# vmnetx.controller.local.virtevent - libvirt glib event loop bindings
#
# Copyright (C) 2008 Daniel P. Berrange
# Copyright (C) 2010-2011 Red Hat, Inc.
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

#
# Based on libvirt-glib 0.1.6, which is released under the LGPLv2.1.
#

import glib
import libvirt
from threading import Lock

# We use short argument names
# pylint: disable=invalid-name

class _EventHandle(object):
    def __init__(self, id, fd, cb, data, free_func):
        self._id = id
        self._fd = fd
        self._cb = cb
        self._data = data
        self._free_func = free_func
        self._source = None
        self._events = 0

    def set_events(self, events):
        if events == self._events:
            return
        if self._events:
            glib.source_remove(self._source)
            self._source = None
        if events:
            cond = 0
            if events & libvirt.VIR_EVENT_HANDLE_READABLE:
                cond |= glib.IO_IN
            if events & libvirt.VIR_EVENT_HANDLE_WRITABLE:
                cond |= glib.IO_OUT
            self._source = glib.io_add_watch(self._fd, cond,
                    self._event_callback)
        self._events = events

    def _event_callback(self, _source, cond):
        events = 0
        if cond & glib.IO_IN:
            events |= libvirt.VIR_EVENT_HANDLE_READABLE
        if cond & glib.IO_OUT:
            events |= libvirt.VIR_EVENT_HANDLE_WRITABLE
        if cond & glib.IO_HUP:
            events |= libvirt.VIR_EVENT_HANDLE_HANGUP
        if cond & glib.IO_ERR:
            events |= libvirt.VIR_EVENT_HANDLE_ERROR
        self._cb(self._id, self._fd, events, self._data)
        return True

    def close(self):
        self.set_events(0)
        if self._free_func is not None:
            glib.idle_add(self._destroy)

    def _destroy(self):
        self._free_func(self._data)
        return False


class _TimeoutHandle(object):
    def __init__(self, id, cb, data, free_func):
        self._id = id
        self._cb = cb
        self._data = data
        self._free_func = free_func
        self._source = None
        self._interval = -1

    def set_interval(self, interval):
        if interval == self._interval:
            return
        if self._interval >= 0:
            glib.source_remove(self._source)
            self._source = None
        if interval >= 0:
            self._source = glib.timeout_add(interval, self._timer_callback)
        self._interval = interval

    def _timer_callback(self):
        self._cb(self._id, self._data)
        return True

    def close(self):
        self.set_interval(-1)
        if self._free_func is not None:
            glib.idle_add(self._destroy)

    def _destroy(self):
        self._free_func(self._data)
        return False


class LibvirtEventImpl(object):
    def __init__(self):
        self._lock = Lock()
        self._next_id = 1
        self._io_handles = {}
        self._timeout_handles = {}

    def register(self):
        libvirt.virEventRegisterImpl(self._add_handle, self._update_handle,
                self._remove_handle, self._add_timeout, self._update_timeout,
                self._remove_timeout)

    def _add_handle(self, fd, events, cb, data, free_func=None):
        with self._lock:
            id = self._next_id
            self._next_id += 1
            self._io_handles[id] = _EventHandle(id, fd, cb, data, free_func)
            self._io_handles[id].set_events(events)
        return id

    def _update_handle(self, id, events):
        with self._lock:
            handle = self._io_handles.get(id)
            if handle is not None:
                handle.set_events(events)

    def _remove_handle(self, id):
        with self._lock:
            handle = self._io_handles.pop(id, None)
            if handle is not None:
                handle.close()

    def _add_timeout(self, interval, cb, data, free_func=None):
        with self._lock:
            id = self._next_id
            self._next_id += 1
            self._timeout_handles[id] = _TimeoutHandle(id, cb, data, free_func)
            self._timeout_handles[id].set_interval(interval)
        return id

    def _update_timeout(self, id, interval):
        with self._lock:
            handle = self._timeout_handles.get(id)
            if handle is not None:
                handle.set_interval(interval)

    def _remove_timeout(self, id):
        with self._lock:
            handle = self._timeout_handles.pop(id, None)
            if handle is not None:
                handle.close()
