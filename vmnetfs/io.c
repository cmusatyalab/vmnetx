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

#include <inttypes.h>
#include "vmnetfs-private.h"

struct chunk_lock {
    GMutex *lock;
    GHashTable *chunks;
};

struct chunk_lock_entry {
    uint64_t chunk;
    struct vmnetfs_cond *available;
    bool busy;
    uint32_t waiters;
};

static struct chunk_lock *chunk_lock_new(void)
{
    struct chunk_lock *cl;

    cl = g_slice_new0(struct chunk_lock);
    cl->lock = g_mutex_new();
    cl->chunks = g_hash_table_new(g_int64_hash, g_int64_equal);
    return cl;
}

static void chunk_lock_free(struct chunk_lock *cl)
{
    g_assert(g_hash_table_size(cl->chunks) == 0);

    g_hash_table_destroy(cl->chunks);
    g_mutex_free(cl->lock);
    g_slice_free(struct chunk_lock, cl);
}

/* Returns false if the lock was not acquired because the FUSE request
   was interrupted. */
static bool G_GNUC_WARN_UNUSED_RESULT chunk_lock_try_acquire(
        struct chunk_lock *cl, uint64_t chunk)
{
    struct chunk_lock_entry *ent;
    bool ret = true;

    g_mutex_lock(cl->lock);
    ent = g_hash_table_lookup(cl->chunks, &chunk);
    if (ent != NULL) {
        ent->waiters++;
        while (ent->busy &&
                !_vmnetfs_cond_wait(ent->available, cl->lock)) {}
        if (ent->busy) {
            /* We were interrupted, give up.  If we were interrupted but
               also acquired the lock, we pretend we weren't interrupted
               so that we never have to free the lock in this path. */
            ret = false;
        } else {
            ent->busy = true;
        }
        ent->waiters--;
    } else {
        ent = g_slice_new0(struct chunk_lock_entry);
        ent->chunk = chunk;
        ent->available = _vmnetfs_cond_new();
        ent->busy = true;
        g_hash_table_replace(cl->chunks, &ent->chunk, ent);
    }
    g_mutex_unlock(cl->lock);
    return ret;
}

static void chunk_lock_release(struct chunk_lock *cl, uint64_t chunk)
{
    struct chunk_lock_entry *ent;

    g_mutex_lock(cl->lock);
    ent = g_hash_table_lookup(cl->chunks, &chunk);
    g_assert(ent != NULL);
    if (ent->waiters > 0) {
        ent->busy = false;
        _vmnetfs_cond_signal(ent->available);
    } else {
        g_hash_table_remove(cl->chunks, &chunk);
        _vmnetfs_cond_free(ent->available);
        g_slice_free(struct chunk_lock_entry, ent);
    }
    g_mutex_unlock(cl->lock);
}

/* Fetch the specified byte range from the image, accounting for possible
   segmentation into multiple URLs. */
static bool fetch_data(struct vmnetfs_image *img, void *buf, uint64_t start,
        uint64_t count, GError **err)
{
    char *url;
    uint64_t cur_start;
    uint64_t cur_count;
    bool ret;

    while (count > 0) {
        if (img->segment_size) {
            url = g_strdup_printf("%s.%"PRIu64, img->url,
                    start / img->segment_size);
            cur_start = start % img->segment_size;
            cur_count = MIN(img->segment_size - cur_start, count);
        } else {
            url = g_strdup(img->url);
            cur_start = start;
            cur_count = count;
        }
        ret = _vmnetfs_transport_fetch(img->cpool, url, buf, cur_start,
                cur_count, err);
        g_free(url);
        if (!ret) {
            return false;
        }
        buf += cur_count;
        start += cur_count;
        count -= cur_count;
    }
    return true;
}

bool _vmnetfs_io_init(struct vmnetfs_image *img, GError **err)
{
    if (!_vmnetfs_ll_pristine_init(img, err)) {
        return false;
    }
    if (!_vmnetfs_ll_modified_init(img, err)) {
        _vmnetfs_ll_pristine_destroy(img);
        return false;
    }
    img->cpool = _vmnetfs_transport_pool_new();
    img->accessed_map = _vmnetfs_bit_new(img->chunks);
    img->chunk_locks = chunk_lock_new();
    return true;
}

