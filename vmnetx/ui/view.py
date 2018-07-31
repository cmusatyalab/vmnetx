#
# vmnetx.ui.view - vmnetx UI widgets
#
# Copyright (C) 2008-2013 Carnegie Mellon University
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
import logging
import math
import sys
import time
import urllib
import cairo

import gi
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('SpiceClientGLib', '2.0')
gi.require_version('SpiceClientGtk', '3.0')
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Gtk
from gi.repository import Pango
from gi.repository import SpiceClientGLib
from gi.repository import SpiceClientGtk

from ..controller import ChunkStateArray
from ..util import ErrorBuffer, BackoffTimer

if sys.platform == 'win32':
    from ..win32 import set_window_progress
else:
    def set_window_progress(_window, _progress):
        pass

class SpiceWidget(Gtk.EventBox):
    __gsignals__ = {
        'viewer-get-fd': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_OBJECT,)),
        'viewer-connect': (GObject.SignalFlags.RUN_LAST, None, ()),
        'viewer-disconnect': (GObject.SignalFlags.RUN_LAST, None, ()),
        'viewer-resize': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_INT, GObject.TYPE_INT)),
        'viewer-keyboard-grab': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_BOOLEAN,)),
        'viewer-mouse-grab': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_BOOLEAN,)),
    }

    BACKOFF_TIMES = (1000, 2000, 5000, 10000)  # ms
    ERROR_EVENTS = set([
        SpiceClientGLib.ChannelEvent.CLOSED,
        SpiceClientGLib.ChannelEvent.ERROR_AUTH,
        SpiceClientGLib.ChannelEvent.ERROR_CONNECT,
        SpiceClientGLib.ChannelEvent.ERROR_IO,
        SpiceClientGLib.ChannelEvent.ERROR_LINK,
        SpiceClientGLib.ChannelEvent.ERROR_TLS,
    ])

    def __init__(self, max_mouse_rate=None):
        Gtk.EventBox.__init__(self)
        self.keyboard_grabbed = False
        self.mouse_grabbed = False

        self._session = None
        self._gtk_session = None
        self._audio = None
        self._display_channel = None
        self._display = None
        self._display_showing = False
        self._accept_next_mouse_event = False
        self._password = None
        self._want_reconnect = False
        self._backoff = BackoffTimer()
        self._last_motion_time = 0
        if max_mouse_rate is not None:
            self._motion_interval = 1000 // max_mouse_rate  # ms
        else:
            self._motion_interval = None

        self._backoff.connect('attempt', self._attempt_connection)
        self.connect('viewer-connect', self._connected)
        self.connect('viewer-disconnect', self._disconnected)
        self.connect('grab-focus', self._grab_focus)

        # SpiceClientGtk < 0.14 (Debian Wheezy) doesn't have the
        # only-downscale property
        self._placeholder = Gtk.EventBox()
        self._placeholder.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA())
        self._placeholder.set_property('can-focus', True)
        self.add(self._placeholder)

    def connect_viewer(self, password):
        '''Start a connection.  Emits viewer-get-fd one or more times; call
        set_fd() with the provided token and the resulting fd.'''
        self._password = password
        self._want_reconnect = True
        self._backoff.reset()
        self._backoff.attempt()

    def _attempt_connection(self, _backoff):
        self._disconnect_viewer()
        self._session = SpiceClientGLib.Session()
        self._session.set_property('password', self._password)
        self._session.set_property('enable-usbredir', False)
        # Ensure clipboard sharing is disabled
        self._gtk_session = SpiceClientGtk.GtkSession.get(self._session)
        self._gtk_session.set_property('auto-clipboard', False)
        GObject.Object.connect(self._session, 'channel-new', self._new_channel)
        self._session.open_fd(-1)

    def _connected(self, _obj):
        self._backoff.reset()

    def _disconnected(self, _obj):
        if self._want_reconnect:
            self._backoff.attempt()

    def disconnect_viewer(self):
        self._want_reconnect = False
        self._backoff.reset()
        self._disconnect_viewer()

    def _grab_focus(self, _wid):
        self.get_child().grab_focus()

    def _new_channel(self, session, channel):
        if session != self._session:
            # Stale channel; ignore
            return
        GObject.Object.connect(channel, 'open-fd', self._request_fd)
        GObject.Object.connect(channel, 'channel-event', self._channel_event)
        type = SpiceClientGLib.Channel.type_to_string(
                channel.get_property('channel-type'))
        if type == 'display':
            # Create the display but don't show it until configured by
            # the server
            GObject.Object.connect(channel, 'display-primary-create',
                    self._display_create)
            self._destroy_display()
            self._display_channel = channel
            self._display = SpiceClientGtk.Display.new(self._session,
                    channel.get_property('channel-id'))
            # Default was False in spice-gtk < 0.14
            self._display.set_property('scaling', True)
            self._display.set_property('only-downscale', True)
            self._display.connect('check-resize', self._size_request)
            self._display.connect('keyboard-grab', self._grab, 'keyboard')
            self._display.connect('mouse-grab', self._grab, 'mouse')
            if self._motion_interval is not None:
                self._display.connect('motion-notify-event', self._motion)
        elif type == 'playback':
            if self._audio is None:
                try:
                    # Enable audio
                    self._audio = SpiceClientGLib.Audio.get(self._session, None)
                except RuntimeError:
                    # No local PulseAudio, etc.
                    pass

    def _display_create(self, channel, _format, _width, _height, _stride,
            _shmid, _imgdata):
        if channel is self._display_channel and not self._display_showing:
            # Display is now configured; show it
            self._display_showing = True
            self.remove(self._placeholder)
            self.add(self._display)
            self.emit('viewer-connect')

    def _request_fd(self, chan, _with_tls):
        try:
            self.emit('viewer-get-fd', chan)
        except TypeError:
            # Channel is invalid because the session was closed while the
            # event was sitting in the queue.
            pass

    def set_fd(self, data, fd):
        '''Pass fd=None if the connection attempt failed.'''
        if fd is None:
            self._disconnect_viewer()
        else:
            data.open_fd(fd)

    def _channel_event(self, channel, event):
        try:
            if channel.get_property('spice-session') != self._session:
                # Stale channel; ignore
                return
        except TypeError:
            # Channel is invalid because the session was closed while the
            # event was sitting in the queue.
            return
        if event in self.ERROR_EVENTS:
            self._disconnect_viewer()

    def _size_request(self, _wid, _req):
        if self._display is not None:
            width, height = self._display.get_size_request()
            if width > 1 and height > 1:
                self.emit('viewer-resize', width, height)

    def _grab(self, _wid, whether, what):
        setattr(self, what + '_grabbed', whether)
        self.emit('viewer-%s-grab' % what, whether)

    def _motion(self, _wid, motion):
        # In server mouse mode, spice-gtk warps the pointer after every
        # motion.  The next motion event it receives (generated by the warp)
        # is only used to set the zero point for the following event.  We
        # therefore have to accept motion events in pairs.
        # pylint: disable=no-else-return
        if self._accept_next_mouse_event:
            # Accept motion
            self._accept_next_mouse_event = False
            return False
        elif motion.time < self._last_motion_time + self._motion_interval:
            # Motion event came too soon; ignore it
            return True
        else:
            # Accept motion
            self._last_motion_time = motion.time
            self._accept_next_mouse_event = True
            return False

    def _destroy_display(self):
        if self._display is not None:
            self._display.destroy()
            self._display = None
            if self.get_children() and self._display_showing:
                self.remove(self._display)
                self.add(self._placeholder)
                self._placeholder.show()
            self._display_showing = False

    def _disconnect_viewer(self):
        if self._session is not None:
            self._destroy_display()
            self._display_channel = None
            self._session.disconnect()
            self._audio = None
            self._gtk_session = None
            self._session = None
            for what in 'keyboard', 'mouse':
                self._grab(None, False, what)
            self.emit('viewer-disconnect')

    def get_pixbuf(self):
        if self._display is None:
            return None
        return self._display.get_pixbuf()
