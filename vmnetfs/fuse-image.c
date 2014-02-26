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
#include <inttypes.h>
#include <errno.h>
#include "vmnetfs-private.h"

static int image_getattr(void *dentry_ctx, struct stat *st)
{
    struct vmnetfs_image *img = dentry_ctx;

    st->st_mode = S_IFREG | 0600;
    st->st_size = _vmnetfs_io_get_image_size(img, NULL);
    return 0;
}

static int image_truncate(void *dentry_ctx, uint64_t size)
{
    struct vmnetfs_image *img = dentry_ctx;
    GError *err = NULL;

    if (!_vmnetfs_io_set_image_size(img, size, &err)) {
        if (g_error_matches(err, VMNETFS_IO_ERROR,
                VMNETFS_IO_ERROR_INTERRUPTED)) {
            g_clear_error(&err);
            return -EINTR;
        } else {
            g_warning("%s", err->message);
            g_clear_error(&err);
            _vmnetfs_u64_stat_increment(img->io_errors, 1);
            return -EIO;
        }
    }
    return 0;
}

static int image_open(void *dentry_ctx, struct vmnetfs_fuse_fh *fh)
{
    struct vmnetfs_image *img = dentry_ctx;

    fh->data = img;
    return 0;
}

static int image_read(struct vmnetfs_fuse_fh *fh, void *buf, uint64_t start,
        uint64_t count)
{
    struct vmnetfs_image *img = fh->data;
    struct vmnetfs_cursor cur;
    GError *err = NULL;
    uint64_t read = 0;

    _vmnetfs_stream_group_write(img->io_stream, "read %"PRIu64"+%"PRIu64"\n",
            start, count);
    for (_vmnetfs_cursor_start(img, &cur, start, count);
            _vmnetfs_cursor_chunk(&cur, read); ) {
        read = _vmnetfs_io_read_chunk(img, buf + cur.io_offset, cur.chunk,
                cur.offset, cur.length, &err);
        if (err) {
            if (g_error_matches(err, VMNETFS_IO_ERROR,
                    VMNETFS_IO_ERROR_INTERRUPTED)) {
                g_clear_error(&err);
                return (int) (cur.io_offset + read) ?: -EINTR;
            } else if (g_error_matches(err, VMNETFS_IO_ERROR,
                    VMNETFS_IO_ERROR_EOF)) {
                g_clear_error(&err);
                return cur.io_offset + read;
            } else {
                g_warning("%s", err->message);
                g_clear_error(&err);
                _vmnetfs_u64_stat_increment(img->io_errors, 1);
                return (int) (cur.io_offset + read) ?: -EIO;
            }
        }
        _vmnetfs_u64_stat_increment(img->bytes_read, cur.length);
    }
    return cur.io_offset;
}

static int image_write(struct vmnetfs_fuse_fh *fh, const void *buf,
        uint64_t start, uint64_t count)
{
    struct vmnetfs_image *img = fh->data;
    struct vmnetfs_cursor cur;
    GError *err = NULL;
    uint64_t written = 0;

    _vmnetfs_stream_group_write(img->io_stream, "write %"PRIu64"+%"PRIu64"\n",
            start, count);
    for (_vmnetfs_cursor_start(img, &cur, start, count);
            _vmnetfs_cursor_chunk(&cur, written); ) {
        written = _vmnetfs_io_write_chunk(img, buf + cur.io_offset, cur.chunk,
                cur.offset, cur.length, &err);
        if (err) {
            if (g_error_matches(err, VMNETFS_IO_ERROR,
                    VMNETFS_IO_ERROR_INTERRUPTED)) {
                g_clear_error(&err);
                return (int) (cur.io_offset + written) ?: -EINTR;
            } else {
                g_warning("%s", err->message);
                g_clear_error(&err);
                _vmnetfs_u64_stat_increment(img->io_errors, 1);
                return (int) (cur.io_offset + written) ?: -EIO;
            }
        }
        _vmnetfs_u64_stat_increment(img->bytes_written, cur.length);
    }
    return cur.io_offset;
}

static const struct vmnetfs_fuse_ops image_ops = {
    .getattr = image_getattr,
    .truncate = image_truncate,
    .open = image_open,
    .read = image_read,
    .write = image_write,
};

void _vmnetfs_fuse_image_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img)
{
    _vmnetfs_fuse_add_file(dir, "image", &image_ops, img);
}
