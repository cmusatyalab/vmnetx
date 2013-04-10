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

#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <libxml/parser.h>
#include <libxml/xmlschemas.h>
#include <libxml/xpath.h>
#include <libxml/xpathInternals.h>
#include "vmnetfs-private.h"

#define IMAGE_ARG_COUNT 7

static void _image_free(struct vmnetfs_image *img)
{
    _vmnetfs_stream_group_free(img->io_stream);
    _vmnetfs_stat_free(img->bytes_read);
    _vmnetfs_stat_free(img->bytes_written);
    _vmnetfs_stat_free(img->chunk_fetches);
    _vmnetfs_stat_free(img->chunk_dirties);
    _vmnetfs_stat_free(img->io_errors);
    g_free(img->url);
    g_free(img->username);
    g_free(img->password);
    while (img->cookies) {
        g_free(img->cookies->data);
        img->cookies = g_list_delete_link(img->cookies, img->cookies);
    }
    g_free(img->read_base);
    g_free(img->etag);
    g_slice_free(struct vmnetfs_image, img);
}

static void image_free(void *data)
{
    struct vmnetfs_image *img = data;

    _vmnetfs_io_destroy(img);
    _image_free(img);
}

static xmlDocPtr read_arguments(GIOChannel *chan, GError **err)
{
    xmlDocPtr schema_doc = NULL;
    xmlSchemaParserCtxtPtr schema_parser = NULL;
    xmlSchemaPtr schema = NULL;
    xmlSchemaValidCtxtPtr validator = NULL;
    gchar *config_data = NULL;
    gsize config_len;
    gsize bytes_read;
    gsize terminator_pos;
    gchar *endptr;
    xmlDocPtr doc = NULL;
    xmlDocPtr ret = NULL;
    GError *my_err = NULL;

    /* Read schema */
    schema_doc = xmlReadFile(VMNETFS_SCHEMA_PATH, NULL, 0);
    if (schema_doc == NULL) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_SCHEMA,
                "Couldn't parse XML schema document");
        goto out;
    }

    /* Load schema */
    schema_parser = xmlSchemaNewDocParserCtxt(schema_doc);
    g_assert(schema_parser);
    schema = xmlSchemaParse(schema_parser);
    if (schema == NULL) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_SCHEMA,
                "Couldn't parse XML schema");
        goto out;
    }
    validator = xmlSchemaNewValidCtxt(schema);
    g_assert(validator);

    /* Read length of XML document */
    g_io_channel_read_line(chan, &config_data, NULL, &terminator_pos,
            &my_err);
    if (config_data == NULL) {
        if (my_err) {
            g_propagate_error(err, my_err);
        } else {
            g_set_error(err, VMNETFS_CONFIG_ERROR,
                    VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                    "Couldn't read XML document length");
        }
        goto out;
    }
    config_data[terminator_pos] = 0;
    config_len = g_ascii_strtoull(config_data, &endptr, 10);
    if (*config_data == 0 || *endptr != 0) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                "Couldn't parse XML document length");
        goto out;
    }
    g_free(config_data);

    /* Read XML document */
    config_data = g_malloc(config_len);
    if (g_io_channel_read_chars(chan, config_data, config_len, &bytes_read,
            &my_err) != G_IO_STATUS_NORMAL) {
        if (my_err) {
            g_propagate_error(err, my_err);
        } else {
            g_set_error(err, VMNETFS_CONFIG_ERROR,
                    VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                    "Couldn't read XML document");
        }
        goto out;
    }
    if (bytes_read != config_len) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                "Couldn't read entire XML document");
        goto out;
    }

    /* Parse XML document */
    doc = xmlReadMemory(config_data, config_len, NULL, NULL, 0);
    if (doc == NULL) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                "Couldn't parse XML document");
        goto out;
    }

    /* Validate XML document */
    if (xmlSchemaValidateDoc(validator, doc)) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_CONFIG,
                "Config XML did not validate");
        goto out;
    }

    ret = doc;
    doc = NULL;

