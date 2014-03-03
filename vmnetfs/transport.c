/*
 * vmnetfs - virtual machine network execution virtual filesystem
 *
 * Copyright (C) 2006-2014 Carnegie Mellon University
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
#include <unistd.h>
#include <curl/curl.h>
#include "vmnetfs-private.h"

#define TRANSPORT_TRIES 5
#define TRANSPORT_RETRY_DELAY 5

struct connection_pool {
    GQueue *conns;
    GMutex *lock;
    CURLSH *share;
    char *user_agent;
};

struct connection {
    struct connection_pool *pool;
    CURL *curl;
    char errbuf[CURL_ERROR_SIZE];
    GError *err;
    char *buf;
    stream_fn *callback;
    void *arg;
    uint64_t offset;
    uint64_t length;
    const char *expected_etag;
    time_t expected_last_modified;
    char *etag;
    should_cancel_fn *should_cancel;
    void *should_cancel_arg;
};

static size_t header_callback(void *data, size_t size, size_t nmemb,
        void *private)
{
    struct connection *conn = private;
    char *header = g_strndup(data, size * nmemb);
    GString *lower = g_string_new(header);
    char **split;

    g_string_ascii_down(lower);
    if (g_str_has_prefix(lower->str, "http/")) {
        /* Followed a redirect; start over */
        g_free(conn->etag);
        conn->etag = NULL;
    } else if (g_str_has_prefix(lower->str, "etag:")) {
        split = g_strsplit(header, ":", 2);
        g_strstrip(split[1]);
        g_free(conn->etag);
        conn->etag = g_strdup(split[1]);
        g_strfreev(split);
    }
    g_string_free(lower, true);
    g_free(header);
    return size * nmemb;
}

static bool check_validators(struct connection *conn, GError **err)
{
    long filetime;

    if (conn->expected_etag) {
        if (conn->etag == NULL) {
            g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                    VMNETFS_TRANSPORT_ERROR_FATAL,
                    "Server did not return ETag");
            return false;
        }
        if (strcmp(conn->expected_etag, conn->etag)) {
            g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                    VMNETFS_TRANSPORT_ERROR_FATAL,
                    "ETag mismatch; expected %s, found %s",
                    conn->expected_etag, conn->etag);
            return false;
        }
    }
    if (conn->expected_last_modified) {
        if (curl_easy_getinfo(conn->curl, CURLINFO_FILETIME, &filetime)) {
            g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                    VMNETFS_TRANSPORT_ERROR_FATAL,
                    "Couldn't read Last-Modified time");
            return false;
        }
        if (filetime != conn->expected_last_modified) {
            g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                    VMNETFS_TRANSPORT_ERROR_FATAL,
                    "Timestamp mismatch; expected %"PRIu64", found %ld",
                    (uint64_t) conn->expected_last_modified, filetime);
            return false;
        }
    }
    return true;
}

static size_t write_callback(void *data, size_t size, size_t nmemb,
        void *private)
{
    struct connection *conn = private;
    uint64_t count = MIN(size * nmemb, conn->length - conn->offset);

    g_return_val_if_fail(conn->err == NULL, 0);

    if (conn->offset == 0) {
        /* First received data; check validators */
        if (!check_validators(conn, &conn->err)) {
            return 0;
        }
    }

    if (conn->callback) {
        if (!conn->callback(conn->arg, data, count, &conn->err)) {
            return 0;
        }
    } else {
        memcpy(conn->buf + conn->offset, data, count);
    }
    conn->offset += count;
    return count;
}

static int progress_callback(void *private,
        double dltotal G_GNUC_UNUSED, double dlnow G_GNUC_UNUSED,
        double ultotal G_GNUC_UNUSED, double ulnow G_GNUC_UNUSED)
{
    struct connection *conn = private;
    bool cancel = false;

    if (conn->should_cancel) {
        cancel = conn->should_cancel(conn->should_cancel_arg);
    }
    return cancel;
}

static void conn_free(struct connection *conn)
{
    g_free(conn->etag);
    if (conn->curl) {
        curl_easy_cleanup(conn->curl);
    }
    g_slice_free(struct connection, conn);
}

static struct connection *conn_new(struct connection_pool *pool,
        GError **err)
{
    struct connection *conn;

    conn = g_slice_new0(struct connection);
    conn->pool = pool;
    conn->curl = curl_easy_init();
    if (conn->curl == NULL) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't initialize CURL handle");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_NOPROGRESS, 0)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't enable curl progress meter");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_NOSIGNAL, 1)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't disable signals");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_SHARE, pool->share)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set share handle");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_FILETIME, 1)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't enable file timestamps");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_USERAGENT, pool->user_agent)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set user agent string");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_HTTPAUTH,
            CURLAUTH_BASIC | CURLAUTH_DIGEST)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't configure authentication");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_HEADERFUNCTION,
            header_callback)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set header callback");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_HEADERDATA, conn)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set header callback data");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_WRITEFUNCTION, write_callback)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set write callback");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_WRITEDATA, conn)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set write callback data");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_PROGRESSFUNCTION,
            progress_callback)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set progress callback");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_PROGRESSDATA, conn)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set progress data");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_ERRORBUFFER, conn->errbuf)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set error buffer");
        goto bad;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_FAILONERROR, 1)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set fail-on-error flag");
        goto bad;
    }
    return conn;

