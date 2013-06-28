#
# vmnetx.status - Display VMNetX disk and memory state
#
# Copyright (C) 2008-2012 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# COPYING.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

from __future__ import division
import cairo
import glib
import gtk
import os

from .monitor import ImageMonitor, ChunkMapMonitor

# pylint chokes on Gtk widgets, #112550
# pylint: disable=R0924

class ImageChunkWidget(gtk.DrawingArea):
    PATTERNS = {
        ChunkMapMonitor.INVALID: cairo.SolidPattern(0, 0, 0),
        ChunkMapMonitor.MISSING: cairo.SolidPattern(.35, .35, .35),
        ChunkMapMonitor.CACHED: cairo.SolidPattern(.63, .63, .63),
        ChunkMapMonitor.ACCESSED: cairo.SolidPattern(1, 1, 1),
        ChunkMapMonitor.MODIFIED: cairo.SolidPattern(.45, 0, 0),
        ChunkMapMonitor.ACCESSED_MODIFIED: cairo.SolidPattern(1, 0, 0),
    }

    TIP = """Red: Accessed and modified this session
White: Accessed this session
Dark red: Modified this session
Light gray: Fetched in previous session
Dark gray: Not present"""

    def __init__(self, image):
        gtk.DrawingArea.__init__(self)
        self._map = image.chunk_map
        self._map_chunk_handler = None
        self._map_resize_handler = None
        self._width_history = [0, 0]
        self.set_tooltip_text(self.TIP)
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)
        self.connect('configure-event', self._configure)
        self.connect('expose-event', self._expose)

    # pylint doesn't understand allocation.width
    # pylint: disable=E1101
    @property
    def valid_rows(self):
        """Return the number of rows where at least one pixel corresponds
        to a chunk."""
        row_width = self.allocation.width
        return (len(self._map.chunks) + row_width - 1) // row_width
    # pylint: enable=E1101

    def _realize(self, _widget):
        self._map_chunk_handler = self._map.connect('chunk-state-changed',
                self._chunk_changed)
        self._map_resize_handler = self._map.connect('image-resized',
                self._image_resized)
        self.queue_resize_no_redraw()

    def _unrealize(self, _widget):
        self._map.disconnect(self._map_chunk_handler)
        self._map.disconnect(self._map_resize_handler)

    def _configure(self, _widget, event):
        self._width_history.append(event.width)
        if (self._width_history.pop(0) == event.width and
                abs(self._width_history[0] - event.width) > 10):
            # We are cycling between two size allocations with significantly
            # different widths, which probably indicates that a parent
            # gtk.ScrolledWindow is oscillating adding and removing the
            # scroll bar.  This can happen when the viewport's size
            # allocation, with scroll bar, is just above the number of
            # pixels we need for the whole image.  Break the loop by
            # refusing to update our size request.
            return
        self.set_size_request(30, self.valid_rows)

    # pylint doesn't understand allocation.width or window.cairo_create()
    # pylint: disable=E1101
    def _expose(self, _widget, event):
        # This function is optimized; be careful when changing it.
        # Localize variables for performance (!!)
        patterns = self.PATTERNS
        chunk_states = self._map.chunks
        chunks = len(chunk_states)
        area_x, area_y, area_height, area_width = (event.area.x,
                event.area.y, event.area.height, event.area.width)
        row_width = self.allocation.width
        valid_rows = self.valid_rows
        default_state = ChunkMapMonitor.MISSING
        invalid_state = ChunkMapMonitor.INVALID

        cr = self.window.cairo_create()
        set_source = cr.set_source
        rectangle = cr.rectangle
        fill = cr.fill

        # Draw MISSING as background color in valid rows
        if valid_rows > area_y:
            set_source(patterns[default_state])
            rectangle(area_x, area_y, area_width,
                    min(area_height, valid_rows - area_y))
            fill()

        # Draw invalid rows
        if valid_rows < area_y + area_height:
            set_source(patterns[invalid_state])
            rectangle(area_x, valid_rows, area_width,
                    area_y + area_height - valid_rows)
            fill()

        # Fill in valid rows.  Avoid drawing MISSING chunks, since those
        # are handled by the background fill.  Combine adjacent pixels
        # of the same color on the same line into a single rectangle.
        last_state = None
        for y in xrange(area_y, min(area_y + area_height, valid_rows)):
            first_x = area_x
            for x in xrange(area_x, area_x + area_width):
                chunk = y * row_width + x
                if chunk < chunks:
                    state = chunk_states[chunk]
                else:
                    state = invalid_state
                if state != last_state:
                    if x > first_x and last_state != default_state:
                        rectangle(first_x, y, x - first_x, 1)
                        fill()
                    set_source(patterns[state])
                    first_x = x
                    last_state = state
            if state != default_state:
                rectangle(first_x, y, area_x + area_width - first_x, 1)
                fill()
    # pylint: enable=E1101

    # pylint doesn't understand allocation.width
    # pylint: disable=E1101
    def _chunk_changed(self, _map, first, last):
        width = self.allocation.width
        for row in xrange(first // width, last // width + 1):
            row_first = max(width * row, first) % width
            row_last = min(width * (row + 1) - 1, last) % width
            self.queue_draw_area(row_first, row, row_last - row_first + 1, 1)
    # pylint: enable=E1101

    def _image_resized(self, _map, _chunks):
        self.queue_resize_no_redraw()


class ScrollingImageChunkWidget(gtk.ScrolledWindow):
    def __init__(self, image):
        gtk.ScrolledWindow.__init__(self)
        self.set_border_width(2)
        self.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self.add_with_viewport(ImageChunkWidget(image))
        viewport = self.get_child()
        viewport.set_shadow_type(gtk.SHADOW_NONE)


class StatWidget(gtk.EventBox):
    ACTIVITY_FLAG = gtk.gdk.Color('#ff4040')

    def __init__(self, image, stat_name, tooltip=None):
        gtk.EventBox.__init__(self)
        self._image = image
        self._stat = image.stats[stat_name]
        self._stat_handler = None
        self._label = gtk.Label('--')
        self._label.set_width_chars(7)
        self._label.set_alignment(1, 0.5)
        self.add(self._label)
        if tooltip:
            self.set_tooltip_text(tooltip)
        self._timer = None
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)

    def _realize(self, _widget):
        self._label.set_text(self._format(self._stat.value))
        self._stat_handler = self._stat.connect('stat-changed', self._changed)

    def _unrealize(self, _widget):
        self._stat.disconnect(self._stat_handler)

    def _format(self, value):
        """Override this in subclasses."""
        return str(value)

    def _changed(self, _stat, _name, value):
        new = self._format(value)
        if self._label.get_text() != new:
            # Avoid unnecessary redraws
            self._label.set_text(new)

        # Update activity flag
        if self._timer is None:
            self.modify_bg(gtk.STATE_NORMAL, self.ACTIVITY_FLAG)
        else:
            # Clear timer before setting a new one
            glib.source_remove(self._timer)
        self._timer = glib.timeout_add(100, self._clear_flag)

    def _clear_flag(self):
        self.modify_bg(gtk.STATE_NORMAL, None)
        self._timer = None
        return False


class MBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value / (1 << 20))


class ChunkMBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value * self._image.chunk_size / (1 << 20))


class ImageStatTableWidget(gtk.Table):
    FIELDS = (
        ('Guest', (
            ('bytes_read', MBStatWidget,
                'Data read by guest OS this session (MB)'),
            ('bytes_written', MBStatWidget,
                'Data written by guest OS this session (MB)'),
        )),
        ('State', (
            ('chunk_fetches', ChunkMBStatWidget,
                'Distinct chunks fetched this session (MB)'),
            ('chunk_dirties', ChunkMBStatWidget,
                'Distinct chunks modified this session (MB)'),
        )),
    )

    def __init__(self, image):
        gtk.Table.__init__(self, len(self.FIELDS), 3, True)
        self.set_border_width(2)
        for row, info in enumerate(self.FIELDS):
            caption, fields = info
            label = gtk.Label(caption)
            label.set_alignment(0, 0.5)
            self.attach(label, 0, 1, row, row + 1, xoptions=gtk.FILL)
            for col, info in enumerate(fields, 1):
                name, cls, tooltip = info
                field = cls(image, name, tooltip)
                self.attach(field, col, col + 1, row, row + 1,
                        xoptions=gtk.FILL, xpadding=3, ypadding=2)


class ImageStatusWidget(gtk.VBox):
    def __init__(self, image):
        gtk.VBox.__init__(self, spacing=5)

        # Stats table
        frame = gtk.Frame('Statistics')
        frame.add(ImageStatTableWidget(image))
        self.pack_start(frame, expand=False)

        # Chunk bitmap
        frame = gtk.Frame('Chunk bitmap')
        vbox = gtk.VBox()
        label = gtk.Label()
        label.set_markup('<span size="small">Chunk size: %d KB</span>' %
                (image.chunk_size / 1024))
        label.set_alignment(0, 0.5)
        label.set_padding(2, 2)
        vbox.pack_start(label, expand=False)
        vbox.pack_start(ScrollingImageChunkWidget(image))
        frame.add(vbox)
        self.pack_start(frame)


class LoadProgressWidget(gtk.ProgressBar):
    PULSE_INITIAL_DELAY = 750  # ms
    PULSE_INTERVAL = 100  # ms

    def __init__(self):
        gtk.ProgressBar.__init__(self)
        self._timer = None
        self.connect('destroy', self._destroy)

    def _destroy(self, _wid):
        if self._timer is not None:
            glib.source_remove(self._timer)
            self._timer = None

    def progress(self, count, total):
        if total != 0:
            fraction = count / total
        else:
            fraction = 1
        self.set_fraction(fraction)
        # qemu can take a long time to finish starting up after it loads the
        # memory image.  Alert the user that something is still happening.
        if fraction == 1 and self._timer is None:
            self._timer = glib.timeout_add(self.PULSE_INITIAL_DELAY,
                    self._timer_tick)
        elif fraction != 1 and self._timer is not None:
            glib.source_remove(self._timer)
            self._timer = None

    def _timer_tick(self):
        self.pulse()
        self._timer = glib.timeout_add(self.PULSE_INTERVAL, self._timer_tick)
        return False

# pylint: enable=R0924
