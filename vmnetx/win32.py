#
# vmnetx.win32 - Win32 compatibility wrappers
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

from ctypes import *
from ctypes.wintypes import *
import os
import socket
import sys

_winsock = windll.ws2_32


if sys.maxsize > (1 << 31):
    SOCKET = c_uint64
    INVALID_SOCKET = (1 << 64) - 1
else:
    SOCKET = c_uint32
    INVALID_SOCKET = (1 << 32) - 1


WSA_FLAG_OVERLAPPED = 0x01


class GUID(Structure):
    _fields_ = [
        ('Data1', DWORD),
        ('Data2', WORD),
        ('Data3', WORD),
        ('Data4', BYTE * 8),
    ]


class WSAPROTOCOLCHAIN(Structure):
    _fields_ = [
        ('ChainLen', c_int),
        ('ChainEntries', DWORD * 7),
    ]


class WSAPROTOCOL_INFO(Structure):
    _fields_ = [
        ('dwServiceFlags1', DWORD),
        ('dwServiceFlags2', DWORD),
        ('dwServiceFlags3', DWORD),
        ('dwServiceFlags4', DWORD),
        ('dwProviderFlags', DWORD),
        ('ProviderId', GUID),
        ('dwCatalogEntryId', DWORD),
        ('ProtocolChain', WSAPROTOCOLCHAIN),
        ('iVersion', c_int),
        ('iAddressFamily', c_int),
        ('iMaxSockAddr', c_int),
        ('iMinSockAddr', c_int),
        ('iSocketType', c_int),
        ('iProtocol', c_int),
        ('iProtocolMaxOffset', c_int),
        ('iNetworkByteOrder', c_int),
        ('iSecurityScheme', c_int),
        ('dwMessageSize', DWORD),
        ('dwProviderReserved', DWORD),
        ('szProtocol', c_uint8 * 256),
    ]


WSADuplicateSocket = _winsock.WSADuplicateSocketA
WSADuplicateSocket.argtypes = [SOCKET, DWORD, POINTER(WSAPROTOCOL_INFO)]
WSADuplicateSocket.restype = c_int


WSASocket = _winsock.WSASocketA
WSASocket.argtypes = [c_int, c_int, c_int, POINTER(WSAPROTOCOL_INFO), c_uint, DWORD]
WSASocket.restype = SOCKET


WSAGetLastError = _winsock.WSAGetLastError
WSAGetLastError.argtypes = []
WSAGetLastError.restype = c_int


def _get_wsa_error():
    err = WSAGetLastError()
    try:
        return socket.errorTab[err]
    except KeyError:
        return os.strerror(err)


def dup(s):
    '''Duplicate a SOCKET.'''
    info = WSAPROTOCOL_INFO()
    if WSADuplicateSocket(s, os.getpid(), byref(info)):
        raise IOError('Cannot serialize socket: %s' % _get_wsa_error())
    s2 = WSASocket(info.iAddressFamily, info.iSocketType, info.iProtocol,
            byref(info), 0, WSA_FLAG_OVERLAPPED)
    if s2 == INVALID_SOCKET:
        raise IOError('Cannot unserialize socket: %s' % _get_wsa_error())
    return s2
