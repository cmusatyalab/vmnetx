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
