#
# Copyright (C) 2007-2012 Carnegie Mellon University
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

## Process this file with autoconf to produce a configure script.

AC_PREREQ(2.68)
AC_INIT([vmnetx], [0.1])
AC_CONFIG_AUX_DIR([build-aux])
AC_CONFIG_MACRO_DIR([m4])
AC_COPYRIGHT([Copyright (C) 2007-2012 Carnegie Mellon University])
AC_CONFIG_SRCDIR([vmnetfs/vmnetfs.h])
AC_CONFIG_HEADERS([config.h])
AC_DISABLE_STATIC

AM_INIT_AUTOMAKE([foreign 1.11])
AM_SILENT_RULES([yes])

# Checks for programs.
AC_PROG_CC
AC_PROG_LIBTOOL
AM_PATH_PYTHON([2.6])
PKG_PROG_PKG_CONFIG()

# Checks for compiler characteristics.
gl_VISIBILITY

# Checks for libraries.
PKG_CHECK_MODULES([libcurl], [libcurl >= 7.17.0])
PKG_CHECK_MODULES([gtk], [gtk+-2.0 >= 2.8])
PKG_CHECK_MODULES([fuse], [fuse >= 2.7])
PKG_CHECK_MODULES([glib], [glib-2.0 >= 2.22])
PKG_CHECK_MODULES([gthread], [gthread-2.0])

AC_CONFIG_FILES([Makefile])
AC_OUTPUT
