#
# vmnetx.view - vmnetx GUI
#
# Copyright (C) 2009-2013 Carnegie Mellon University
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
import gobject
import gtk
import gtkvnc
import logging
import pango
import sys
import traceback

# have_spice_viewer is a variable, not a constant
# pylint: disable=C0103
try:
    import SpiceClientGtk
    have_spice_viewer = True
except ImportError:
    have_spice_viewer = False
# pylint: enable=C0103

from vmnetx.status import ImageStatusWidget, LoadProgressWidget

# pylint chokes on Gtk widgets, #112550
# pylint: disable=R0924

class _ViewerWidget(gtk.EventBox):
    __gsignals__ = {
        'viewer-connect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-resize': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_INT, gobject.TYPE_INT)),
        'viewer-keyboard-grab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
        'viewer-mouse-grab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
    }

    def __init__(self, max_mouse_rate=None):
        gtk.EventBox.__init__(self)
        # Must be updated by subclasses
        self.keyboard_grabbed = False
        self.mouse_grabbed = False

        self.connect('grab-focus', self._grab_focus)

        self._last_motion_time = 0
        if max_mouse_rate is not None:
            self._motion_interval = 1000 // max_mouse_rate  # ms
            self.connect('motion-notify-event', self._motion)
        else:
            self._motion_interval = None

    def connect_viewer(self, address, password):
        raise NotImplementedError

    def get_pixbuf(self):
        raise NotImplementedError

    def _reemit(self, _wid, target):
        self.emit(target)

    def _grab_focus(self, _wid):
        self.get_child().grab_focus()

    def _motion(self, _wid, motion):
        if motion.time < self._last_motion_time + self._motion_interval:
            # Motion event came too soon; ignore it
            return True
        else:
            self._last_motion_time = motion.time
            return False


