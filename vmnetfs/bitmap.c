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

/* struct bitmap requires external serialization to ensure that the bits
   don't change while the caller requires them to be consistent. The
   internal serialization ensures independent stores from different threads
   don't cause corruption. */
struct bitmap {
    GMutex *lock;
    uint8_t *bits;
    uint64_t count;
};

struct bitmap *_vmnetfs_bit_new(uint64_t bits)
{
    struct bitmap *map;

    map = g_slice_new0(struct bitmap);
    map->lock = g_mutex_new();
    map->bits = g_malloc0((bits + 7) / 8);
    map->count = bits;
    return map;
}

void _vmnetfs_bit_free(struct bitmap *map)
{
    g_free(map->bits);
    g_mutex_free(map->lock);
    g_slice_free(struct bitmap, map);
}

void _vmnetfs_bit_set(struct bitmap *map, uint64_t bit)
{
    g_assert(bit < map->count);
    g_mutex_lock(map->lock);
    map->bits[bit / 8] |= 1 << (7 - (bit % 8));
    g_mutex_unlock(map->lock);
}

bool _vmnetfs_bit_test(struct bitmap *map, uint64_t bit)
{
    bool ret;

    g_assert(bit < map->count);
    g_mutex_lock(map->lock);
    ret = !!(map->bits[bit / 8] & (1 << (7 - (bit % 8))));
    g_mutex_unlock(map->lock);
    return ret;
}