GObject.type_register(SpiceWidget)


class StatusBarWidget(Gtk.HBox):
    def __init__(self, viewer, is_remote=False):
        super(StatusBarWidget, self).__init__(self, spacing=3)
        self._theme = Gtk.IconTheme.get_default()

        self._warnings = Gtk.HBox()
        self.pack_start(self._warnings, False, True, 0)

        self.pack_start(Gtk.Label(), True, True, 0)  # filler

        def add_icon(name, sensitive):
            icon = self._get_icon(name)
            icon.set_sensitive(sensitive)
            self.pack_start(icon, False, True, 0)
            return icon

        escape_label = Gtk.Label(label='Ctrl-Alt')
        escape_label.set_padding(3, 0)
        self.pack_start(escape_label, False, True, 0)

        keyboard_icon = add_icon('input-keyboard', viewer.keyboard_grabbed)
        mouse_icon = add_icon('input-mouse', viewer.mouse_grabbed)
        if is_remote:
            add_icon('network-idle', True)
        else:
            add_icon('computer', True)
        viewer.connect('viewer-keyboard-grab', self._grabbed, keyboard_icon)
        viewer.connect('viewer-mouse-grab', self._grabbed, mouse_icon)

    def _get_icon(self, name):
        icon = Gtk.Image()
        icon.set_from_pixbuf(self._theme.load_icon(name, 24, 0))
        return icon

    def _grabbed(self, _wid, grabbed, icon):
        icon.set_sensitive(grabbed)

    def add_warning(self, icon, message):
        image = self._get_icon(icon)
        image.set_tooltip_markup(message)
        self._warnings.pack_start(image, True, True, 0)
        image.show()
        return image

    def remove_warning(self, warning):
        self._warnings.remove(warning)


