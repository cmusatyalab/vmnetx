ACLOCAL_AMFLAGS = -I m4

AM_CPPFLAGS = -D_GNU_SOURCE -D_FILE_OFFSET_BITS=64
AM_CFLAGS = -std=gnu99 -W -Wall -Wstrict-prototypes -pthread \
	$(libcurl_CFLAGS) $(glib_CFLAGS) $(gthread_CFLAGS) $(fuse_CFLAGS)
AM_LDFLAGS = -pthread $(libcurl_LIBS) $(glib_LIBS) $(gthread_LIBS) \
	$(fuse_LIBS)

dist_bin_SCRIPTS = tools/vmnetx \
	tools/vmnetx-generate

pkglibexec_PROGRAMS = vmnetfs/vmnetfs
vmnetfs_vmnetfs_SOURCES  = vmnetfs/bitmap.c vmnetfs/cond.c vmnetfs/fuse.c \
	vmnetfs/fuse-image.c vmnetfs/fuse-stats.c vmnetfs/fuse-stream.c \
	vmnetfs/io.c vmnetfs/ll-modified.c vmnetfs/ll-pristine.c \
	vmnetfs/pollable.c vmnetfs/stats.c vmnetfs/stream.c \
	vmnetfs/transport.c vmnetfs/util.c vmnetfs/vmnetfs.c \
	vmnetfs/vmnetfs-private.h

nobase_python_PYTHON = vmnetx/__init__.py \
	vmnetx/app.py \
	vmnetx/domain.py \
	vmnetx/execute.py \
	vmnetx/generate.py \
	vmnetx/manifest.py \
	vmnetx/view.py \
	vmnetx/vmnetfs.py \
	vmnetx/status/__init__.py \
	vmnetx/status/monitor.py
nobase_nodist_python_PYTHON = vmnetx/system.py
CLEANFILES = vmnetx/system.py
EXTRA_DIST = vmnetx/system.py.in pylintrc

dist_pkgpython_DATA = manifest.xsd

vmnetx/system.py: vmnetx/system.py.in Makefile
	$(MKDIR_P) vmnetx
	$(AM_V_GEN) sed -e "s:@pkglibexecdir@:$(pkglibexecdir):g" \
		-e "s:@version@:$(VERSION):g" $< > $@

check-local:
	@# Fail on serious pylint errors
	PYTHONPATH=$(srcdir) \
		$(PYLINT) --rcfile=$(srcdir)/pylintrc vmnetx || \
		[ $$(($$? & 3)) -eq 0 ]
