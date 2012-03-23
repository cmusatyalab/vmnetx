#
# vmnetx.vmnetfs - Wrapper for vmnetfs FUSE driver
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

import os
import subprocess

# system.py is built at install time, so pylint may fail to import it.
# Also avoid warning on variable name.
# pylint: disable=F0401,C0103
vmnetfs_path = ''
from .system import vmnetfs_path
# pylint: enable=F0401,C0103

class VMNetFSError(Exception):
    pass


class VMNetFS(object):
    def __init__(self, args):
        self._args = [vmnetfs_path] + args
        self._pipe = None
        self.mountpoint = None

    # pylint is confused by the values returned from Popen.communicate()
    # pylint: disable=E1103
    def start(self):
        read, write = os.pipe()
        try:
            proc = subprocess.Popen(self._args, stdin=read,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True)
            out, err = proc.communicate()
            if len(err) > 0:
                raise VMNetFSError(err.strip())
            elif proc.returncode > 0:
                raise VMNetFSError('vmnetfs returned status %d' %
                        proc.returncode)
            self.mountpoint = out.strip()
            self._pipe = os.fdopen(write, 'w')
        except:
            os.close(write)
            raise
        finally:
            os.close(read)
    # pylint: enable=E1103

    def terminate(self):
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None