out:
    if (doc) {
        xmlFreeDoc(doc);
    }
    g_free(config_data);
    if (validator) {
        xmlSchemaFreeValidCtxt(validator);
    }
    if (schema) {
        xmlSchemaFree(schema);
    }
    if (schema_parser) {
        xmlSchemaFreeParserCtxt(schema_parser);
    }
    if (schema_doc) {
        xmlFreeDoc(schema_doc);
    }
    return ret;
}

static xmlXPathContextPtr make_xpath_context(xmlDocPtr doc)
{
    xmlXPathContextPtr ctx;

    ctx = xmlXPathNewContext(doc);
    g_assert(ctx);

    if (xmlXPathRegisterNs(ctx, BAD_CAST "v",
            BAD_CAST "http://olivearchive.org/xmlns/vmnetx/vmnetfs")) {
        g_assert_not_reached();
    }

    return ctx;
}

static char *xpath_get_str(xmlXPathContextPtr ctx, const char *xpath)
{
    xmlXPathObjectPtr result;
    xmlChar *content;
    char *ret;

    result = xmlXPathEval(BAD_CAST xpath, ctx);
    if (result == NULL || result->nodesetval == NULL ||
            result->nodesetval->nodeNr == 0) {
        if (result) {
            xmlXPathFreeObject(result);
        }
        return NULL;
    }
    g_assert(result->nodesetval->nodeNr == 1);
    content = xmlNodeGetContent(result->nodesetval->nodeTab[0]);
    ret = g_strdup((const char *) content);
    xmlFree(content);
    xmlXPathFreeObject(result);
    return ret;
}

/* Returns 0 if the node is not found. */
static uint64_t xpath_get_uint(xmlXPathContextPtr ctx, const char *xpath)
{
    char *str;
    char *endptr;
    uint64_t ret;

    str = xpath_get_str(ctx, xpath);
    if (str == NULL) {
        return 0;
    }
    ret = g_ascii_strtoull(str, &endptr, 10);
    /* Assert if invalid number, since schema validation should have caught
       this */
    g_assert(*str != 0 && *endptr == 0);
    g_free(str);
    return ret;
}

static bool image_add(GHashTable *images, xmlDocPtr args,
        xmlNodePtr image_args, GError **err)
{
    struct vmnetfs_image *img;
    xmlXPathContextPtr ctx;
    xmlXPathObjectPtr obj;
    xmlChar *content;
    int i;

    ctx = make_xpath_context(args);
    ctx->node = image_args;

    img = g_slice_new0(struct vmnetfs_image);
    img->url = xpath_get_str(ctx, "v:origin/v:url/text()");
    img->username = xpath_get_str(ctx,
            "v:origin/v:credentials/v:username/text()");
    img->password = xpath_get_str(ctx,
            "v:origin/v:credentials/v:password/text()");
    img->read_base = xpath_get_str(ctx, "v:cache/v:path/text()");
    img->fetch_offset = xpath_get_uint(ctx, "v:origin/v:offset/text()");
    img->initial_size = xpath_get_uint(ctx, "v:size/text()");
    img->chunk_size = xpath_get_uint(ctx, "v:cache/v:chunk-size/text()");
    img->etag = xpath_get_str(ctx, "v:origin/v:validators/v:etag/text()");
    img->last_modified = xpath_get_uint(ctx,
            "v:origin/v:validators/v:last-modified/text()");

    obj = xmlXPathEval(BAD_CAST "v:origin/v:cookies/v:cookie/text()", ctx);
    for (i = 0; obj && obj->nodesetval && i < obj->nodesetval->nodeNr; i++) {
        content = xmlNodeGetContent(obj->nodesetval->nodeTab[i]);
        img->cookies = g_list_prepend(img->cookies,
                g_strdup((const char *) content));
        xmlFree(content);
    }
    xmlXPathFreeObject(obj);

    img->io_stream = _vmnetfs_stream_group_new(NULL, NULL);
    img->bytes_read = _vmnetfs_stat_new();
    img->bytes_written = _vmnetfs_stat_new();
    img->chunk_fetches = _vmnetfs_stat_new();
    img->chunk_dirties = _vmnetfs_stat_new();
    img->io_errors = _vmnetfs_stat_new();

    if (!_vmnetfs_io_init(img, err)) {
        _image_free(img);
        xmlXPathFreeContext(ctx);
        return false;
    }

    g_hash_table_insert(images, xpath_get_str(ctx, "v:name/text()"), img);
    xmlXPathFreeContext(ctx);
    return true;
}