class VMWindow(Gtk.Window):
    INITIAL_VIEWER_SIZE = (640, 480)
    MIN_SCALE = 0.25
    SCREEN_SIZE_FUDGE = (-100, -100)

    __gsignals__ = {
        'viewer-get-fd': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_OBJECT,)),
        'viewer-connect': (GObject.SignalFlags.RUN_LAST, None, ()),
        'viewer-disconnect': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-screenshot': (GObject.SignalFlags.RUN_LAST, None,
                (GdkPixbuf.Pixbuf,)),
        'user-restart': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-quit': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, name, disk_stats, disk_chunks, disk_chunk_size,
            max_mouse_rate=None, is_remote=False):
        Gtk.Window.__init__(self)
        self._agrp = VMActionGroup(self)
        for sig in 'user-restart', 'user-quit':
            self._agrp.connect(sig, lambda _obj, s: self.emit(s), sig)
        self._agrp.connect('user-screenshot', self._screenshot)

        self.set_title(name)
        self.connect('window-state-event', self._window_state_changed)
        self.connect('delete-event',
                lambda _wid, _ev:
                self._agrp.get_action('quit').activate() or True)
        self.connect('destroy', self._destroy)

        self._log = LogWindow(name, self._agrp.get_action('show-log'))
        if disk_stats and disk_chunks and disk_chunk_size:
            self._activity = ActivityWindow(name, disk_stats, disk_chunks,
                    disk_chunk_size, self._agrp.get_action('show-activity'))
            self._agrp.set_statistics_available(True)
        else:
            self._activity = None

        self._viewer_width, self._viewer_height = self.INITIAL_VIEWER_SIZE
        self._is_fullscreen = False

        box = Gtk.VBox()
        self.add(box)

        def item(name):
            return self._agrp.get_action(name).create_tool_item()
        tbar = Gtk.Toolbar()
        tbar.set_style(Gtk.ToolbarStyle.ICONS)
        tbar.set_icon_size(Gtk.IconSize.LARGE_TOOLBAR)
        tbar.insert(item('quit'), -1)
        tbar.insert(item('restart'), -1)
        tbar.insert(item('fullscreen'), -1)
        tbar.insert(item('screenshot'), -1)
        tbar.insert(Gtk.SeparatorToolItem(), -1)
        tbar.insert(item('show-activity'), -1)
        tbar.insert(item('show-log'), -1)
        box.pack_start(tbar, False, True, 0)

        self._viewer = SpiceWidget(max_mouse_rate)
        self._viewer.connect('viewer-get-fd', self._viewer_get_fd)
        self._viewer.connect('viewer-resize', self._viewer_resized)
        self._viewer.connect('viewer-connect', self._viewer_connected)
        self._viewer.connect('viewer-disconnect', self._viewer_disconnected)
        box.pack_start(self._viewer, True, True, 0)

        hints = Gdk.Geometry()
        hints.min_width = self._viewer_width
        hints.min_height = self._viewer_height
        self.set_geometry_hints(self._viewer, hints, Gdk.WindowHints.MIN_SIZE)
        self._viewer.grab_focus()

        self._statusbar = StatusBarWidget(self._viewer, is_remote)
        box.pack_end(self._statusbar, False, True, 0)

    def set_vm_running(self, running):
        self._agrp.set_vm_running(running)

    def connect_viewer(self, password):
        self._viewer.connect_viewer(password)

    def set_viewer_fd(self, data, fd):
        self._viewer.set_fd(data, fd)

    def disconnect_viewer(self):
        self._viewer.disconnect_viewer()

    def show_activity(self, enabled):
        if self._activity is None:
            return
        if enabled:
            self._activity.show()
        else:
            self._activity.hide()

    def show_log(self, enabled):
        if enabled:
            self._log.show()
        else:
            self._log.hide()

    def add_warning(self, icon, message):
        return self._statusbar.add_warning(icon, message)

    def remove_warning(self, warning):
        self._statusbar.remove_warning(warning)

    def take_screenshot(self):
        return self._viewer.get_pixbuf()

    def _viewer_get_fd(self, _obj, data):
        self.emit('viewer-get-fd', data)

    def _viewer_connected(self, _obj):
        self._agrp.set_viewer_connected(True)
        self.emit('viewer-connect')

    def _viewer_disconnected(self, _obj):
        self._agrp.set_viewer_connected(False)
        self.emit('viewer-disconnect')

    def _update_window_size_constraints(self):
        # If fullscreen, constrain nothing.
        if self._is_fullscreen:
            self.set_geometry_hints(self._viewer, None, 0)
            return

        # Update window geometry constraints for the guest screen size.
        # We would like to use min_aspect and max_aspect as well, but they
        # seem to apply to the whole window rather than the geometry widget.
        hints = Gdk.Geometry()
        hints.min_width = self._viewer_width * self.MIN_SCALE
        hints.min_height = self._viewer_height * self.MIN_SCALE
        self.set_geometry_hints(self._viewer, hints, Gdk.WindowHints.MIN_SIZE)

        # Resize the window to the largest size that can comfortably fit on
        # the screen, constrained by the maximums.
        screen = self.get_screen()
        monitor = screen.get_monitor_at_window(self.get_window())
        geom = screen.get_monitor_geometry(monitor)
        ow, oh = self.SCREEN_SIZE_FUDGE
        self.resize(max(1, geom.width + ow), max(1, geom.height + oh))

    def _viewer_resized(self, _wid, width, height):
        self._viewer_width = width
        self._viewer_height = height
        self._update_window_size_constraints()

    def _window_state_changed(self, _obj, event):
        if event.changed_mask & Gdk.WindowState.FULLSCREEN:
            self._is_fullscreen = bool(event.new_window_state &
                    Gdk.WindowState.FULLSCREEN)
            self._agrp.get_action('fullscreen').set_active(self._is_fullscreen)
            self._update_window_size_constraints()

    def _screenshot(self, _obj):
        self.emit('user-screenshot', self._viewer.get_pixbuf())

    def _destroy(self, _wid):
        self._log.destroy()
        if self._activity is not None:
            self._activity.destroy()
