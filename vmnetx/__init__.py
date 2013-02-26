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

import libvirt

from vmnetx.system import __version__

assert(libvirt.getVersion() >= 9008) # 0.9.8

# Squash redundant reporting of libvirt errors to stderr.  This modifies
# global state, since the Python bindings don't provide a way to do this
# per-connection.
libvirt.registerErrorHandler(lambda _ctx, _error: None, None)