class AspectBin(gtk.Bin):
    # Like an AspectFrame but without the frame.

    __gtype_name__ = 'AspectBin'

    def __init__(self):
        gtk.Bin.__init__(self)
        self.connect('grab-focus', self._grab_focus)

    def _grab_focus(self, _wid):
        child = self.get_child()
        if child is not None:
            child.grab_focus()

    def do_size_request(self, req):
        child = self.get_child()
        if child is not None:
            req.width, req.height = child.size_request()

    def do_size_allocate(self, alloc):
        self.allocation = alloc
        child = self.get_child()
        if child is not None:
            width, height = child.get_child_requisition()
            if width > 0 and height > 0:
                scale = min(1.0, alloc.width / width, alloc.height / height)
            else:
                scale = 1.0
            rect = gtk.gdk.Rectangle()
            rect.width = int(width * scale)
            rect.height = int(height * scale)
            rect.x = alloc.x + max(0, (alloc.width - rect.width) // 2)
            rect.y = alloc.y + max(0, (alloc.height - rect.height) // 2)
            child.size_allocate(rect)


class VNCWidget(_ViewerWidget):
    def __init__(self, max_mouse_rate=None):
        _ViewerWidget.__init__(self, max_mouse_rate)
        aspect = AspectBin()
        self.add(aspect)
        self._vnc = gtkvnc.Display()
        aspect.add(self._vnc)

        self._vnc.connect('vnc-connected', self._reemit, 'viewer-connect')
        self._vnc.connect('vnc-disconnected', self._reemit,
                'viewer-disconnect')
        self._vnc.connect('vnc-desktop-resize', self._resize)
        self._vnc.connect('vnc-keyboard-grab', self._grab, 'keyboard', True)
        self._vnc.connect('vnc-keyboard-ungrab', self._grab, 'keyboard', False)
        self._vnc.connect('vnc-pointer-grab', self._grab, 'mouse', True)
        self._vnc.connect('vnc-pointer-ungrab', self._grab, 'mouse', False)
        self._vnc.set_pointer_grab(True)
        self._vnc.set_keyboard_grab(True)
        self._vnc.set_scaling(True)

    def _resize(self, _wid, width, height):
        self.emit('viewer-resize', width, height)

    def _grab(self, _wid, what, whether):
        setattr(self, what + '_grabbed', whether)
        self.emit('viewer-%s-grab' % what, whether)

    def connect_viewer(self, address, password):
        host, port = address
        self._vnc.set_credential(gtkvnc.CREDENTIAL_PASSWORD, password)
        self._vnc.open_host(host, str(port))

    def get_pixbuf(self):
        return self._vnc.get_pixbuf()
gobject.type_register(VNCWidget)


class SpiceWidget(_ViewerWidget):
    # Defer attribute lookups: SpiceClientGtk is conditionally imported
    _ERROR_EVENTS = (
        'CHANNEL_CLOSED',
        'CHANNEL_ERROR_AUTH',
        'CHANNEL_ERROR_CONNECT',
        'CHANNEL_ERROR_IO',
        'CHANNEL_ERROR_LINK',
        'CHANNEL_ERROR_TLS',
    )

    def __init__(self, max_mouse_rate=None):
        _ViewerWidget.__init__(self, max_mouse_rate)
        self._session = None
        self._gtk_session = None
        self._audio = None
        self._display = None
        self._error_events = set([getattr(SpiceClientGtk, e)
                for e in self._ERROR_EVENTS])

        self._aspect = AspectBin()
        self._placeholder = gtk.EventBox()
        self._placeholder.modify_bg(gtk.STATE_NORMAL, gtk.gdk.Color())
        self._placeholder.set_property('can-focus', True)
        self.add(self._placeholder)

    def connect_viewer(self, address, password):
        assert self._session is None
        host, port = address
        self._session = SpiceClientGtk.Session()
        self._session.set_property('host', host)
        self._session.set_property('port', str(port))
        self._session.set_property('password', password)
        self._session.set_property('enable-usbredir', False)
        # Ensure clipboard sharing is disabled
        self._gtk_session = SpiceClientGtk.spice_gtk_session_get(self._session)
        self._gtk_session.set_property('auto-clipboard', False)
        try:
            # Enable audio
            self._audio = SpiceClientGtk.Audio(self._session)
        except RuntimeError:
            # No local PulseAudio, etc.
            pass
        self._session.connect_object('channel-new', self._new_channel,
                self._session)
        self._session.connect()

    def _new_channel(self, _session, channel):
        channel.connect_object('channel-event', self._channel_event, channel)
        type = SpiceClientGtk.spice_channel_type_to_string(
                channel.get_property('channel-type'))
        if type == 'display':
            self._destroy_display()
            self.remove(self._placeholder)
            self.add(self._aspect)
            self._display = SpiceClientGtk.Display(self._session,
                    channel.get_property('channel-id'))
            # Default was False in spice-gtk < 0.14
            self._display.set_property('scaling', True)
            self._display.connect('size-request', self._size_request)
            self._display.connect('keyboard-grab', self._grab, 'keyboard')
            self._display.connect('mouse-grab', self._grab, 'mouse')
            self._aspect.add(self._display)
            self._aspect.show_all()
            self.emit('viewer-connect')

    def _channel_event(self, _channel, event):
        if event in self._error_events:
            self._disconnect()

    def _size_request(self, _wid, _req):
        if self._display is not None:
            width, height = self._display.get_size_request()
            if width > 1 and height > 1:
                self.emit('viewer-resize', width, height)

    def _grab(self, _wid, whether, what):
        setattr(self, what + '_grabbed', whether)
        self.emit('viewer-%s-grab' % what, whether)

    def _destroy_display(self):
        if self._display is not None:
            self._aspect.remove(self._display)
            self._display.destroy()
            self._display = None
            self.remove(self._aspect)
            self.add(self._placeholder)
            self._placeholder.show()

    def _disconnect(self):
        if self._session is not None:
            self._destroy_display()
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
gobject.type_register(SpiceWidget)


class StatusBarWidget(gtk.HBox):
    def __init__(self, viewer):
        gtk.HBox.__init__(self, spacing=3)
        self._theme = gtk.icon_theme_get_default()

        self._warnings = gtk.HBox()
        self.pack_start(self._warnings, expand=False)

        self.pack_start(gtk.Label())  # filler

        def add_icon(name, sensitive):
            icon = self._get_icon(name)
            icon.set_sensitive(sensitive)
            self.pack_start(icon, expand=False)
            return icon

        escape_label = gtk.Label('Ctrl-Alt')
        escape_label.set_alignment(0.5, 0.8)
        escape_label.set_padding(3, 0)
        self.pack_start(escape_label, expand=False)

        keyboard_icon = add_icon('input-keyboard', viewer.keyboard_grabbed)
        mouse_icon = add_icon('input-mouse', viewer.mouse_grabbed)
        viewer.connect('viewer-keyboard-grab', self._grabbed, keyboard_icon)
        viewer.connect('viewer-mouse-grab', self._grabbed, mouse_icon)

    def _get_icon(self, name):
        icon = gtk.Image()
        icon.set_from_pixbuf(self._theme.load_icon(name, 24, 0))
        return icon

    def _grabbed(self, _wid, grabbed, icon):
        icon.set_sensitive(grabbed)

    def add_warning(self, icon, message):
        image = self._get_icon(icon)
        image.set_tooltip_markup(message)
        self._warnings.pack_start(image)
        image.show()


class VMWindow(gtk.Window):
    INITIAL_VIEWER_SIZE = (640, 480)
    MIN_SCALE = 0.25
    SCREEN_SIZE_FUDGE = (-100, -100)

    __gsignals__ = {
        'viewer-connect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-screenshot': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gtk.gdk.Pixbuf,)),
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, name, disk_monitor, use_spice=True,
            max_mouse_rate=None):
        gtk.Window.__init__(self)
        self._agrp = VMActionGroup(self)
        for sig in 'user-restart', 'user-quit':
            self._agrp.connect(sig, lambda _obj, s: self.emit(s), sig)
        self._agrp.connect('user-screenshot', self._screenshot)

        self.set_title(name)
        self.connect('delete-event',
                lambda _wid, _ev:
                self._agrp.get_action('quit').activate() or True)
        self.connect('destroy', self._destroy)

        self._log = LogWindow(name, self._agrp.get_action('show-log'))
        self._activity = ActivityWindow(name, disk_monitor,
                self._agrp.get_action('show-activity'))

        box = gtk.VBox()
        self.add(box)

        tbar = gtk.Toolbar()
        tbar.insert(self._agrp.get_action('quit').create_tool_item(), -1)
        tbar.insert(self._agrp.get_action('restart').create_tool_item(), -1)
        tbar.insert(self._agrp.get_action('screenshot').create_tool_item(), -1)
        tbar.insert(gtk.SeparatorToolItem(), -1)
        tbar.insert(self._agrp.get_action('show-activity').create_tool_item(),
                -1)
        tbar.insert(self._agrp.get_action('show-log').create_tool_item(), -1)
        box.pack_start(tbar, expand=False)

        if use_spice:
            self._viewer = SpiceWidget(max_mouse_rate)
        else:
            self._viewer = VNCWidget(max_mouse_rate)
        self._viewer.connect('viewer-resize', self._viewer_resized)
        self._viewer.connect('viewer-connect', self._viewer_connected)
        self._viewer.connect('viewer-disconnect', self._viewer_disconnected)
        box.pack_start(self._viewer)
        w, h = self.INITIAL_VIEWER_SIZE
        self.set_geometry_hints(self._viewer, min_width=w, max_width=w,
                min_height=h, max_height=h)
        self._viewer.grab_focus()

        self._statusbar = StatusBarWidget(self._viewer)
        box.pack_end(self._statusbar, expand=False)

    def connect_viewer(self, address, password):
        self._viewer.connect_viewer(address, password)

    def show_activity(self, enabled):
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
        self._statusbar.add_warning(icon, message)

    def take_screenshot(self):
        return self._viewer.get_pixbuf()

    def _viewer_connected(self, _obj):
        self._agrp.set_vm_running(True)
        self.emit('viewer-connect')

    def _viewer_disconnected(self, _obj):
        self._agrp.set_vm_running(False)
        self.emit('viewer-disconnect')

    def _viewer_resized(self, _wid, width, height):
        # Update window geometry constraints for the new guest size.
        # We would like to use min_aspect and max_aspect as well, but they
        # seem to apply to the whole window rather than the geometry widget.
        self.set_geometry_hints(self._viewer,
                min_width=int(width * self.MIN_SCALE),
                min_height=int(height * self.MIN_SCALE),
                max_width=width, max_height=height)

        # Resize the window to the largest size that can comfortably fit on
        # the screen, constrained by the maximums.
        screen = self.get_screen()
        monitor = screen.get_monitor_at_window(self.get_window())
        geom = screen.get_monitor_geometry(monitor)
        ow, oh = self.SCREEN_SIZE_FUDGE
        self.resize(max(1, geom.width + ow), max(1, geom.height + oh))

    def _screenshot(self, _obj):
        self.emit('user-screenshot', self._viewer.get_pixbuf())

    def _destroy(self, _wid):
        self._log.destroy()
        self._activity.destroy()
