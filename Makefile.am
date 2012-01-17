ACLOCAL_AMFLAGS = -I m4

AM_CPPFLAGS = -D_GNU_SOURCE -D_FILE_OFFSET_BITS=64
AM_CFLAGS = -std=gnu99 -W -Wall -Wstrict-prototypes -pthread \
	$(CFLAG_VISIBILITY) $(libcurl_CFLAGS) $(glib_CFLAGS) \
	$(gthread_CFLAGS) $(fuse_CFLAGS)
AM_LDFLAGS = -avoid-version -pthread $(libcurl_LIBS) $(glib_LIBS) \
	$(gthread_LIBS) $(fuse_LIBS)

pkglib_LTLIBRARIES = libvmnetfs.la
libvmnetfs_la_SOURCES  = vmnetfs/bitmap.c vmnetfs/cond.c vmnetfs/fuse.c \
	vmnetfs/fuse-image.c vmnetfs/fuse-stats.c vmnetfs/fuse-stream.c \
	vmnetfs/io.c vmnetfs/ll-modified.c vmnetfs/ll-pristine.c \
	vmnetfs/stats.c vmnetfs/stream.c vmnetfs/transport.c vmnetfs/util.c \
	vmnetfs/vmnetfs.c vmnetfs/vmnetfs.h vmnetfs/vmnetfs-private.h

nobase_python_PYTHON = vmnetx/__init__.py vmnetx/vmnetfs.py
nobase_nodist_python_PYTHON = vmnetx/system.py
CLEANFILES = vmnetx/system.py
EXTRA_DIST = vmnetx/system.py.in pylintrc

vmnetx/system.py: vmnetx/system.py.in Makefile
	$(AM_V_GEN) sed -e "s:@pkglibdir@:$(pkglibdir):g" $< > $@

check-local:
	@# Fail on serious pylint errors
	PYTHONPATH=$(srcdir) \
		$(PYLINT) --rcfile=$(srcdir)/pylintrc vmnetx || \
		[ $$(($$? & 3)) -eq 0 ]
