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
    uint64_t u64;
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
    g_mutex_free(stat->lock);
    g_slice_free(struct vmnetfs_stat, stat);
}

void _vmnetfs_u64_stat_increment(struct vmnetfs_stat *stat, uint64_t val)
{
    g_mutex_lock(stat->lock);
    stat->u64 += val;
    g_mutex_unlock(stat->lock);
}

uint64_t _vmnetfs_u64_stat_get(struct vmnetfs_stat *stat)
{
    uint64_t ret;

    g_mutex_lock(stat->lock);
    ret = stat->u64;
    g_mutex_unlock(stat->lock);
    return ret;
}