void _vmnetfs_io_close(struct vmnetfs_image *img)
{
    _vmnetfs_stream_group_close(_vmnetfs_bit_get_stream_group(
            img->accessed_map));
    _vmnetfs_ll_pristine_close(img);
    _vmnetfs_ll_modified_close(img);
}

void _vmnetfs_io_destroy(struct vmnetfs_image *img)
{
    if (img == NULL) {
        return;
    }
    _vmnetfs_ll_modified_destroy(img);
    _vmnetfs_ll_pristine_destroy(img);
    chunk_lock_free(img->chunk_locks);
    _vmnetfs_bit_free(img->accessed_map);
    _vmnetfs_transport_pool_free(img->cpool);
}

static bool constrain_io(struct vmnetfs_image *img, uint64_t chunk,
        uint32_t offset, uint32_t *length, GError **err)
{
    g_assert(offset < img->chunk_size);
    g_assert(offset + *length <= img->chunk_size);

    /* If start is after EOF, return EOF. */
    if (chunk * img->chunk_size + offset >= img->size) {
        g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_EOF,
                "End of file");
        return false;
    }

    /* If end is after EOF, constrain it to the valid length of the
       image. */
    *length = MIN(img->size - chunk * img->chunk_size, *length);
    return true;
}

static bool read_chunk_unlocked(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err)
{
    if (!constrain_io(img, chunk, offset, &length, err)) {
        return false;
    }
    _vmnetfs_bit_set(img->accessed_map, chunk);
    if (_vmnetfs_bit_test(img->modified_map, chunk)) {
        return _vmnetfs_ll_modified_read_chunk(img, data, chunk, offset,
                length, err);
    } else {
        /* If two vmnetfs instances are working out of the same pristine
           cache, they will redundantly fetch chunks due to our failure to
           keep the present map up to date. */
        if (!_vmnetfs_bit_test(img->present_map, chunk)) {
            uint64_t start = chunk * img->chunk_size;
            uint64_t count = MIN(img->size - start, img->chunk_size);
            void *buf = g_malloc(count);

            _vmnetfs_u64_stat_increment(img->chunk_fetches, 1);
            if (!fetch_data(img, buf, start, count, err)) {
                g_free(buf);
                return false;
            }
            bool ret = _vmnetfs_ll_pristine_write_chunk(img, buf, chunk,
                    count, err);
            g_free(buf);
            if (!ret) {
                return false;
            }
        }
        return _vmnetfs_ll_pristine_read_chunk(img, data, chunk, offset,
                length, err);
    }
}

bool _vmnetfs_io_read_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err)
{
    bool ret;

    if (!chunk_lock_try_acquire(img->chunk_locks, chunk)) {
        g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INTERRUPTED,
                "Operation interrupted");
        return false;
    }
    ret = read_chunk_unlocked(img, data, chunk, offset, length, err);
    chunk_lock_release(img->chunk_locks, chunk);
    return ret;
}

bool _vmnetfs_io_write_chunk(struct vmnetfs_image *img, const void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err)
{
    bool ret;

    if (!constrain_io(img, chunk, offset, &length, err)) {
        return false;
    }
    if (!chunk_lock_try_acquire(img->chunk_locks, chunk)) {
        g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INTERRUPTED,
                "Operation interrupted");
        return false;
    }
    _vmnetfs_bit_set(img->accessed_map, chunk);
    if (!_vmnetfs_bit_test(img->modified_map, chunk)) {
        uint64_t count = MIN(img->size - chunk * img->chunk_size,
                img->chunk_size);
        void *buf = g_malloc(count);

        _vmnetfs_u64_stat_increment(img->chunk_dirties, 1);
        ret = read_chunk_unlocked(img, buf, chunk, 0, count, err);
        if (!ret) {
            g_free(buf);
            goto out;
        }
        ret = _vmnetfs_ll_modified_write_chunk(img, buf, chunk, 0, count,
                err);
        g_free(buf);
        if (!ret) {
            goto out;
        }
    }
    ret = _vmnetfs_ll_modified_write_chunk(img, data, chunk, offset, length,
            err);
out:
    chunk_lock_release(img->chunk_locks, chunk);
    return ret;
}
