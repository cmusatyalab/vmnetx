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

static void image_free(struct vmnetfs_image *img)
{
    if (img == NULL) {
        return;
    }
    _vmnetfs_io_destroy(img);
    _vmnetfs_stream_group_free(img->io_stream);
    _vmnetfs_stat_free(img->bytes_read);
    _vmnetfs_stat_free(img->bytes_written);
    _vmnetfs_stat_free(img->chunk_reads);
    _vmnetfs_stat_free(img->chunk_writes);
    _vmnetfs_stat_free(img->chunk_fetches);
    _vmnetfs_stat_free(img->chunk_dirties);
    g_free(img->url);
    g_free(img->read_base);
    g_slice_free(struct vmnetfs_image, img);
}

static struct vmnetfs_image *image_new(const char *url, const char *cache,
        uint64_t size, uint64_t segment_size, uint32_t chunk_size,
        GError **err)
{
    struct vmnetfs_image *img;

    img = g_slice_new0(struct vmnetfs_image);
    img->url = g_strdup(url);
    img->read_base = g_strdup(cache);
    img->size = size;
    img->segment_size = segment_size;
    img->chunk_size = chunk_size;
    img->chunks = (size + chunk_size - 1) / chunk_size;

    img->io_stream = _vmnetfs_stream_group_new(NULL, NULL);
    img->bytes_read = _vmnetfs_stat_new();
    img->bytes_written = _vmnetfs_stat_new();
    img->chunk_reads = _vmnetfs_stat_new();
    img->chunk_writes = _vmnetfs_stat_new();
    img->chunk_fetches = _vmnetfs_stat_new();
    img->chunk_dirties = _vmnetfs_stat_new();

    if (!_vmnetfs_io_init(img, err)) {
        image_free(img);
        return NULL;
    }

    return img;
}

exported bool vmnetfs_init(void)
{
    if (!g_thread_supported()) {
        g_thread_init(NULL);
    }
    return _vmnetfs_transport_init();
}

/* This function always returns a struct vmnetfs *, even on failure. */
exported struct vmnetfs *vmnetfs_new(const char *mountpoint,
        const char *disk_url, const char *disk_cache,
        uint64_t disk_size, uint64_t disk_segment_size,
        uint32_t disk_chunk_size,
        const char *memory_url, const char *memory_cache,
        uint64_t memory_size, uint64_t memory_segment_size,
        uint32_t memory_chunk_size)
{
    struct vmnetfs *fs;
    GError *err = NULL;

    fs = g_slice_new0(struct vmnetfs);
    fs->disk = image_new(disk_url, disk_cache, disk_size, disk_segment_size,
            disk_chunk_size, &err);
    if (err) {
        _vmnetfs_set_error(fs, "%s", err->message);
        g_clear_error(&err);
        return fs;
    }

    fs->memory = image_new(memory_url, memory_cache, memory_size,
            memory_segment_size, memory_chunk_size, &err);
    if (err) {
        _vmnetfs_set_error(fs, "%s", err->message);
        g_clear_error(&err);
        return fs;
    }

    fs->fuse = _vmnetfs_fuse_new(fs, mountpoint, &err);
    if (err) {
        _vmnetfs_set_error(fs, "%s", err->message);
        g_clear_error(&err);
        return fs;
    }

    return fs;
}

exported const char *vmnetfs_get_error(struct vmnetfs *fs)
{
    return g_atomic_pointer_get(&fs->error);
}

/* runs until the filesystem is unmounted */
exported void vmnetfs_run(struct vmnetfs *fs)
{
    if (vmnetfs_get_error(fs)) {
        return;
    }
    _vmnetfs_fuse_run(fs->fuse);
}

exported void vmnetfs_free(struct vmnetfs *fs)
{
    _vmnetfs_fuse_free(fs->fuse);
    image_free(fs->disk);
    image_free(fs->memory);
    g_free(fs->error);
    g_slice_free(struct vmnetfs, fs);
}
