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
#include "vmnetfs-private.h"

/* struct bitmap requires external serialization to ensure that the bits
   don't change while the caller requires them to be consistent. */
struct bitmap {
    GMutex *lock;
    uint8_t *bits;
    uint64_t allocated_bytes;
    struct vmnetfs_stream_group *sgrp;
};

static void populate_stream(struct vmnetfs_stream *strm, void *_map)
{
    struct bitmap *map = _map;
    uint64_t byte;
    uint8_t bit;

    g_mutex_lock(map->lock);
    for (byte = 0; byte < map->allocated_bytes; byte++) {
        if (map->bits[byte]) {
            for (bit = 0; bit < 8; bit++) {
                if (map->bits[byte] & (1 << bit)) {
                    _vmnetfs_stream_write(strm, "%"PRIu64"\n",
                            byte * 8 + bit);
                }
            }
        }
    }
    g_mutex_unlock(map->lock);
}

struct bitmap *_vmnetfs_bit_new(void)
{
    struct bitmap *map;

    map = g_slice_new0(struct bitmap);
    map->lock = g_mutex_new();
    map->sgrp = _vmnetfs_stream_group_new(populate_stream, map);
    return map;
}

void _vmnetfs_bit_free(struct bitmap *map)
{
    _vmnetfs_stream_group_free(map->sgrp);
    g_free(map->bits);
    g_mutex_free(map->lock);
    g_slice_free(struct bitmap, map);
}

void _vmnetfs_bit_set(struct bitmap *map, uint64_t bit)
{
    bool is_new;

    g_mutex_lock(map->lock);
    if (bit >= map->allocated_bytes * 8) {
        /* Resize to the next larger power of two. */
        uint64_t new_size = 1 << g_bit_storage((bit + 7) / 8);
        map->bits = g_realloc(map->bits, new_size);
        memset(map->bits + map->allocated_bytes, 0,
                new_size - map->allocated_bytes);
        map->allocated_bytes = new_size;
    }
    is_new = !(map->bits[bit / 8] & (1 << (bit % 8)));
    map->bits[bit / 8] |= 1 << (bit % 8);
    g_mutex_unlock(map->lock);
    if (is_new) {
        _vmnetfs_stream_group_write(map->sgrp, "%"PRIu64"\n", bit);
    }
}

bool _vmnetfs_bit_test(struct bitmap *map, uint64_t bit)
{
    bool ret = false;

    g_mutex_lock(map->lock);
    if (bit < map->allocated_bytes * 8) {
        ret = !!(map->bits[bit / 8] & (1 << (bit % 8)));
    }
    g_mutex_unlock(map->lock);
    return ret;
}

struct vmnetfs_stream_group *_vmnetfs_bit_get_stream_group(struct bitmap *map)
{
    return map->sgrp;
}
