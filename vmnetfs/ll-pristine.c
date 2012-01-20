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

#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>
#include <inttypes.h>
#include <errno.h>
#include "vmnetfs-private.h"

#define CHUNKS_PER_DIR 4096

static bool mkdir_with_parents(const char *dir, GError **err)
{
    if (g_mkdir_with_parents(dir, 0700)) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't create %s: %s", dir, strerror(errno));
        return false;
    }
    return true;
}

static uint64_t get_dir_num(uint64_t chunk)
{
    return chunk / CHUNKS_PER_DIR * CHUNKS_PER_DIR;
}

static char *get_dir(struct vmnetfs_image *img, uint64_t chunk)
{
    return g_strdup_printf("%s/%"PRIu64, img->read_base, get_dir_num(chunk));
}

static char *get_file(struct vmnetfs_image *img, uint64_t chunk)
{
    return g_strdup_printf("%s/%"PRIu64"/%"PRIu64, img->read_base,
            get_dir_num(chunk), chunk);
}

static bool set_present_from_directory(struct vmnetfs_image *img,
        const char *path, uint64_t dir_num, GError **err)
{
    GDir *dir;
    const char *file;
    uint64_t chunk;
    uint64_t chunks;
    char *endptr;

    chunks = (img->initial_size + img->chunk_size - 1) / img->chunk_size;
    dir = g_dir_open(path, 0, err);
    if (dir == NULL) {
        return false;
    }
    while ((file = g_dir_read_name(dir)) != NULL) {
        chunk = g_ascii_strtoull(file, &endptr, 10);
        if (*file == 0 || *endptr != 0 || chunk > chunks ||
                dir_num != get_dir_num(chunk)) {
            g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INVALID_CACHE,
                    "Invalid cache entry %s/%s", path, file);
            g_dir_close(dir);
            return false;
        }
        _vmnetfs_bit_set(img->present_map, chunk);
    }
    g_dir_close(dir);
    return true;
}

bool _vmnetfs_ll_pristine_init(struct vmnetfs_image *img, GError **err)
{
    GDir *dir;
    const char *name;
    char *path;
    char *endptr;
    uint64_t dir_num;

    if (!mkdir_with_parents(img->read_base, err)) {
        return false;
    }

    dir = g_dir_open(img->read_base, 0, err);
    if (dir == NULL) {
        return false;
    }
    img->present_map = _vmnetfs_bit_new();
    while ((name = g_dir_read_name(dir)) != NULL) {
        path = g_strdup_printf("%s/%s", img->read_base, name);
        dir_num = g_ascii_strtoull(name, &endptr, 10);
        if (*name != 0 && *endptr == 0 && g_file_test(path,
                G_FILE_TEST_IS_DIR)) {
            if (!set_present_from_directory(img, path, dir_num, err)) {
                g_free(path);
                g_dir_close(dir);
                _vmnetfs_bit_free(img->present_map);
                return false;
            }
        }
        g_free(path);
    }
    g_dir_close(dir);
    return true;
}

void _vmnetfs_ll_pristine_close(struct vmnetfs_image *img)
{
    _vmnetfs_stream_group_close(_vmnetfs_bit_get_stream_group(
            img->present_map));
}

void _vmnetfs_ll_pristine_destroy(struct vmnetfs_image *img)
{
    _vmnetfs_bit_free(img->present_map);
}

bool _vmnetfs_ll_pristine_read_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err)
{
    char *file;
    int fd;
    bool ret;

    g_assert(_vmnetfs_bit_test(img->present_map, chunk));
    g_assert(offset < img->chunk_size);
    g_assert(offset + length <= img->chunk_size);
    g_assert(chunk * img->chunk_size + offset + length <= img->initial_size);

    file = get_file(img, chunk);
    fd = open(file, O_RDONLY);
    if (fd == -1) {
        g_set_error(err, G_FILE_ERROR, g_file_error_from_errno(errno),
                "Couldn't open %s: %s", file, strerror(errno));
        g_free(file);
        return false;
    }
    ret = _vmnetfs_safe_pread(file, fd, data, length, offset, err);
    close(fd);
    g_free(file);
    return ret;
}

bool _vmnetfs_ll_pristine_write_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t length, GError **err)
{
    char *dir;
    char *file;
    bool ret;

    g_assert(length <= img->chunk_size);
    g_assert(chunk * img->chunk_size + length <= img->initial_size);

    dir = get_dir(img, chunk);
    file = get_file(img, chunk);

    ret = mkdir_with_parents(dir, err);
    if (!ret) {
        goto out;
    }
    ret = g_file_set_contents(file, data, length, err);
    if (!ret) {
        goto out;
    }
    _vmnetfs_bit_set(img->present_map, chunk);

out:
    g_free(file);
    g_free(dir);
    return ret;
}