bad:
    conn_free(conn);
    return NULL;
}

static struct connection *conn_get(struct connection_pool *cpool,
        GError **err)
{
    struct connection *conn;

    g_mutex_lock(cpool->lock);
    conn = g_queue_pop_head(cpool->conns);
    g_mutex_unlock(cpool->lock);
    if (conn == NULL) {
        conn = conn_new(cpool, err);
    }
    return conn;
}

static void conn_put(struct connection *conn)
{
    g_free(conn->etag);
    conn->etag = NULL;
    g_mutex_lock(conn->pool->lock);
    g_queue_push_head(conn->pool->conns, conn);
    g_mutex_unlock(conn->pool->lock);
}

static void lock_callback(CURL *handle G_GNUC_UNUSED,
        curl_lock_data data G_GNUC_UNUSED,
        curl_lock_access access G_GNUC_UNUSED, void *private)
{
    struct connection_pool *cpool = private;

    g_mutex_lock(cpool->lock);
}

static void unlock_callback(CURL *handle G_GNUC_UNUSED,
        curl_lock_data data G_GNUC_UNUSED, void *private)
{
    struct connection_pool *cpool = private;

    g_mutex_unlock(cpool->lock);
}

bool _vmnetfs_transport_init(void)
{
    if (curl_global_init(CURL_GLOBAL_ALL)) {
        return false;
    }
    return true;
}

struct connection_pool *_vmnetfs_transport_pool_new(GError **err)
{
    struct connection_pool *cpool;

    cpool = g_slice_new0(struct connection_pool);
    cpool->conns = g_queue_new();
    cpool->lock = g_mutex_new();
    cpool->share = curl_share_init();
    cpool->user_agent = g_strdup_printf("vmnetfs/" PACKAGE_VERSION " %s",
            curl_version());

    if (cpool->share == NULL) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't initialize share handle");
        goto bad;
    }
    if (curl_share_setopt(cpool->share, CURLSHOPT_USERDATA, cpool)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set share handle private data");
        goto bad;
    }
    if (curl_share_setopt(cpool->share, CURLSHOPT_LOCKFUNC, lock_callback)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set lock callback");
        goto bad;
    }
    if (curl_share_setopt(cpool->share, CURLSHOPT_UNLOCKFUNC,
            unlock_callback)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set unlock callback");
        goto bad;
    }
    if (curl_share_setopt(cpool->share, CURLSHOPT_SHARE,
            CURL_LOCK_DATA_COOKIE)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't enable cookie sharing");
        goto bad;
    }
    if (curl_share_setopt(cpool->share, CURLSHOPT_SHARE,
            CURL_LOCK_DATA_DNS)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't enable DNS sharing");
        goto bad;
    }
    /* Not supported on RHEL 6, so ignore failures */
    curl_share_setopt(cpool->share, CURLSHOPT_SHARE,
            CURL_LOCK_DATA_SSL_SESSION);

    return cpool;

bad:
    g_free(cpool->user_agent);
    if (cpool->share) {
        curl_share_cleanup(cpool->share);
    }
    g_queue_free(cpool->conns);
    g_mutex_free(cpool->lock);
    g_slice_free(struct connection_pool, cpool);
    return NULL;
}

void _vmnetfs_transport_pool_free(struct connection_pool *cpool)
{
    struct connection *conn;

    while ((conn = g_queue_pop_head(cpool->conns)) != NULL) {
        conn_free(conn);
    }
    g_queue_free(cpool->conns);
    curl_share_cleanup(cpool->share);
    g_mutex_free(cpool->lock);
    g_free(cpool->user_agent);
    g_slice_free(struct connection_pool, cpool);
}

/* This is not safe if any connections may be active, curl #1215 */
bool _vmnetfs_transport_pool_set_cookie(struct connection_pool *cpool,
        const char *cookie, GError **err)
{
    struct connection *conn;
    char *str;
    bool ret = true;

    conn = conn_get(cpool, err);
    if (conn == NULL) {
        return false;
    }
    str = g_strdup_printf("Set-Cookie:%s", cookie);
    if (curl_easy_setopt(conn->curl, CURLOPT_COOKIELIST, str)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set cookie");
        ret = false;
    }
    g_free(str);
    conn_put(conn);
    return ret;
}

