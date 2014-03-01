/*
 * vmnetfs - virtual machine network execution virtual filesystem
 *
 * Copyright (C) 2006-2012 Carnegie Mellon University
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of version 2 of the GNU General Public License as published
 * by the Free Software Foundation.  A copy of the GNU General Public License
 * should have been distributed along with this program in the file
 * COPYING.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
 * or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
 * for more details.
 */

#include <string.h>
#include <unistd.h>
#include <errno.h>
#include "vmnetfs-private.h"

GQuark _vmnetfs_config_error_quark(void)
{
    return g_quark_from_static_string("vmnetfs-config-error-quark");
}

GQuark _vmnetfs_fuse_error_quark(void)
{
    return g_quark_from_static_string("vmnetfs-fuse-error-quark");
}

GQuark _vmnetfs_io_error_quark(void)
{
    return g_quark_from_static_string("vmnetfs-io-error-quark");
}

GQuark _vmnetfs_stream_error_quark(void)
{
    return g_quark_from_static_string("vmnetfs-stream-error-quark");
}

GQuark _vmnetfs_transport_error_quark(void)
{
    return g_quark_from_static_string("vmnetfs-transport-error-quark");
}

bool _vmnetfs_safe_pread(const char *file, int fd, void *buf, uint64_t count,
        uint64_t offset, GError **err)
{
    uint64_t cur;

    while (count > 0 && (cur = pread(fd, buf, count, offset)) > 0) {
        buf += cur;
        offset += cur;
        count -= cur;
    }
    if (count == 0) {
        return true;
    } else if (cur == 0) {
        g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_PREMATURE_EOF,
                "Couldn't read %s: Premature end of file", file);
        return false;
    } else {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't read %s: %s", file, strerror(errno));
        return false;
    }
}

bool _vmnetfs_safe_pwrite(const char *file, int fd, const void *buf,
        uint64_t count, uint64_t offset, GError **err)
{
    int64_t cur;

    while (count > 0 && (cur = pwrite(fd, buf, count, offset)) >= 0) {
        buf += cur;
        offset += cur;
        count -= cur;
    }
    if (count > 0) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't write %s: %s", file, strerror(errno));
        return false;
    }
    return true;
}

/* The cursor is assumed to be allocated on the stack; this just fills
   it in. */
void _vmnetfs_cursor_start(struct vmnetfs_image *img,
        struct vmnetfs_cursor *cur, uint64_t start, uint64_t count)
{
    memset(cur, 0, sizeof(*cur));
    cur->img = img;
    cur->start = start;
    cur->count = count;
}

/* Populate the public fields of the cursor with information on the next
   chunk in the I/O, starting from the first, given that the last I/O
   completed @count bytes.  Returns true if we produced a valid chunk,
   false if done with this I/O.  Assumes an infinite-size image. */
bool _vmnetfs_cursor_chunk(struct vmnetfs_cursor *cur, uint64_t count)
{
    uint64_t position;

    cur->io_offset += count;
    position = cur->start + cur->io_offset;
    cur->chunk = position / cur->img->chunk_size;
    cur->offset = position - cur->chunk * cur->img->chunk_size;
    cur->length = MIN(cur->img->chunk_size - cur->offset,
            cur->count - cur->io_offset);
    return cur->io_offset < cur->count;
}