GObject.type_register(VMWindow)


class VMActionGroup(Gtk.ActionGroup):
    __gsignals__ = {
        'user-screenshot': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-restart': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-quit': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, parent):
        super(VMActionGroup, self).__init__(self, 'vmnetx-global')
        def add_nonstock(name, label, tooltip, icon, handler):
            action = Gtk.Action(name, label, tooltip, None)
            action.set_icon_name(icon)
            action.connect('activate', handler, parent)
            self.add_action(action)
        self.add_actions((
            ('restart', 'gtk-refresh', None, None, 'Restart', self._restart),
            ('quit', 'gtk-quit', None, None, 'Quit', self._quit),
        ), user_data=parent)
        add_nonstock('screenshot', 'Screenshot', 'Take Screenshot',
                'camera-photo', self._screenshot)
        self.add_toggle_actions((
            ('fullscreen', 'gtk-fullscreen', 'Full screen', None,
                    'Toggle full screen', self._fullscreen),
            ('show-activity', 'gtk-properties', 'Activity', None,
                    'Show virtual machine activity', self._show_activity),
            ('show-log', 'gtk-file', 'Log', None,
                    'Show log', self._show_log),
        ), user_data=parent)
        self.set_vm_running(False)
        self.set_viewer_connected(False)
        self.set_statistics_available(False)

    def set_vm_running(self, running):
        for name in ('restart',):
            self.get_action(name).set_sensitive(running)

    def set_viewer_connected(self, connected):
        for name in ('screenshot',):
            self.get_action(name).set_sensitive(connected)

    def set_statistics_available(self, available):
        for name in ('show-activity',):
            self.get_action(name).set_sensitive(available)

    def _confirm(self, parent, signal, message):
        dlg = Gtk.MessageDialog(parent=parent,
                type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                message_format=message)
        dlg.set_default_response(Gtk.ResponseType.OK)
        result = dlg.run()
        dlg.destroy()
        if result == Gtk.ResponseType.OK:
            self.emit(signal)

    def _screenshot(self, _action, _parent):
        self.emit('user-screenshot')

    def _restart(self, _action, parent):
        self._confirm(parent, 'user-restart',
                'Really reboot the guest?  Unsaved data will be lost.')

    def _quit(self, _action, parent):
        self._confirm(parent, 'user-quit',
                'Really quit?  All changes will be lost.')

    def _fullscreen(self, action, parent):
        if action.get_active():
            parent.fullscreen()
        else:
            parent.unfullscreen()

    def _show_activity(self, action, parent):
        parent.show_activity(action.get_active())

    def _show_log(self, action, parent):
        parent.show_log(action.get_active())