/* Make one attempt to fetch the specified byte range from the URL. */
static bool fetch(struct connection_pool *cpool, const char *url,
        const char *username, const char *password, const char *etag,
        time_t last_modified, void *buf, stream_fn *callback, void *arg,
        uint64_t offset, uint64_t length,
        should_cancel_fn *should_cancel, void *should_cancel_arg,
        GError **err)
{
    struct connection *conn;
    char *range;
    bool ret = false;
    CURLcode code;

    conn = conn_get(cpool, err);
    if (conn == NULL) {
        return false;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_URL, url)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set connection URL");
        goto out;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_USERNAME, username)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set authentication username");
        goto out;
    }
    if (curl_easy_setopt(conn->curl, CURLOPT_PASSWORD, password)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set authentication password");
        goto out;
    }
    range = g_strdup_printf("%"PRIu64"-%"PRIu64, offset, offset + length - 1);
    if (curl_easy_setopt(conn->curl, CURLOPT_RANGE, range)) {
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "Couldn't set transfer byte range");
        g_free(range);
        goto out;
    }
    g_free(range);
    if (buf) {
        conn->buf = buf;
        conn->callback = NULL;
    } else {
        conn->buf = NULL;
        conn->callback = callback;
        conn->arg = arg;
    }
    conn->offset = 0;
    conn->length = length;
    conn->expected_etag = etag;
    conn->expected_last_modified = last_modified;
    conn->should_cancel = should_cancel;
    conn->should_cancel_arg = should_cancel_arg;
    g_assert(conn->err == NULL);

    code = curl_easy_perform(conn->curl);
    if (conn->err) {
        g_propagate_error(err, conn->err);
        conn->err = NULL;
        goto out;
    }
    switch (code) {
    case CURLE_OK:
        if (conn->offset != length) {
            g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                    VMNETFS_TRANSPORT_ERROR_FATAL,
                    "short read from server: %"PRIu64"/%"PRIu64,
                    conn->offset, length);
        }
        ret = true;
        break;
    case CURLE_COULDNT_RESOLVE_PROXY:
    case CURLE_COULDNT_RESOLVE_HOST:
    case CURLE_COULDNT_CONNECT:
    case CURLE_HTTP_RETURNED_ERROR:
    case CURLE_OPERATION_TIMEDOUT:
    case CURLE_GOT_NOTHING:
    case CURLE_SEND_ERROR:
    case CURLE_RECV_ERROR:
    case CURLE_BAD_CONTENT_ENCODING:
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_NETWORK,
                "curl error %d: %s", code, conn->errbuf);
        break;
    case CURLE_ABORTED_BY_CALLBACK:
        g_set_error(err, VMNETFS_IO_ERROR, VMNETFS_IO_ERROR_INTERRUPTED,
                "Operation interrupted");
        break;
    default:
        g_set_error(err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_FATAL,
                "curl error %d: %s", code, conn->errbuf);
        break;
    }
out:
    conn_put(conn);
    return ret;
}

/* Attempt to fetch the specified byte range from the URL, retrying
   several times in case of retryable errors. */
bool _vmnetfs_transport_fetch(struct connection_pool *cpool, const char *url,
        const char *username, const char *password, const char *etag,
        time_t last_modified, void *buf, uint64_t offset, uint64_t length,
        should_cancel_fn *should_cancel, void *should_cancel_arg,
        GError **err)
{
    GError *my_err = NULL;
    int i;

    for (i = 0; i < TRANSPORT_TRIES; i++) {
        if (my_err != NULL) {
            g_clear_error(&my_err);
            sleep(TRANSPORT_RETRY_DELAY);
        }
        if (fetch(cpool, url, username, password, etag, last_modified, buf,
                NULL, NULL, offset, length, should_cancel, should_cancel_arg,
                &my_err)) {
            return true;
        }
        if (!g_error_matches(my_err, VMNETFS_TRANSPORT_ERROR,
                VMNETFS_TRANSPORT_ERROR_NETWORK)) {
            /* fatal error */
            break;
        }
    }
    g_propagate_error(err, my_err);
    return false;
}

/* Attempt to stream the specified URL.  Do not retry. */
bool _vmnetfs_transport_fetch_stream_once(struct connection_pool *cpool,
        const char *url, const char *username, const char *password,
        const char *etag, time_t last_modified, stream_fn *callback,
        void *arg, uint64_t offset, uint64_t length,
        should_cancel_fn *should_cancel, void *should_cancel_arg,
        GError **err)
{
    return fetch(cpool, url, username, password, etag, last_modified, NULL,
            callback, arg, offset, length, should_cancel, should_cancel_arg,
            err);
}