gobject.type_register(VMWindow)


class VMActionGroup(gtk.ActionGroup):
    __gsignals__ = {
        'user-screenshot': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.ActionGroup.__init__(self, 'vmnetx-global')
        def add_nonstock(name, label, tooltip, icon, handler):
            action = gtk.Action(name, label, tooltip, None)
            action.set_icon_name(icon)
            action.connect('activate', handler, parent)
            self.add_action(action)
        self.add_actions((
            ('restart', 'gtk-refresh', None, None, 'Restart', self._restart),
            ('quit', 'gtk-quit', None, None, 'Quit', self._quit),
        ), user_data=parent)
        add_nonstock('screenshot', 'Screenshot', 'Take Screenshot', 'camera',
                self._screenshot)
        self.add_toggle_actions((
            ('show-activity', 'gtk-properties', 'Activity', None,
                    'Show virtual machine activity', self._show_activity),
            ('show-log', 'gtk-file', 'Log', None,
                    'Show log', self._show_log),
        ), user_data=parent)
        self.set_vm_running(False)

    def set_vm_running(self, running):
        for name in 'screenshot', 'restart':
            self.get_action(name).set_sensitive(running)

    def _confirm(self, parent, signal, message):
        dlg = gtk.MessageDialog(parent=parent,
                type=gtk.MESSAGE_WARNING,
                buttons=gtk.BUTTONS_OK_CANCEL,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                message_format=message)
        dlg.set_default_response(gtk.RESPONSE_OK)
        result = dlg.run()
        dlg.destroy()
        if result == gtk.RESPONSE_OK:
            self.emit(signal)

    def _screenshot(self, _action, _parent):
        self.emit('user-screenshot')

    def _restart(self, _action, parent):
        self._confirm(parent, 'user-restart',
                'Really reboot the guest?  Unsaved data will be lost.')

    def _quit(self, _action, parent):
        self._confirm(parent, 'user-quit',
                'Really quit?  All changes will be lost.')

    def _show_activity(self, action, parent):
        parent.show_activity(action.get_active())

    def _show_log(self, action, parent):
        parent.show_log(action.get_active())
gobject.type_register(VMActionGroup)


class _MainLoopCallbackHandler(logging.Handler):
    def __init__(self, callback):
        logging.Handler.__init__(self)
        self._callback = callback

    def emit(self, record):
        gobject.idle_add(self._callback, self.format(record))


class _LogWidget(gtk.ScrolledWindow):
    FONT = 'monospace 8'
    MIN_HEIGHT = 150

    def __init__(self):
        gtk.ScrolledWindow.__init__(self)
        self.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self._textview = gtk.TextView()
        self._textview.set_editable(False)
        self._textview.set_cursor_visible(False)
        self._textview.set_wrap_mode(gtk.WRAP_WORD_CHAR)
        font = pango.FontDescription(self.FONT)
        self._textview.modify_font(font)
        width = self._textview.get_pango_context().get_metrics(font,
                None).get_approximate_char_width()
        self._textview.set_size_request(80 * width // pango.SCALE,
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


class LogWindow(gtk.Window):
    def __init__(self, name, hide_action):
        gtk.Window.__init__(self)
        self.set_title('Log: %s' % name)
        self.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        widget = _LogWidget()
        self.add(widget)
        widget.show_all()


class ActivityWindow(gtk.Window):
    def __init__(self, name, monitor, hide_action):
        gtk.Window.__init__(self)
        self.set_title('Activity: %s' % name)
        self.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        status = ImageStatusWidget(monitor)
        self.add(status)
        status.show_all()


class LoadProgressWindow(gtk.Dialog):
    __gsignals__ = {
        'user-cancel': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, monitor, parent):
        gtk.Dialog.__init__(self, parent.get_title(), parent, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
        self.set_resizable(False)
        self.connect('response', self._response)

        box = self.get_content_area()

        label = gtk.Label()
        label.set_markup('<b>Loading...</b>')
        label.set_alignment(0, 0.5)
        label.set_padding(5, 5)
        box.pack_start(label)

        bin = gtk.Alignment(xscale=1)
        bin.add(LoadProgressWidget(monitor))
        bin.set_padding(5, 5, 3, 3)
        box.pack_start(bin, expand=True)

        # Ensure a minimum width for the progress bar, without affecting
        # its height
        label = gtk.Label()
        label.set_size_request(300, 0)
        box.pack_start(label)

    def _response(self, _wid, _id):
        self.hide()
        self.emit('user-cancel')
gobject.type_register(LoadProgressWindow)


class PasswordWindow(gtk.Dialog):
    def __init__(self, site, realm):
        gtk.Dialog.__init__(self, 'Log in', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OK,
                gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(False)
        self.connect('response', self._response)

        table = gtk.Table()
        table.set_border_width(5)
        self.get_content_area().pack_start(table)

        row = 0
        for text in 'Site', 'Realm', 'Username', 'Password':
            label = gtk.Label(text + ':')
            label.set_alignment(1, 0.5)
            table.attach(label, 0, 1, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        self._invalid = gtk.Label()
        self._invalid.set_markup('<span foreground="red">Invalid username' +
                ' or password.</span>')
        table.attach(self._invalid, 0, 2, row, row + 1, xpadding=5, ypadding=5)
        row += 1

        self._username = gtk.Entry()
        self._username.connect('activate', self._activate_username)
        self._password = gtk.Entry()
        self._password.set_visibility(False)
        self._password.set_activates_default(True)
        row = 0
        for text in site, realm:
            label = gtk.Label(text)
            label.set_alignment(0, 0.5)
            table.attach(label, 1, 2, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        for widget in self._username, self._password:
            table.attach(widget, 1, 2, row, row + 1)
            row += 1

        table.show_all()
        self._invalid.hide()

    # pylint < 0.25.1 doesn't understand @foo.setter
    # pylint: disable=E0202
    @property
    def username(self):
        return self._username.get_text()
    # pylint: enable=E0202

    # pylint < 0.25.1 doesn't understand @foo.setter
    # pylint: disable=E0102,E0202,E1101
    @username.setter
    def username(self, value):
        # Side effect: set focus to password field
        self._username.set_text(value)
        self._password.grab_focus()
    # pylint: enable=E0102,E0202,E1101

    @property
    def password(self):
        return self._password.get_text()

    def _activate_username(self, _wid):
        self._password.grab_focus()

    def _set_sensitive(self, sensitive):
        self._username.set_sensitive(sensitive)
        self._password.set_sensitive(sensitive)
        for id in gtk.RESPONSE_OK, gtk.RESPONSE_CANCEL:
            self.set_response_sensitive(id, sensitive)
        self.set_deletable(sensitive)

        if not sensitive:
            self._invalid.hide()

    def _response(self, _wid, resp):
        if resp == gtk.RESPONSE_OK:
            self._set_sensitive(False)

    def fail(self):
        self._set_sensitive(True)
        self._invalid.show()
        self._password.grab_focus()


class SaveMediaWindow(gtk.FileChooserDialog):
    PREVIEW_SIZE = 250

    def __init__(self, parent, title, filename, preview):
        gtk.FileChooserDialog.__init__(self, title, parent,
                gtk.FILE_CHOOSER_ACTION_SAVE,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                gtk.STOCK_SAVE, gtk.RESPONSE_OK))
        self.set_current_name(filename)
        self.set_do_overwrite_confirmation(True)

        w, h = preview.get_width(), preview.get_height()
        scale = min(1, self.PREVIEW_SIZE / w, self.PREVIEW_SIZE / h)
        preview = preview.scale_simple(int(w * scale), int(h * scale),
                gtk.gdk.INTERP_BILINEAR)
        image = gtk.Image()
        image.set_from_pixbuf(preview)
        image.set_padding(5, 5)
        frame = gtk.Frame('Preview')
        frame.add(image)
        image.show()
        self.set_preview_widget(frame)
        self.set_use_preview_label(False)


class ErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, message):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_OK,
                message_format='Error')
        self.format_secondary_text(message)


class IgnorableErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, message):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_NONE,
                message_format='Error')
        self.format_secondary_text(message)
        self.add_buttons('Continue', gtk.RESPONSE_CANCEL,
                gtk.STOCK_QUIT, gtk.RESPONSE_OK)
        self.set_default_response(gtk.RESPONSE_OK)


class ErrorBuffer(object):
    def __init__(self):
        exception = sys.exc_info()[1]
        detail = getattr(exception, 'detail', None)
        tb = traceback.format_exc()
        self.exception = str(exception)
        if detail:
            self.detail = detail + '\n\n' + tb
        else:
            self.detail = tb


class FatalErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, error=None):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_OK,
                message_format='Fatal Error')
        if error is None:
            error = ErrorBuffer()
        self.format_secondary_text(error.exception)

        content = self.get_content_area()
        expander = gtk.Expander('Details')
        content.pack_start(expander)

        view = gtk.TextView()
        view.get_buffer().set_text(error.detail)
        view.set_editable(False)
        scroller = gtk.ScrolledWindow()
        view.set_scroll_adjustments(scroller.get_hadjustment(),
                scroller.get_vadjustment())
        scroller.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        scroller.add(view)
        scroller.set_size_request(600, 150)
        expander.add(scroller)

        # RHEL 6 doesn't have MessageDialog.get_widget_for_response()
        self.get_action_area().get_children()[0].grab_focus()
        content.show_all()

# pylint: enable=R0924