GObject.type_register(VMActionGroup)


class _MainLoopCallbackHandler(logging.Handler):
    def __init__(self, callback):
        logging.Handler.__init__(self)
        self._callback = callback

    def emit(self, record):
        GObject.idle_add(self._callback, self.format(record))


class _LogWidget(Gtk.ScrolledWindow):
    FONT = 'monospace 8'
    MIN_HEIGHT = 150

    def __init__(self):
        Gtk.ScrolledWindow.__init__(self)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._textview = Gtk.TextView()
        self._textview.set_editable(False)
        self._textview.set_cursor_visible(False)
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        font = Pango.FontDescription(self.FONT)
        self._textview.modify_font(font)
        width = self._textview.get_pango_context().get_metrics(font,
                None).get_approximate_char_width()
        self._textview.set_size_request(80 * width // Pango.SCALE,
                self.MIN_HEIGHT)
        self.add(self._textview)
        self._handler = _MainLoopCallbackHandler(self._log)
        logging.getLogger().addHandler(self._handler)
        self.connect('destroy', self._destroy)

    def _log(self, line):
        buf = self._textview.get_buffer()
        buf.insert(buf.get_end_iter(), line + '\n')

    def _destroy(self, _wid):
        logging.getLogger().removeHandler(self._handler)


class LogWindow(Gtk.Window):
    def __init__(self, name, hide_action):
        Gtk.Window.__init__(self)
        self.set_title('Log: %s' % name)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        widget = _LogWidget()
        self.add(widget)
        widget.show_all()


class ImageChunkWidget(Gtk.DrawingArea):
    PATTERNS = {
        ChunkStateArray.INVALID: cairo.SolidPattern(0, 0, 0),
        ChunkStateArray.MISSING: cairo.SolidPattern(.35, .35, .35),
        ChunkStateArray.CACHED: cairo.SolidPattern(.63, .63, .63),
        ChunkStateArray.ACCESSED: cairo.SolidPattern(1, 1, 1),
        ChunkStateArray.MODIFIED: cairo.SolidPattern(.45, 0, 0),
        ChunkStateArray.ACCESSED_MODIFIED: cairo.SolidPattern(1, 0, 0),
    }

    TIP = """Red: Accessed and modified this session
White: Accessed this session
Dark red: Modified this session
Light gray: Fetched in previous session
Dark gray: Not present"""

    def __init__(self, chunk_map):
        Gtk.DrawingArea.__init__(self)
        self._map = chunk_map
        self._map_chunk_handler = None
        self._map_resize_handler = None
        self._width_history = [0, 0]
        self.set_tooltip_text(self.TIP)
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)
        self.connect('configure-event', self._configure)
        self.connect('draw', self._draw)

    # pylint doesn't understand allocation.width
    # pylint: disable=no-member
    @property
    def valid_rows(self):
        """Return the number of rows where at least one pixel corresponds
        to a chunk."""
        row_width = self.get_allocated_width()
        return (len(self._map) + row_width - 1) // row_width
    # pylint: enable=no-member

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
            # Gtk.ScrolledWindow is oscillating adding and removing the
            # scroll bar.  This can happen when the viewport's size
            # allocation, with scroll bar, is just above the number of
            # pixels we need for the whole image.  Break the loop by
            # refusing to update our size request.
            return
        self.set_size_request(30, self.valid_rows)

    def _draw(self, _widget, cairo_context):
        # This function is optimized; be careful when changing it.
        # Localize variables for performance (!!)
        patterns = self.PATTERNS
        chunk_states = self._map
        chunks = len(chunk_states)

        _, rect = Gdk.cairo_get_clip_rectangle(cairo_context)
        if rect:
            area_x, area_y, area_height, area_width = (rect.x, rect.y,
                    rect.height, rect.width)
        else:
            area_x, area_y = (0, 0)
            area_height = _widget.get_allocated_height()
            area_width = _widget.get_allocated_width()

        row_width = self.get_allocated_width()
        valid_rows = self.valid_rows
        default_state = ChunkStateArray.MISSING
        invalid_state = ChunkStateArray.INVALID

        set_source = cairo_context.set_source
        rectangle = cairo_context.rectangle
        fill = cairo_context.fill

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
    # pylint: enable=no-member

    # pylint doesn't understand allocation.width
    # pylint: disable=no-member
    def _chunk_changed(self, _map, first, last):
        width = self.get_allocated_width()
        for row in xrange(first // width, last // width + 1):
            row_first = max(width * row, first) % width
            row_last = min(width * (row + 1) - 1, last) % width
            self.queue_draw_area(row_first, row, row_last - row_first + 1, 1)
    # pylint: enable=no-member

    def _image_resized(self, _map, _chunks):
        self.queue_resize_no_redraw()


class ScrollingImageChunkWidget(Gtk.ScrolledWindow):
    def __init__(self, chunk_map):
        Gtk.ScrolledWindow.__init__(self)
        self.set_border_width(2)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add_with_viewport(ImageChunkWidget(chunk_map))
        viewport = self.get_child()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)


