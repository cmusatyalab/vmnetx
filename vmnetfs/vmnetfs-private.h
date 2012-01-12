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

#ifndef VMNETFS_PRIVATE_H
#define VMNETFS_PRIVATE_H

#define G_LOG_DOMAIN "vmnetfs"

#include <sys/stat.h>
#include <stdint.h>
#include <stdbool.h>
#include <glib.h>
#include "vmnetfs.h"
#include "config.h"

#if HAVE_VISIBILITY
#define exported __attribute__((visibility("default")))
#else
#define exported
#endif

struct vmnetfs {
    struct vmnetfs_image *disk;
    struct vmnetfs_image *memory;
    struct vmnetfs_fuse *fuse;
    char *error;  /* atomic operations only */
};

struct vmnetfs_image {
    char *url;
    char *read_base;
    uint64_t size;
    uint64_t chunks;
    /* if nonzero, server file is divided into segments of this size */
    uint64_t segment_size;
    uint32_t chunk_size;

    /* io */
    struct connection_pool *cpool;
    struct chunk_lock *chunk_locks;
    struct bitmap *accessed_map;

    /* ll_pristine */
    struct bitmap *present_map;

    /* ll_modified */
    int write_fd;
    struct bitmap *modified_map;

    /* stats */
    struct vmnetfs_stream_group *io_stream;
    struct vmnetfs_stat *bytes_read;
    struct vmnetfs_stat *bytes_written;
    struct vmnetfs_stat *chunk_fetches;
    struct vmnetfs_stat *chunk_dirties;
};

struct vmnetfs_fuse {
    struct vmnetfs *fs;
    char *mountpoint;
    struct vmnetfs_fuse_dentry *root;
    struct fuse *fuse;
    struct fuse_chan *chan;
};

struct vmnetfs_fuse_fh {
    const struct vmnetfs_fuse_ops *ops;
    void *data;
    uint64_t length;
    bool blocking;
};

struct vmnetfs_fuse_ops {
    int (*getattr)(void *dentry_ctx, struct stat *st);
    int (*open)(void *dentry_ctx, struct vmnetfs_fuse_fh *fh);
    int (*read)(struct vmnetfs_fuse_fh *fh, void *buf, uint64_t start,
            uint64_t count);
    int (*write)(struct vmnetfs_fuse_fh *fh, const void *buf,
            uint64_t start, uint64_t count);
    void (*release)(struct vmnetfs_fuse_fh *fh);
    bool nonseekable;
};

#define VMNETFS_FUSE_ERROR _vmnetfs_fuse_error_quark()
#define VMNETFS_IO_ERROR _vmnetfs_io_error_quark()
#define VMNETFS_STREAM_ERROR _vmnetfs_stream_error_quark()
#define VMNETFS_TRANSPORT_ERROR _vmnetfs_transport_error_quark()

enum VMNetFSFUSEError {
    VMNETFS_FUSE_ERROR_FAILED,
    VMNETFS_FUSE_ERROR_BAD_MOUNTPOINT,
};

enum VMNetFSIOError {
    VMNETFS_IO_ERROR_PREMATURE_EOF,
    VMNETFS_IO_ERROR_INVALID_CACHE,
    VMNETFS_IO_ERROR_INTERRUPTED,
};

enum VMNetFSStreamError {
    VMNETFS_STREAM_ERROR_NONBLOCKING,
};

enum VMNetFSTransportError {
    VMNETFS_TRANSPORT_ERROR_FATAL,
    VMNETFS_TRANSPORT_ERROR_NETWORK,
};

/* fuse */
struct vmnetfs_fuse *_vmnetfs_fuse_new(struct vmnetfs *fs,
        const char *mountpoint, GError **err);
void _vmnetfs_fuse_run(struct vmnetfs_fuse *fuse);
void _vmnetfs_fuse_free(struct vmnetfs_fuse *fuse);
struct vmnetfs_fuse_dentry *_vmnetfs_fuse_add_dir(
        struct vmnetfs_fuse_dentry *parent, const char *name);
void _vmnetfs_fuse_add_file(struct vmnetfs_fuse_dentry *parent,
        const char *name, const struct vmnetfs_fuse_ops *ops, void *ctx);
void _vmnetfs_fuse_image_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img);
void _vmnetfs_fuse_stats_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img);
void _vmnetfs_fuse_stream_populate(struct vmnetfs_fuse_dentry *dir,
        struct vmnetfs_image *img);
