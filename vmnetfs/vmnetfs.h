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

#ifndef VMNETFS_H
#define VMNETFS_H

bool vmnetfs_init(void);

struct vmnetfs *vmnetfs_new(const char *mountpoint,
        const char *disk_url, const char *disk_cache,
        uint64_t disk_size, uint64_t disk_segment_size,
        uint32_t disk_chunk_size,
        const char *memory_url, const char *memory_cache,
        uint64_t memory_size, uint64_t memory_segment_size,
        uint32_t memory_chunk_size);

const char *vmnetfs_get_error(struct vmnetfs *fs);

// runs until the filesystem is unmounted
void vmnetfs_run(struct vmnetfs *fs);

void vmnetfs_terminate(struct vmnetfs *fs);

void vmnetfs_free(struct vmnetfs *fs);

#endif