class StatWidget(Gtk.EventBox):
    ACTIVITY_FLAG = Gdk.RGBA()
    ACTIVITY_FLAG.parse('#ff4040')

    def __init__(self, stat, chunk_size=None, tooltip=None):
        Gtk.EventBox.__init__(self)
        self._chunk_size = chunk_size
        self._stat = stat
        self._stat_handler = None
        self._label = Gtk.Label(label='--')
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
            self.override_background_color(Gtk.StateFlags.NORMAL, self.ACTIVITY_FLAG)
        else:
            # Clear timer before setting a new one
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(100, self._clear_flag)

    def _clear_flag(self):
        self.override_background_color(Gtk.StateFlags.NORMAL, None)
        self._timer = None
        return False


class MBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value / (1 << 20))


class ChunkMBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value * self._chunk_size / (1 << 20))


class ImageStatGridWidget(Gtk.Grid):
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

    def __init__(self, stats, chunk_size):
        Gtk.Grid.__init__(self)
        self.set_column_spacing(2)
        self.set_column_homogeneous(True)
        self.set_row_spacing(2)
        for row, row_info in enumerate(self.FIELDS):
            caption, fields = row_info
            label = Gtk.Label(label=caption)
            label.set_alignment(0, 0.5)
            self.attach(label, 0, row, 1, 1)
            for col, col_info in enumerate(fields, 1):
                name, cls, tooltip = col_info
                field = cls(stats[name], chunk_size, tooltip)
                self.attach(field, col, row, 1, 1)


class ImageStatusWidget(Gtk.VBox):
    def __init__(self, stats, chunk_map, chunk_size):
        Gtk.VBox.__init__(self)
        self.set_homogeneous(False)
        self.set_spacing(5)

        # Stats table
        frame = Gtk.Frame.new('Statistics')
        frame.add(ImageStatGridWidget(stats, chunk_size))
        self.pack_start(frame, False, True, 0)

        # Chunk bitmap
        frame = Gtk.Frame.new('Chunk bitmap')
        vbox = Gtk.VBox()
        label = Gtk.Label()
        label.set_markup('<span size="small">Chunk size: %d KB</span>' %
                (chunk_size / 1024))
        label.set_alignment(0, 0.5)
        label.set_padding(2, 2)
        vbox.pack_start(label, False, True, 0)
        vbox.pack_start(ScrollingImageChunkWidget(chunk_map), True, True, 0)
        frame.add(vbox)
        self.pack_start(frame, True, True, 0)


class ActivityWindow(Gtk.Window):
    def __init__(self, name, stats, chunk_map, chunk_size, hide_action):
        Gtk.Window.__init__(self)
        self.set_title('Activity: %s' % name)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        status = ImageStatusWidget(stats, chunk_map, chunk_size)
        self.add(status)
        status.show_all()


