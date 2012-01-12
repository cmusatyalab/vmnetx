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

#include "vmnetfs-private.h"

struct vmnetfs_stat {
    GMutex *lock;
    GList *unchanged_handles;
    uint64_t u64;
};

/* A reference to a particular point in the history of a vmnetfs_stat.
   Can be queried to determine whether the stat has subsequently changed.
   Protected by stat->lock. */
struct vmnetfs_stat_handle {
    struct vmnetfs_stat *stat;
    bool changed;
    struct fuse_pollhandle *ph;  /* only if unchanged */
};

struct vmnetfs_stat *_vmnetfs_stat_new(void)
{
    struct vmnetfs_stat *stat;

    stat = g_slice_new0(struct vmnetfs_stat);
    stat->lock = g_mutex_new();
    return stat;
}

void _vmnetfs_stat_free(struct vmnetfs_stat *stat)
{
    if (stat == NULL) {
        return;
    }
    g_assert(stat->unchanged_handles == NULL);
    g_mutex_free(stat->lock);
    g_slice_free(struct vmnetfs_stat, stat);
}

/* stat lock must be held. */
static struct vmnetfs_stat_handle *stat_handle_new(struct vmnetfs_stat *stat)
{
    struct vmnetfs_stat_handle *hdl;

    hdl = g_slice_new0(struct vmnetfs_stat_handle);
    hdl->stat = stat;
    stat->unchanged_handles = g_list_prepend(stat->unchanged_handles, hdl);
    return hdl;
}

void _vmnetfs_stat_handle_free(struct vmnetfs_stat_handle *hdl)
{
    if (hdl == NULL) {
        return;
    }
    g_mutex_lock(hdl->stat->lock);
    if (!hdl->changed) {
        hdl->stat->unchanged_handles = g_list_remove(
                hdl->stat->unchanged_handles, hdl);
        _vmnetfs_finish_poll(hdl->ph, false);
    }
    g_mutex_unlock(hdl->stat->lock);
    g_slice_free(struct vmnetfs_stat_handle, hdl);
}

void _vmnetfs_stat_handle_set_poll(struct vmnetfs_stat_handle *hdl,
        struct fuse_pollhandle *ph)
{
    g_mutex_lock(hdl->stat->lock);
    if (hdl->changed) {
        _vmnetfs_finish_poll(ph, true);
    } else {
        _vmnetfs_finish_poll(hdl->ph, false);
        hdl->ph = ph;
    }
    g_mutex_unlock(hdl->stat->lock);
}

/* stat lock must be held. */
static void change_stat(struct vmnetfs_stat *stat)
{
    struct vmnetfs_stat_handle *hdl;
    GList *el;

    for (el = g_list_first(stat->unchanged_handles); el != NULL;
            el = g_list_next(el)) {
        hdl = el->data;
        hdl->changed = true;
        _vmnetfs_finish_poll(hdl->ph, true);
        hdl->ph = NULL;
    }
    g_list_free(stat->unchanged_handles);
    stat->unchanged_handles = NULL;
}

bool _vmnetfs_stat_handle_is_changed(struct vmnetfs_stat_handle *hdl)
{
    bool ret;

    g_mutex_lock(hdl->stat->lock);
    ret = hdl->changed;
    g_mutex_unlock(hdl->stat->lock);
    return ret;
}

void _vmnetfs_u64_stat_increment(struct vmnetfs_stat *stat, uint64_t val)
{
    g_mutex_lock(stat->lock);
    stat->u64 += val;
    change_stat(stat);
    g_mutex_unlock(stat->lock);
}

uint64_t _vmnetfs_u64_stat_get(struct vmnetfs_stat *stat,
        struct vmnetfs_stat_handle **hdl)
{
    uint64_t ret;

    g_mutex_lock(stat->lock);
    ret = stat->u64;
    if (hdl != NULL) {
        *hdl = stat_handle_new(stat);
    }
    g_mutex_unlock(stat->lock);
    return ret;
}
