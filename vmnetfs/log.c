/*
 * vmnetfs - virtual machine network execution virtual filesystem
 *
 * Copyright (C) 2006-2013 Carnegie Mellon University
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
#include "vmnetfs-private.h"

#define STARTUP_BUFFER_SIZE (64 << 10)

struct vmnetfs_log {
    struct vmnetfs_stream_group *sgrp;

    /* Messages produced during startup are queued for the first process
       to open the log file */
    GMutex *lock;
    GQueue *messages;
    uint64_t remaining;
};

/* Handle log messages produced during early startup. */
static void populate_log_stream(struct vmnetfs_stream *strm, void *data)
{
    struct vmnetfs_log *log = data;
    char *message;

    g_mutex_lock(log->lock);
    if (log->messages != NULL) {
        while ((message = g_queue_pop_head(log->messages)) != NULL) {
            _vmnetfs_stream_write(strm, "%s", message);
            g_free(message);
        }
        g_queue_free(log->messages);
        log->messages = NULL;
    }
    g_mutex_unlock(log->lock);
}

static void glib_log_handler(const gchar *log_domain,
        GLogLevelFlags log_level, const gchar *message, void *data)
{
    struct vmnetfs_log *log = data;
    const char *level;
    char *str;
    uint64_t len;

    if (log_level & G_LOG_LEVEL_ERROR) {
        level = "error";
    } else if (log_level & G_LOG_LEVEL_CRITICAL) {
        level = "critical";
    } else if (log_level & G_LOG_LEVEL_WARNING) {
        level = "warning";
    } else if (log_level & G_LOG_LEVEL_MESSAGE) {
        level = "message";
    } else if (log_level & G_LOG_LEVEL_INFO) {
        level = "info";
    } else if (log_level & G_LOG_LEVEL_DEBUG) {
        level = "debug";
    } else {
        level = "unknown";
    }
    str = g_strdup_printf("[%s][%s] %s\n", log_domain ?: "vmnetfs", level,
            message);

    g_mutex_lock(log->lock);
    if (log->messages != NULL) {
        if (log->remaining) {
            g_queue_push_tail(log->messages, str);
            len = strlen(str);
            if (log->remaining > len) {
                log->remaining -= len;
            } else {
                log->remaining = 0;
                g_queue_push_tail(log->messages, g_strdup("[truncated]\n"));
            }
        } else {
            g_free(str);
        }
        g_mutex_unlock(log->lock);
    } else {
        g_mutex_unlock(log->lock);
        _vmnetfs_stream_group_write(log->sgrp, "%s", str);
        g_free(str);
    }
}

/* Modifies global state: the glib log handler. */
struct vmnetfs_log *_vmnetfs_log_init(void)
{
    struct vmnetfs_log *log;

    log = g_slice_new0(struct vmnetfs_log);
    log->sgrp = _vmnetfs_stream_group_new(populate_log_stream, log);
    log->lock = g_mutex_new();
    log->messages = g_queue_new();
    log->remaining = STARTUP_BUFFER_SIZE;
    g_log_set_default_handler(glib_log_handler, log);
    return log;
}

struct vmnetfs_stream_group *_vmnetfs_log_get_stream_group(
        struct vmnetfs_log *log)
{
    return log->sgrp;
}

/* Modifies global state: the glib log handler. */
void _vmnetfs_log_close(struct vmnetfs_log *log)
{
    g_log_set_default_handler(g_log_default_handler, NULL);
    _vmnetfs_stream_group_close(log->sgrp);
}

/* Modifies global state: the glib log handler. */
void _vmnetfs_log_destroy(struct vmnetfs_log *log)
{
    char *message;

    if (log == NULL) {
        return;
    }
    g_log_set_default_handler(g_log_default_handler, NULL);
    _vmnetfs_stream_group_free(log->sgrp);
    g_mutex_lock(log->lock);
    if (log->messages != NULL) {
        while ((message = g_queue_pop_head(log->messages)) != NULL) {
            g_free(message);
        }
        g_queue_free(log->messages);
    }
    g_mutex_unlock(log->lock);
    g_mutex_free(log->lock);
    g_slice_free(struct vmnetfs_log, log);
}