def humanize(seconds):
    # pylint: disable=no-else-return
    if seconds < 2:
        return "any time now"

    elif seconds < 90:
        return "%d seconds" % (math.ceil(seconds / 5) * 5)

    elif seconds < 4800:
        return "%d minutes" % max(seconds / 60, 2)

    elif seconds < 86400:
        return "%d hours" % max(seconds / 3600, 2)

    else:
        return "more than a day"


class LoadProgressWindow(Gtk.Dialog):
    __gsignals__ = {
        'user-cancel': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, parent):
        Gtk.Dialog.__init__(self, parent.get_title(), parent,
                Gtk.DialogFlags.MODAL, (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL))
        self._parent = parent
        self.set_resizable(False)
        self.connect('response', self._response)

        self._progress = Gtk.ProgressBar()
        self.connect('destroy', self._destroy)

        box = self.get_content_area()
        hbox = Gtk.HBox()

        label = Gtk.Label()
        label.set_markup('<b>Loading...</b>')
        label.set_alignment(0, 0.5)
        label.set_padding(5, 5)
        hbox.pack_start(label, True, True, 0)

        self._eta_label = Gtk.Label()
        self._eta_label.set_alignment(1, 0.5)
        self._eta_label.set_padding(5, 5)
        hbox.pack_start(self._eta_label, True, True, 0)

        box.pack_start(hbox, True, True, 0)

        self._progress.set_property('halign', Gtk.Align.FILL)
        self._progress.set_property('margin', 4)
        box.pack_start(self._progress, True, True, 0)

        # Ensure a minimum width for the progress bar, without affecting
        # its height
        label = Gtk.Label()
        label.set_size_request(300, 0)
        box.pack_start(label, True, True, 0)

        # track time elapsed for ETA estimates
        self.start_time = time.time()

    def _destroy(self, _wid):
        set_window_progress(self._parent, None)

    def progress(self, count, total):
        if total != 0:
            fraction = count / total
        else:
            fraction = 1
        self._progress.set_fraction(fraction)
        set_window_progress(self._parent, fraction)

        elapsed = time.time() - self.start_time
        if count != 0:
            if elapsed >= 5:
                eta = humanize((elapsed / fraction) - elapsed)
            else:
                eta = 'calculating...'
            self._eta_label.set_label('ETA: %s' % eta)

    def _response(self, _wid, _id):
        self.hide()
        set_window_progress(self._parent, None)
        self.emit('user-cancel')
GObject.type_register(LoadProgressWindow)


