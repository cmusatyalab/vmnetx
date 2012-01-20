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

static char *format_u64(uint64_t val)
{
    return g_strdup_printf("%"PRIu64"\n", val);
}

static int stat_getattr(void *dentry_ctx G_GNUC_UNUSED, struct stat *st)
{
    st->st_mode = S_IFREG | 0400;
    return 0;
}

static int u64_stat_open(void *dentry_ctx, struct vmnetfs_fuse_fh *fh)
{
    struct vmnetfs_stat *stat = dentry_ctx;
    struct vmnetfs_stat_handle *hdl;

    if (_vmnetfs_stat_is_closed(stat)) {
        return -EACCES;
    }
    fh->buf = format_u64(_vmnetfs_u64_stat_get(stat, &hdl));
    fh->length = strlen(fh->buf);
    fh->data = hdl;
    return 0;
}

static int u64_fixed_open(void *dentry_ctx, struct vmnetfs_fuse_fh *fh)
{
    uint64_t *val = dentry_ctx;

    fh->buf = format_u64(*val);
    fh->length = strlen(fh->buf);
    return 0;
}

static int chunks_open(void *dentry_ctx, struct vmnetfs_fuse_fh *fh)
{
    struct vmnetfs_image *img = dentry_ctx;
    uint64_t len;

    len = (_vmnetfs_io_get_image_size(img) + img->chunk_size - 1) /
            img->chunk_size;
    fh->buf = format_u64(len);
    fh->length = strlen(fh->buf);
    return 0;
}

static int stat_read(struct vmnetfs_fuse_fh *fh, void *buf, uint64_t start,
        uint64_t count)
{
    uint64_t cur;

    if (fh->length <= start) {
        return 0;
    }
    cur = MIN(count, fh->length - start);
    memcpy(buf, fh->buf + start, cur);
    return cur;
}

static int stat_poll(struct vmnetfs_fuse_fh *fh, struct fuse_pollhandle *ph,
        bool *readable)
{
    struct vmnetfs_stat_handle *hdl = fh->data;

    g_assert(hdl != NULL);
    if (ph != NULL) {
        _vmnetfs_stat_handle_set_poll(hdl, ph);
    }
    *readable = _vmnetfs_stat_handle_is_changed(hdl);
    return 0;
}

static void stat_release(struct vmnetfs_fuse_fh *fh)
{
    _vmnetfs_stat_handle_free(fh->data);
    g_free(fh->buf);
}

static const struct vmnetfs_fuse_ops u64_stat_ops = {
    .getattr = stat_getattr,
    .open = u64_stat_open,
    .read = stat_read,
    .poll = stat_poll,
    .release = stat_release,
};

static const struct vmnetfs_fuse_ops u64_fixed_ops = {
    .getattr = stat_getattr,
    .open = u64_fixed_open,
    .read = stat_read,
    .release = stat_release,
};

static const struct vmnetfs_fuse_ops chunks_ops = {
    .getattr = stat_getattr,
    .open = chunks_open,
    .read = stat_read,
    .release = stat_release,
};

void _vmnetfs_fuse_stats_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img)
{
    struct vmnetfs_fuse_dentry *stats;

    stats = _vmnetfs_fuse_add_dir(dir, "stats");

#define add_stat(n) _vmnetfs_fuse_add_file(stats, #n, &u64_stat_ops, img->n)
    add_stat(bytes_read);
    add_stat(bytes_written);
    add_stat(chunk_fetches);
    add_stat(chunk_dirties);
#undef add_stat

#define add_fixed(n) _vmnetfs_fuse_add_file(stats, #n, &u64_fixed_ops, &img->n)
    add_fixed(chunk_size);
#undef add_fixed

    _vmnetfs_fuse_add_file(stats, "chunks", &chunks_ops, img);
}
