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
#include <sys/wait.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include "vmnetfs-private.h"

#define IMAGE_ARG_COUNT 5

static void _image_free(struct vmnetfs_image *img)
{
    _vmnetfs_stream_group_free(img->io_stream);
    _vmnetfs_stat_free(img->bytes_read);
    _vmnetfs_stat_free(img->bytes_written);
    _vmnetfs_stat_free(img->chunk_fetches);
    _vmnetfs_stat_free(img->chunk_dirties);
    g_free(img->url);
    g_free(img->username);
    g_free(img->password);
    g_free(img->read_base);
    g_slice_free(struct vmnetfs_image, img);
}

static void image_free(struct vmnetfs_image *img)
{
    if (img == NULL) {
        return;
    }
    _vmnetfs_io_destroy(img);
    _image_free(img);
}

static uint64_t parse_uint(const char *str, GError **err)
{
    char *endptr;
    uint64_t ret;

    ret = g_ascii_strtoull(str, &endptr, 10);
    if (*str == 0 || *endptr != 0) {
        g_set_error(err, VMNETFS_CONFIG_ERROR,
                VMNETFS_CONFIG_ERROR_INVALID_ARGUMENT,
                "Invalid integer argument: %s", str);
        return 0;
    }
    return ret;
}

static char *read_line(GIOChannel *chan, GError **err)
{
    char *buf;
    gsize terminator;

    switch (g_io_channel_read_line(chan, &buf, NULL, &terminator, err)) {
    case G_IO_STATUS_ERROR:
        return NULL;
    case G_IO_STATUS_NORMAL:
        buf[terminator] = 0;
        return buf;
    case G_IO_STATUS_EOF:
        g_set_error(err, G_IO_CHANNEL_ERROR, G_IO_CHANNEL_ERROR_IO,
                "Unexpected EOF");
        return NULL;
    case G_IO_STATUS_AGAIN:
        g_set_error(err, G_IO_CHANNEL_ERROR, G_IO_CHANNEL_ERROR_IO,
                "Unexpected EAGAIN");
        return NULL;
    default:
        g_assert_not_reached();
    }
}

static char **get_arguments(GIOChannel *chan, GError **err)
{
    uint64_t n;
    uint64_t count;
    char *str;
    GPtrArray *args;
    GError *my_err = NULL;

    /* Get argument count */
    str = read_line(chan, err);
    if (str == NULL) {
        return NULL;
    }
    count = parse_uint(str, &my_err);
    if (my_err) {
        g_propagate_error(err, my_err);
        return NULL;
    }

    /* Get arguments */
    args = g_ptr_array_new_with_free_func(g_free);
    for (n = 0; n < count; n++) {
        str = read_line(chan, err);
        if (str == NULL) {
            g_ptr_array_free(args, TRUE);
            return NULL;
        }
        g_ptr_array_add(args, str);
    }
    g_ptr_array_add(args, NULL);
    return (char **) g_ptr_array_free(args, FALSE);
}

static struct vmnetfs_image *image_new(char **argv, const char *username,
        const char *password, GError **err)
{
    struct vmnetfs_image *img;
    int arg = 0;
    GError *my_err = NULL;

    const char *url = argv[arg++];
    const char *cache = argv[arg++];
    const uint64_t size = parse_uint(argv[arg++], &my_err);
    if (my_err) {
        g_propagate_error(err, my_err);
        return NULL;
    }
    const uint64_t segment_size = parse_uint(argv[arg++], &my_err);
    if (my_err) {
        g_propagate_error(err, my_err);
        return NULL;
    }
    const uint32_t chunk_size = parse_uint(argv[arg++], &my_err);
    if (my_err) {
        g_propagate_error(err, my_err);
        return NULL;
    }

    img = g_slice_new0(struct vmnetfs_image);
    img->url = g_strdup(url);
    img->username = g_strdup(username);
    img->password = g_strdup(password);
    img->read_base = g_strdup(cache);
    img->initial_size = size;
    img->segment_size = segment_size;
    img->chunk_size = chunk_size;

    img->io_stream = _vmnetfs_stream_group_new(NULL, NULL);
    img->bytes_read = _vmnetfs_stat_new();
    img->bytes_written = _vmnetfs_stat_new();
    img->chunk_fetches = _vmnetfs_stat_new();
    img->chunk_dirties = _vmnetfs_stat_new();

    if (!_vmnetfs_io_init(img, err)) {
        _image_free(img);
        return NULL;
    }

    return img;
}

static void image_close(struct vmnetfs_image *img)
{
    _vmnetfs_io_close(img);
    _vmnetfs_stat_close(img->bytes_read);
    _vmnetfs_stat_close(img->bytes_written);
    _vmnetfs_stat_close(img->chunk_fetches);
    _vmnetfs_stat_close(img->chunk_dirties);
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
    image_close(fs->disk);
    if (fs->memory != NULL) {
        image_close(fs->memory);
    }
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
    int argc;
    char **argv;
    int arg = 0;
    int images;
    char *username;
    char *password;
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

    /* Read arguments */
    chan = g_io_channel_unix_new(0);
    argv = get_arguments(chan, &err);
    if (argv == NULL) {
        fprintf(pipe, "%s\n", err->message);
        g_clear_error(&err);
        fclose(pipe);
        return;
    }

    /* Check argc */
    argc = g_strv_length(argv);
    images = (argc - 2) / IMAGE_ARG_COUNT;
    if (argc % IMAGE_ARG_COUNT != 2 || images < 1 || images > 2) {
        fprintf(pipe, "Incorrect argument count\n");
        fclose(pipe);
        return;
    }

    /* Get initial arguments */
    username = argv[arg++];
    if (!username[0]) {
        username = NULL;
    }
    password = argv[arg++];
    if (!password[0]) {
        password = NULL;
    }

    /* Set up disk */
    fs = g_slice_new0(struct vmnetfs);
    fs->disk = image_new(argv + arg, username, password, &err);
    if (err) {
        fprintf(pipe, "%s\n", err->message);
        goto out;
    }
    arg += IMAGE_ARG_COUNT;

    /* Set up memory */
    if (images > 1) {
        fs->memory = image_new(argv + arg, username, password, &err);
        if (err) {
            fprintf(pipe, "%s\n", err->message);
            goto out;
        }
        arg += IMAGE_ARG_COUNT;
    }

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
        g_io_channel_unref(chan);
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
    image_free(fs->disk);
    image_free(fs->memory);
    g_slice_free(struct vmnetfs, fs);
    g_strfreev(argv);
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
