#
# vmnetx.vmnetfs - Bindings to vmnetfs C API
#
# Copyright (C) 2011-2012 Carnegie Mellon University
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

from ctypes import cdll, c_void_p, c_char_p, c_uint64, c_uint32, c_bool

# system.py is built at install time, so pylint may fail to import it.
# Also avoid warning on variable name.
# pylint: disable=F0401,C0103
vmnetfs_path = ''
from .system import vmnetfs_path
# pylint: enable=F0401,C0103

class VMNetFSError(Exception):
    pass


class VMNetFS(object):
    def __init__(self, mountpoint, disk_url, disk_cache, disk_size,
            disk_segment_size, disk_chunk_size, memory_url, memory_cache,
            memory_size, memory_segment_size, memory_chunk_size):
        self._hdl = _new(mountpoint, disk_url, disk_cache, disk_size,
            disk_segment_size, disk_chunk_size, memory_url, memory_cache,
            memory_size, memory_segment_size, memory_chunk_size)

    def run(self):
        _run(self._hdl)

    def terminate(self):
        _terminate(self._hdl)


class _VMNetFS(object):
    """Wrapper class for a VMNetFS handle."""

    def __init__(self, ptr):
        self._as_parameter_ = ptr
        # Retain a reference to _free() to avoid GC problems during
        # interpreter shutdown
        self._free = _free

    def __del__(self):
        self._free(self)

    @classmethod
    def from_param(cls, obj):
        if obj.__class__ != cls:
            raise ValueError("Not a VMNetFS reference")
        return obj


# resolve and return a library function with the specified properties
def _import(name, restype, argtypes, errcheck=None):
    func = getattr(_lib, name)
    func.argtypes = argtypes
    func.restype = restype
    if errcheck is not None:
        func.errcheck = errcheck
    return func


# check if the handle is in error state
def _check_handle(hdl):
    err = _get_error(hdl)
    if err is not None:
        raise VMNetFSError(err)


# wrap the handle and check for errors
def _check_new(result, _func, _args):
    hdl = _VMNetFS(c_void_p(result))
    _check_handle(hdl)
    return hdl


# check if the library got into an error state after a library call
def _check_error(result, _func, args):
    _check_handle(args[0])
    return result


_lib = cdll.LoadLibrary(vmnetfs_path)

_new = _import('vmnetfs_new', c_void_p, [c_char_p,
    c_char_p, c_char_p, c_uint64, c_uint64, c_uint32,
    c_char_p, c_char_p, c_uint64, c_uint64, c_uint32], _check_new)

_run = _import('vmnetfs_run', None, [_VMNetFS], _check_error)

_terminate = _import('vmnetfs_terminate', None, [_VMNetFS], _check_error)

_get_error = _import('vmnetfs_get_error', c_char_p, [_VMNetFS])

_free = _import('vmnetfs_free', None, [_VMNetFS])


# Initialize library
if not _import('vmnetfs_init', c_bool, [])():
    raise VMNetFSError("Couldn't initialize VMNetFS")
