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

from ctypes import (windll, c_int, c_uint, c_uint8, c_uint32, c_uint64,
        c_ushort, c_ulong, c_ulonglong, c_wchar_p, Structure, POINTER, byref)
from ctypes.wintypes import (BYTE, DWORD, WORD, HRESULT, HANDLE, LPVOID,
        tagRECT)
import errno
import os
from select import select
import socket
import sys
# No comtypes on Linux
# pylint: disable=import-error
from comtypes import CoClass, COMMETHOD, GUID as COMGUID, IUnknown, wireHWND
from comtypes.client import CreateObject
# pylint: enable=import-error

# We use the Win32 naming scheme when wrapping its objects
# comtypes doesn't provide initializers
# pylint: disable=invalid-name,no-init

# Any DLL loaded here will be bundled by PyInstaller by default.  This is
# undesirable for system libraries.  To prevent it, modify the exclusion
# list in vmnetx-packaging/windows/build.sh.
_winsock = windll.ws2_32
_shell32 = windll.shell32
_ole32 = windll.ole32


if sys.maxsize > (1 << 31):
    SOCKET = c_uint64
    INVALID_SOCKET = (1 << 64) - 1
else:
    SOCKET = c_uint32
    INVALID_SOCKET = (1 << 32) - 1


WSA_FLAG_OVERLAPPED = 0x01
KF_FLAG_INIT = 0x800
KF_FLAG_CREATE = 0x8000
TBPF_NOPROGRESS = 0


EightByte = BYTE * 8
class GUID(Structure):
    _fields_ = [
        ('Data1', DWORD),
        ('Data2', WORD),
        ('Data3', WORD),
        ('Data4', EightByte),
    ]


FOLDERID_LocalAppData = GUID(0xf1b32785, 0x6fba, 0x4fcf,
        EightByte(0x9d, 0x55, 0x7b, 0x8e, 0x7f, 0x15, 0x70, 0x91))


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
WSASocket.argtypes = [c_int, c_int, c_int, POINTER(WSAPROTOCOL_INFO),
        c_uint, DWORD]
WSASocket.restype = SOCKET


WSAGetLastError = _winsock.WSAGetLastError
WSAGetLastError.argtypes = []
WSAGetLastError.restype = c_int


SHGetKnownFolderPath = _shell32.SHGetKnownFolderPath
SHGetKnownFolderPath.argtypes = [POINTER(GUID), DWORD, HANDLE,
        POINTER(c_wchar_p)]
SHGetKnownFolderPath.restype = HRESULT


SetCurrentProcessExplicitAppUserModelID = \
        _shell32.SetCurrentProcessExplicitAppUserModelID
SetCurrentProcessExplicitAppUserModelID.argtypes = [c_wchar_p]
SetCurrentProcessExplicitAppUserModelID.restype = HRESULT


CoTaskMemFree = _ole32.CoTaskMemFree
CoTaskMemFree.argtypes = [LPVOID]
CoTaskMemFree.restype = None


class tagTHUMBBUTTON(Structure):
    _fields_ = [
        ('dwMask', c_ulong),
        ('iId', c_uint),
        ('iBitmap', c_uint),
        ('hIcon', POINTER(IUnknown)),
        ('szTip', c_ushort * 260),
        ('dwFlags', c_ulong),
    ]


class ITaskbarList(IUnknown):
    _iid_ = COMGUID('{56FDF342-FD6D-11D0-958A-006097C9A090}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'HrInit'),
        COMMETHOD([], HRESULT, 'AddTab',
                  (['in'], c_int, 'hwnd')),
        COMMETHOD([], HRESULT, 'DeleteTab',
                  (['in'], c_int, 'hwnd')),
        COMMETHOD([], HRESULT, 'ActivateTab',
                  (['in'], c_int, 'hwnd')),
        COMMETHOD([], HRESULT, 'SetActivateAlt',
                  (['in'], c_int, 'hwnd')),
    ]


class ITaskbarList2(ITaskbarList):
    _iid_ = COMGUID('{602D4995-B13A-429B-A66E-1935E44F4317}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'MarkFullscreenWindow',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_int, 'fFullscreen')),
    ]