static void image_close(void *key G_GNUC_UNUSED, void *value,
        void *data G_GNUC_UNUSED)
{
    struct vmnetfs_image *img = value;

    _vmnetfs_io_close(img);
    _vmnetfs_stat_close(img->bytes_read);
    _vmnetfs_stat_close(img->bytes_written);
    _vmnetfs_stat_close(img->chunk_fetches);
    _vmnetfs_stat_close(img->chunk_dirties);
    _vmnetfs_stat_close(img->io_errors);
    _vmnetfs_stream_group_close(img->io_stream);
}

static void *glib_loop_thread(void *data)
{
    struct vmnetfs *fs = data;

    fs->glib_loop = g_main_loop_new(NULL, TRUE);
    g_main_loop_run(fs->glib_loop);
    g_main_loop_unref(fs->glib_loop);
    fs->glib_loop = NULL;
    return NULL;
}

static gboolean read_stdin(GIOChannel *source G_GNUC_UNUSED,
        GIOCondition cond G_GNUC_UNUSED, void *data)
{
    struct vmnetfs *fs = data;
    char buf[16];
    ssize_t ret;

    /* See if stdin has been closed. */
    do {
        ret = read(0, buf, sizeof(buf));
        if (ret == -1 && (errno == EAGAIN || errno == EINTR)) {
            return TRUE;
        }
    } while (ret > 0);

    /* Stop allowing blocking reads on streams (to prevent unmount from
       blocking forever) and lazy-unmount the filesystem.  For complete
       correctness, this should disallow new image opens, wait for existing
       image fds to close, disallow new stream opens and blocking reads,
       then lazy unmount. */
    g_hash_table_foreach(fs->images, image_close, NULL);
    _vmnetfs_log_close(fs->log);
    _vmnetfs_fuse_terminate(fs->fuse);
    return FALSE;
}

static gboolean shutdown_callback(void *data)
{
    struct vmnetfs *fs = data;

    g_main_loop_quit(fs->glib_loop);
    return FALSE;
}

