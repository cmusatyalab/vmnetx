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

struct io_cursor {
    /* Public fields; do not modify */
    uint64_t chunk;
    uint64_t offset;
    uint64_t length;
    uint64_t buf_offset;
    bool eof;  /* Tried to do I/O past the end of the disk */

    /* Private fields */
    struct vmnetfs_image *img;
    uint64_t start;
    uint64_t count;
};

/* The cursor is assumed to be allocated on the stack; this just fills
   it in. */
static void io_start(struct vmnetfs_image *img, struct io_cursor *cur,
        uint64_t start, uint64_t count)
{
    memset(cur, 0, sizeof(*cur));
    cur->img = img;
    cur->start = start;
    cur->count = count;
}

/* Populate the public fields of the cursor with information on the next
   chunk in the I/O, starting from the first.  Returns true if we produced
   a valid chunk, false if done with this I/O. */
static bool io_chunk(struct io_cursor *cur)
{
    uint64_t position;
    uint64_t this_chunk_start;
    uint64_t this_chunk_size;

    cur->buf_offset += cur->length;
    if (cur->buf_offset >= cur->count) {
        /* Done */
        return false;
    }
    position = cur->start + cur->buf_offset;
    if (position >= cur->img->size) {
        /* End of image */
        cur->eof = true;
        return false;
    }
    cur->chunk = position / cur->img->chunk_size;
    this_chunk_start = cur->chunk * cur->img->chunk_size;
    this_chunk_size = MIN(cur->img->size - this_chunk_start,
            cur->img->chunk_size);
    cur->offset = position - this_chunk_start;
    cur->length = MIN(this_chunk_size - cur->offset,
            cur->count - cur->buf_offset);
    return true;
}

static int image_getattr(void *dentry_ctx, struct stat *st)
{
    struct vmnetfs_image *img = dentry_ctx;

    st->st_mode = S_IFREG | 0600;
    st->st_size = img->size;
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
    struct io_cursor cur;
    GError *err = NULL;

    g_debug("Read %"PRIu64" at %"PRIu64, count, start);
    for (io_start(img, &cur, start, count); io_chunk(&cur); ) {
        if (!_vmnetfs_io_read_chunk(img, buf + cur.buf_offset, cur.chunk,
                        cur.offset, cur.length, &err)) {
            g_warning("%s", err->message);
            g_clear_error(&err);
            return (int) cur.buf_offset ?: -EIO;
        }
        _vmnetfs_u64_stat_increment(img->bytes_read, cur.length);
    }
    return cur.buf_offset;
}

static int image_write(struct vmnetfs_fuse_fh *fh, const void *buf,
        uint64_t start, uint64_t count)
{
    struct vmnetfs_image *img = fh->data;
    struct io_cursor cur;
    GError *err = NULL;

    g_debug("Write %"PRIu64" at %"PRIu64, count, start);
    for (io_start(img, &cur, start, count); io_chunk(&cur); ) {
        if (!_vmnetfs_io_write_chunk(img, buf + cur.buf_offset, cur.chunk,
                        cur.offset, cur.length, &err)) {
            g_warning("%s", err->message);
            g_clear_error(&err);
            return (int) cur.buf_offset ?: -EIO;
        }
        _vmnetfs_u64_stat_increment(img->bytes_written, cur.length);
    }
    if (cur.eof && !cur.buf_offset) {
        return -ENOSPC;
    }
    return cur.buf_offset;
}

static const struct vmnetfs_fuse_ops image_ops = {
    .getattr = image_getattr,
    .open = image_open,
    .read = image_read,
    .write = image_write,
};

void _vmnetfs_fuse_image_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img)
{
    _vmnetfs_fuse_add_file(dir, "image", &image_ops, img);
}