bool _vmnetfs_interrupted(void);

/* io */
bool _vmnetfs_io_init(struct vmnetfs_image *img, GError **err);
void _vmnetfs_io_destroy(struct vmnetfs_image *img);
bool _vmnetfs_io_read_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err);
bool _vmnetfs_io_write_chunk(struct vmnetfs_image *img, const void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err);

/* ll_pristine */
bool _vmnetfs_ll_pristine_init(struct vmnetfs_image *img, GError **err);
void _vmnetfs_ll_pristine_destroy(struct vmnetfs_image *img);
bool _vmnetfs_ll_pristine_read_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err);
bool _vmnetfs_ll_pristine_write_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t length, GError **err);

/* ll_modified */
bool _vmnetfs_ll_modified_init(struct vmnetfs_image *img, GError **err);
void _vmnetfs_ll_modified_destroy(struct vmnetfs_image *img);
bool _vmnetfs_ll_modified_read_chunk(struct vmnetfs_image *img, void *data,
        uint64_t chunk, uint32_t offset, uint32_t length, GError **err);
bool _vmnetfs_ll_modified_write_chunk(struct vmnetfs_image *img,
        const void *data, uint64_t chunk, uint32_t offset, uint32_t length,
        GError **err);

/* transport */
bool _vmnetfs_transport_init(void);
struct connection_pool *_vmnetfs_transport_pool_new(void);
void _vmnetfs_transport_pool_free(struct connection_pool *cpool);
bool _vmnetfs_transport_fetch(struct connection_pool *cpool, const char *url,
        void *buf, uint64_t offset, uint64_t length, GError **err);

/* bitmap */
struct bitmap *_vmnetfs_bit_new(uint64_t bits);
void _vmnetfs_bit_free(struct bitmap *map);
void _vmnetfs_bit_set(struct bitmap *map, uint64_t bit);
bool _vmnetfs_bit_test(struct bitmap *map, uint64_t bit);
struct vmnetfs_stream_group *_vmnetfs_bit_get_stream_group(struct bitmap *map);

/* stream */
struct vmnetfs_stream;
typedef void (populate_stream_fn)(struct vmnetfs_stream *strm, void *data);
struct vmnetfs_stream_group *_vmnetfs_stream_group_new(
        populate_stream_fn *populate, void *populate_data);
void _vmnetfs_stream_group_free(struct vmnetfs_stream_group *sgrp);
struct vmnetfs_stream *_vmnetfs_stream_new(struct vmnetfs_stream_group *sgrp);
void _vmnetfs_stream_free(struct vmnetfs_stream *strm);
uint64_t _vmnetfs_stream_read(struct vmnetfs_stream *strm, void *buf,
        uint64_t count, bool blocking, GError **err);
void _vmnetfs_stream_write(struct vmnetfs_stream *strm, const char *fmt, ...);
void _vmnetfs_stream_group_write(struct vmnetfs_stream_group *sgrp,
        const char *fmt, ...);

/* stats */
struct vmnetfs_stat *_vmnetfs_stat_new(void);
void _vmnetfs_stat_free(struct vmnetfs_stat *stat);
void _vmnetfs_u64_stat_increment(struct vmnetfs_stat *stat, uint64_t val);
uint64_t _vmnetfs_u64_stat_get(struct vmnetfs_stat *stat);

/* cond */
struct vmnetfs_cond *_vmnetfs_cond_new(void);
void _vmnetfs_cond_free(struct vmnetfs_cond *cond);
bool _vmnetfs_cond_wait(struct vmnetfs_cond *cond, GMutex *lock);
void _vmnetfs_cond_signal(struct vmnetfs_cond *cond);
void _vmnetfs_cond_broadcast(struct vmnetfs_cond *cond);

/* utility */
void _vmnetfs_set_error(struct vmnetfs *fs, const char *fmt, ...);
GQuark _vmnetfs_fuse_error_quark(void);
GQuark _vmnetfs_io_error_quark(void);
GQuark _vmnetfs_stream_error_quark(void);
GQuark _vmnetfs_transport_error_quark(void);
bool _vmnetfs_safe_pread(const char *file, int fd, void *buf, uint64_t count,
        uint64_t offset, GError **err);
bool _vmnetfs_safe_pwrite(const char *file, int fd, const void *buf,
        uint64_t count, uint64_t offset, GError **err);

#endif
