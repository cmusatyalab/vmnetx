#
# Copyright (C) 2007-2013 Carnegie Mellon University
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

AC_PREREQ(2.63)
AC_INIT([vmnetx], [0.4.3])
AC_CONFIG_AUX_DIR([build-aux])
AC_CONFIG_MACRO_DIR([m4])
AC_COPYRIGHT([Copyright (C) 2007-2013 Carnegie Mellon University])
AC_CONFIG_SRCDIR([vmnetfs/vmnetfs.c])
AC_CONFIG_HEADERS([config.h])
AC_DISABLE_STATIC
AC_CANONICAL_HOST

AM_INIT_AUTOMAKE([foreign 1.11 dist-xz subdir-objects])
AM_SILENT_RULES([yes])

# Enable local VM execution?
AC_ARG_ENABLE([local-execution],
        [AS_HELP_STRING([--enable-local-execution],
        [allow VMs to be executed locally @<:@default=check@:>@])])
AC_MSG_CHECKING([whether to support local execution])
AS_CASE(["$enable_local_execution"], [yes|no], [], [
    AS_CASE([$host_os], [linux-gnu], [
        enable_local_execution=yes
    ], [
        enable_local_execution=no
    ])
])
AC_MSG_RESULT([$enable_local_execution])
AM_CONDITIONAL([ENABLE_LOCAL_EXECUTION], [test $enable_local_execution = yes])

# Enable update checking?
AC_ARG_ENABLE([update-checking],
        [AS_HELP_STRING([--enable-update-checking=URL],
        [periodically check for new versions @<:@default=yes@:>@])], [],
        [enable_update_checking="https://olivearchive.org/vmnetx/source/latest.json"])
AC_MSG_CHECKING([URL for update checking])
AS_CASE(["$enable_update_checking"], [no], [
    UPDATE_CHECK_URL=""
    AC_MSG_RESULT([disabled])
], [
    UPDATE_CHECK_URL="$enable_update_checking"
    AC_MSG_RESULT([$enable_update_checking])
])
AC_SUBST([UPDATE_CHECK_URL])

# Checks for programs.
AC_PROG_CC
AC_PROG_LIBTOOL
AM_PATH_PYTHON([2.6])
PKG_PROG_PKG_CONFIG()

AC_PATH_PROG([PYLINT], [pylint], [:])
AC_ARG_VAR([PYLINT], [path to pylint])

# Checks for libraries.
AS_IF([test $enable_local_execution = yes], [
    PKG_CHECK_MODULES([libcurl], [libcurl >= 7.19.1])
    PKG_CHECK_MODULES([fuse], [fuse >= 2.7])
    PKG_CHECK_MODULES([glib], [glib-2.0 >= 2.22])
    PKG_CHECK_MODULES([gthread], [gthread-2.0])
    PKG_CHECK_MODULES([libxml2], [libxml-2.0])
    # glib doesn't have special handling for API changes back to 2.22, so
    # set the threshold to 2.26
    AC_SUBST([GLIB_VER_DEFINES], ['-DGLIB_VERSION_MIN_REQUIRED=GLIB_VERSION_2_26 -DGLIB_VERSION_MAX_ALLOWED=GLIB_VERSION_MIN_REQUIRED'])
])

AC_CONFIG_FILES([Makefile])
AC_OUTPUT