static void child(FILE *pipe)
{
    struct vmnetfs *fs;
    GThread *loop_thread = NULL;
    GIOChannel *chan;
    GIOFlags flags;
    xmlDocPtr args;
    xmlXPathContextPtr xpath;
    xmlXPathObjectPtr obj;
    int i;
    GError *err = NULL;

    /* Initialize */
    if (!g_thread_supported()) {
        g_thread_init(NULL);
    }
    if (!_vmnetfs_transport_init()) {
        fprintf(pipe, "Could not initialize transport\n");
        fclose(pipe);
        return;
    }

    /* Read and validate arguments */
    chan = g_io_channel_unix_new(0);
    args = read_arguments(chan, &err);
    if (args == NULL) {
        fprintf(pipe, "%s\n", err->message);
        g_clear_error(&err);
        fclose(pipe);
        return;
    }

    /* Set up fs */
    fs = g_slice_new0(struct vmnetfs);
    fs->images = g_hash_table_new_full(g_str_hash, g_str_equal, g_free,
            image_free);

    /* Set up images */
    xpath = make_xpath_context(args);
    obj = xmlXPathEval(BAD_CAST "/v:config/v:image", xpath);
    for (i = 0; obj && obj->nodesetval && i < obj->nodesetval->nodeNr; i++) {
        if (!image_add(fs->images, args, obj->nodesetval->nodeTab[i], &err)) {
            fprintf(pipe, "%s\n", err->message);
            xmlXPathFreeObject(obj);
            xmlXPathFreeContext(xpath);
            xmlFreeDoc(args);
            goto out;
        }
    }
    xmlXPathFreeObject(obj);
    xmlXPathFreeContext(xpath);

    /* Free args */
    xmlFreeDoc(args);

    /* Set up logging */
    fs->log = _vmnetfs_log_init();

    /* Set up fuse */
    fs->fuse = _vmnetfs_fuse_new(fs, &err);
    if (err) {
        fprintf(pipe, "%s\n", err->message);
        goto out;
    }

    /* Start main loop thread */
    loop_thread = g_thread_create(glib_loop_thread, fs, TRUE, &err);
    if (err) {
        fprintf(pipe, "%s\n", err->message);
        goto out;
    }

    /* Add watch for stdin being closed */
    flags = g_io_channel_get_flags(chan);
    g_io_channel_set_flags(chan, flags | G_IO_FLAG_NONBLOCK, &err);
    if (err) {
        fprintf(pipe, "%s\n", err->message);
        goto out;
    }
    g_io_add_watch(chan, G_IO_IN | G_IO_ERR | G_IO_HUP | G_IO_NVAL,
            read_stdin, fs);

    /* Started successfully.  Send the mountpoint back to the parent and
       run FUSE event loop until the filesystem is unmounted. */
    fprintf(pipe, "\n%s\n", fs->fuse->mountpoint);
    fclose(pipe);
    pipe = NULL;
    _vmnetfs_fuse_run(fs->fuse);

out:
    /* Shut down */
    if (err != NULL) {
        g_clear_error(&err);
    }
    if (pipe != NULL) {
        fclose(pipe);
    }
    if (loop_thread != NULL) {
        g_idle_add(shutdown_callback, fs);
        g_thread_join(loop_thread);
    }
    _vmnetfs_fuse_free(fs->fuse);
    g_hash_table_destroy(fs->images);
    _vmnetfs_log_destroy(fs->log);
    g_slice_free(struct vmnetfs, fs);
    g_io_channel_unref(chan);
}

static void setsignal(int signum, void (*handler)(int))
{
    const struct sigaction sa = {
        .sa_handler = handler,
        .sa_flags = SA_RESTART,
    };

    sigaction(signum, &sa, NULL);
}

int main(int argc G_GNUC_UNUSED, char **argv G_GNUC_UNUSED)
{
    int pipes[2];
    FILE *pipe_fh;
    pid_t pid;

    setsignal(SIGINT, SIG_IGN);

    if (pipe(pipes)) {
        fprintf(stderr, "Could not create pipes\n");
        return 1;
    }

    pid = fork();
    if (pid) {
        /* Parent */
        char buf[256];
        int status;
        pid_t exited;

        pipe_fh = fdopen(pipes[0], "r");
        close(pipes[1]);

        /* Read possible error status from child */
        buf[0] = 0;
        fgets(buf, sizeof(buf), pipe_fh);
        if (ferror(pipe_fh)) {
            fprintf(stderr, "Error reading status from vmnetfs\n");
            return 1;
        }
        if (buf[0] != 0 && buf[0] != '\n') {
            fprintf(stderr, "%s", buf);
            return 1;
        }

        /* See if it exited */
        exited = waitpid(pid, &status, WNOHANG);
        if (exited == -1) {
            fprintf(stderr, "Error reading exit status from vmnetfs\n");
            return 1;
        } else if (exited && WIFSIGNALED(status)) {
            fprintf(stderr, "vmnetfs died on signal %d\n", WTERMSIG(status));
            return 1;
        } else if (exited) {
            fprintf(stderr, "vmnetfs died with exit status %d\n",
                    WEXITSTATUS(status));
            return 1;
        }

        /* Print mountpoint and exit */
        buf[0] = 0;
        fgets(buf, sizeof(buf), pipe_fh);
        if (ferror(pipe_fh)) {
            fprintf(stderr, "Error reading mountpoint from vmnetfs\n");
            return 1;
        }
        printf("%s", buf);
        return 0;

    } else {
        /* Child */
        pipe_fh = fdopen(pipes[1], "w");
        close(pipes[0]);

        /* Ensure the grandparent doesn't block reading our output */
        close(1);
        close(2);
        open("/dev/null", O_WRONLY);
        open("/dev/null", O_WRONLY);

        child(pipe_fh);
        return 0;
    }
}