class PasswordWindow(Gtk.Dialog):
    def __init__(self, site, realm):
        Gtk.Dialog.__init__(self, 'Log in', None, Gtk.DialogFlags.MODAL,
                (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK,
                Gtk.ResponseType.OK))
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)
        self.connect('response', self._response)

        table = Gtk.Table()
        table.set_border_width(5)
        self.get_content_area().pack_start(table, True, True, 0)

        row = 0
        for text in 'Site', 'Realm', 'Username', 'Password':
            label = Gtk.Label(label=text + ':')
            label.set_alignment(1, 0.5)
            table.attach(label, 0, 1, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        self._invalid = Gtk.Label()
        self._invalid.set_markup('<span foreground="red">Invalid username' +
                ' or password.</span>')
        table.attach(self._invalid, 0, 2, row, row + 1, xpadding=5, ypadding=5)
        row += 1

        self._username = Gtk.Entry()
        self._username.connect('activate', self._activate_username)
        self._password = Gtk.Entry()
        self._password.set_visibility(False)
        self._password.set_activates_default(True)
        row = 0
        for text in site, realm:
            label = Gtk.Label(label=text)
            label.set_alignment(0, 0.5)
            table.attach(label, 1, 2, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        for widget in self._username, self._password:
            table.attach(widget, 1, 2, row, row + 1)
            row += 1

        table.show_all()
        self._invalid.hide()

    @property
    def username(self):
        return self._username.get_text()

    @username.setter
    def username(self, value):
        # Side effect: set focus to password field
        self._username.set_text(value)
        self._password.grab_focus()

    @property
    def password(self):
        return self._password.get_text()

    def _activate_username(self, _wid):
        self._password.grab_focus()

    def _set_sensitive(self, sensitive):
        self._username.set_sensitive(sensitive)
        self._password.set_sensitive(sensitive)
        for id in Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL:
            self.set_response_sensitive(id, sensitive)
        self.set_deletable(sensitive)

        if not sensitive:
            self._invalid.hide()

    def _response(self, _wid, resp):
        if resp == Gtk.ResponseType.OK:
            self._set_sensitive(False)

    def fail(self):
        self._set_sensitive(True)
        self._invalid.show()
        self._password.grab_focus()


class SaveMediaWindow(Gtk.FileChooserDialog):
    PREVIEW_SIZE = 250

    def __init__(self, parent, title, filename, preview):
        Gtk.FileChooserDialog.__init__(self, title, parent,
                Gtk.FileChooserAction.SAVE,
                (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE, Gtk.ResponseType.OK))
        self.set_current_name(filename)
        self.set_do_overwrite_confirmation(True)

        w, h = preview.get_width(), preview.get_height()
        scale = min(1, self.PREVIEW_SIZE / w, self.PREVIEW_SIZE / h)
        preview = preview.scale_simple(int(w * scale), int(h * scale),
                GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image()
        image.set_from_pixbuf(preview)
        image.set_padding(5, 5)
        frame = Gtk.Frame.new('Preview')
        frame.add(image)
        image.show()
        self.set_preview_widget(frame)
        self.set_use_preview_label(False)


class UpdateWindow(Gtk.MessageDialog):
    ICON_SIZE = 64

    __gsignals__ = {
        'user-defer-update': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-skip-release': (GObject.SignalFlags.RUN_LAST, None, ()),
        'user-update': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, parent, version, date):
        Gtk.MessageDialog.__init__(self, parent,
                Gtk.DialogFlags.DESTROY_WITH_PARENT, Gtk.MessageType.INFO,
                Gtk.ButtonsType.NONE, 'VMNetX update available')
        theme = Gtk.IconTheme.get_default()
        try:
            icon = theme.load_icon('vmnetx', 256, 0)
            icon = icon.scale_simple(self.ICON_SIZE, self.ICON_SIZE,
                    GdkPixbuf.InterpType.BILINEAR)
        except GLib.GError:
            # VMNetX icon not installed in search path
            icon = theme.load_icon('software-update-available',
                    self.ICON_SIZE, 0)
        self.set_image(Gtk.Image.new_from_pixbuf(icon))
        self.set_title('Update Available')
        datestr = '%s %s, %s' % (
            date.strftime('%B'),
            date.strftime('%d').lstrip('0'),
            date.strftime('%Y')
        )
        self.format_secondary_markup(
                'VMNetX <b>%s</b> was released on <b>%s</b>.' % (
                urllib.quote(version), datestr))
        self.add_buttons('Skip this version', Gtk.ResponseType.REJECT,
                'Remind me later', Gtk.ResponseType.CLOSE,
                'Download update', Gtk.ResponseType.ACCEPT)
        self.set_default_response(Gtk.ResponseType.ACCEPT)
        self.connect('response', self._response)

    def _response(self, _wid, response):
        if response == Gtk.ResponseType.ACCEPT:
            self.emit('user-update')
        elif response == Gtk.ResponseType.REJECT:
            self.emit('user-skip-release')
        else:
            self.emit('user-defer-update')


class ErrorWindow(Gtk.MessageDialog):
    def __init__(self, parent, message):
        Gtk.MessageDialog.__init__(self, parent=parent,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                message_format='Error')
        self.format_secondary_text(message)


class IgnorableErrorWindow(Gtk.MessageDialog):
    def __init__(self, parent, message):
        Gtk.MessageDialog.__init__(self, parent=parent,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.NONE,
                message_format='Error')
        self.format_secondary_text(message)
        self.add_buttons('Continue', Gtk.ResponseType.CANCEL,
                Gtk.STOCK_QUIT, Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)


class FatalErrorWindow(Gtk.MessageDialog):
    def __init__(self, parent, error=None):
        Gtk.MessageDialog.__init__(self, parent=parent,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                message_format='Fatal Error')
        if error is None:
            error = ErrorBuffer()
        self.format_secondary_text(error.exception)
        content = self.get_content_area()

        if error.detail:
            expander = Gtk.Expander.new('Details')
            content.pack_start(expander, True, True, 0)

            view = Gtk.TextView()
            view.get_buffer().set_text(error.detail)
            view.set_editable(False)
            scroller = Gtk.ScrolledWindow()
            view.set_hadjustment(scroller.get_hadjustment())
            view.set_vadjustment(scroller.get_vadjustment())
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.add(view)
            scroller.set_size_request(600, 150)
            expander.add(scroller)

        # RHEL 6 doesn't have MessageDialog.get_widget_for_response()
        self.get_action_area().get_children()[0].grab_focus()
        content.show_all()
