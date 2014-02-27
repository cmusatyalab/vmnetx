/*
 * vmnetfs - virtual machine network execution virtual filesystem
 *
 * Copyright (C) 2006-2014 Carnegie Mellon University
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
#include "vmnetfs-private.h"

int _vmnetfs_fuse_readonly_pseudo_file_getattr(void *dentry_ctx G_GNUC_UNUSED,
        struct stat *st)
{
    st->st_mode = S_IFREG | 0400;
    return 0;
}

int _vmnetfs_fuse_buffered_file_read(struct vmnetfs_fuse_fh *fh, void *buf,
        uint64_t start, uint64_t count)
{
    uint64_t cur;

    if (fh->length <= start) {
        return 0;
    }
    cur = MIN(count, fh->length - start);
    memcpy(buf, fh->buf + start, cur);
    return cur;
}

void _vmnetfs_fuse_buffered_file_release(struct vmnetfs_fuse_fh *fh)
{
    g_free(fh->buf);
}