class ITaskbarList3(ITaskbarList2):
    _iid_ = COMGUID('{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'SetProgressValue',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_ulonglong, 'ullCompleted'),
                  (['in'], c_ulonglong, 'ullTotal')),
        COMMETHOD([], HRESULT, 'SetProgressState',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_int, 'tbpFlags')),
        COMMETHOD([], HRESULT, 'RegisterTab',
                  (['in'], c_int, 'hwndTab'),
                  (['in'], wireHWND, 'hwndMDI')),
        COMMETHOD([], HRESULT, 'UnregisterTab',
                  (['in'], c_int, 'hwndTab')),
        COMMETHOD([], HRESULT, 'SetTabOrder',
                  (['in'], c_int, 'hwndTab'),
                  (['in'], c_int, 'hwndInsertBefore')),
        COMMETHOD([], HRESULT, 'SetTabActive',
                  (['in'], c_int, 'hwndTab'),
                  (['in'], c_int, 'hwndMDI'),
                  (['in'], c_int, 'tbatFlags')),
        COMMETHOD([], HRESULT, 'ThumbBarAddButtons',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_uint, 'cButtons'),
                  (['in'], POINTER(tagTHUMBBUTTON), 'pButton')),
        COMMETHOD([], HRESULT, 'ThumbBarUpdateButtons',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_uint, 'cButtons'),
                  (['in'], POINTER(tagTHUMBBUTTON), 'pButton')),
        COMMETHOD([], HRESULT, 'ThumbBarSetImageList',
                  (['in'], c_int, 'hwnd'),
                  (['in'], POINTER(IUnknown), 'himl')),
        COMMETHOD([], HRESULT, 'SetOverlayIcon',
                  (['in'], c_int, 'hwnd'),
                  (['in'], POINTER(IUnknown), 'hIcon'),
                  (['in'], c_wchar_p, 'pszDescription')),
        COMMETHOD([], HRESULT, 'SetThumbnailTooltip',
                  (['in'], c_int, 'hwnd'),
                  (['in'], c_wchar_p, 'pszTip')),
        COMMETHOD([], HRESULT, 'SetThumbnailClip',
                  (['in'], c_int, 'hwnd'),
                  (['in'], POINTER(tagRECT), 'prcClip')),
    ]


class TaskbarList(CoClass):
    _com_interfaces_ = [ITaskbarList3]
    _reg_clsid_ = COMGUID('{56FDF344-FD6D-11D0-958A-006097C9A090}')


_taskbar = CreateObject(TaskbarList)
_taskbar.HrInit()


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


def socketpair():
    '''Create a pair of connected TCP sockets.'''
    for _ in range(100):
        listener = socket.socket()
        try:
            # Listen
            listener.bind(('localhost', 0))
            listener.listen(0)

            # Connect
            a = socket.socket()
            a.setblocking(0)
            try:
                a.connect(listener.getsockname())
            except socket.error, e:
                if e.errno != errno.EWOULDBLOCK:
                    raise

            # Accept
            b, peer = listener.accept()

            # Confirm that the connection we accepted is the one we made
            if peer != a.getsockname():
                raise IOError('Someone else connected to us')

            # Confirm connection completion
            _, w, _ = select([], [a], [a], 0.5)
            if a not in w:
                raise IOError('Connect failed')

            # Set blocking and return
            a.setblocking(1)
            return a, b
        except (socket.error, IOError):
            pass
        finally:
            listener.close()

    raise IOError("Couldn't create socket pair")


def get_local_appdata_dir():
    outptr = c_wchar_p()
    # Raises WindowsError on failure
    SHGetKnownFolderPath(FOLDERID_LocalAppData,
            KF_FLAG_CREATE | KF_FLAG_INIT, None, byref(outptr))
    ret = outptr.value
    CoTaskMemFree(outptr)
    return ret


def set_window_progress(window, progress):
    hwnd = window.window.handle
    if progress is None:
        _taskbar.SetProgressState(hwnd, TBPF_NOPROGRESS)
    else:
        _taskbar.SetProgressValue(hwnd, int(progress * 1000), 1000)


def windows_vmnetx_init():
    SetCurrentProcessExplicitAppUserModelID('Olive.VMNetX')

# pylint: enable=invalid-name,no-init
