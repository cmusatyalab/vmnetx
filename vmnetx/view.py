#
# vmnetx.view - vmnetx GUI
#
# Copyright (C) 2009-2012 Carnegie Mellon University
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

import gobject
import gtk
import gtkvnc
import socket
import sys
import traceback

from vmnetx.status import ImageStatusWidget, LoadProgressWidget

class VNCWidget(gtkvnc.Display):
    def __init__(self, path):
        gtkvnc.Display.__init__(self)
        self._path = path
        self._sock = None

        self.keyboard_grabbed = False
        self.mouse_grabbed = False
        def sa(wid, attr, value):
            setattr(self, attr, value)
        self.connect('vnc-keyboard-grab', sa, 'keyboard_grabbed', True)
        self.connect('vnc-keyboard-ungrab', sa, 'keyboard_grabbed', False)
        self.connect('vnc-pointer-grab', sa, 'mouse_grabbed', True)
        self.connect('vnc-pointer-ungrab', sa, 'mouse_grabbed', False)
        self.set_pointer_grab(True)
        self.set_keyboard_grab(True)

        # Set initial widget size
        self.set_size_request(640, 480)

    def connect_vnc(self):
        try:
            self._sock = socket.socket(socket.AF_UNIX)
            self._sock.connect(self._path)
            self.open_fd(self._sock.fileno())
        except socket.error:
            self.emit('vnc-disconnected')


class StatusBarWidget(gtk.HBox):
    def __init__(self, vnc):
        gtk.HBox.__init__(self, spacing=3)
        self.pack_start(gtk.Label())  # filler

        theme = gtk.icon_theme_get_default()
        def add_icon(name, sensitive):
            icon = gtk.Image()
            icon.set_from_pixbuf(theme.load_icon(name, 24, 0))
            icon.set_sensitive(sensitive)
            self.pack_start(icon, expand=False)
            return icon

        escape_label = gtk.Label('Ctrl-Alt')
        escape_label.set_alignment(0.5, 0.8)
        escape_label.set_padding(3, 0)
        self.pack_start(escape_label, expand=False)

        keyboard_icon = add_icon('input-keyboard', vnc.keyboard_grabbed)
        mouse_icon = add_icon('input-mouse', vnc.mouse_grabbed)
        vnc.connect('vnc-keyboard-grab', self._grabbed, keyboard_icon, True)
        vnc.connect('vnc-keyboard-ungrab', self._grabbed, keyboard_icon, False)
        vnc.connect('vnc-pointer-grab', self._grabbed, mouse_icon, True)
        vnc.connect('vnc-pointer-ungrab', self._grabbed, mouse_icon, False)

    def _grabbed(self, wid, icon, grabbed):
        icon.set_sensitive(grabbed)


class VMWindow(gtk.Window):
    __gsignals__ = {
        'vnc-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, name, path, monitor):
        gtk.Window.__init__(self)
        agrp = VMActionGroup(self)
        agrp.connect('user-quit', lambda _obj: self.emit('user-quit'))

        self.set_title(name)
        self.connect('delete-event',
                lambda _wid, _ev: agrp.get_action('quit').activate() or True)
        self.connect('destroy', self._destroy)

        self._activity = ActivityWindow(name, monitor,
                agrp.get_action('show-activity'))

        box = gtk.VBox()
        self.add(box)

        bar = gtk.Toolbar()
        bar.insert(agrp.get_action('quit').create_tool_item(), -1)
        bar.insert(gtk.SeparatorToolItem(), -1)
        bar.insert(agrp.get_action('show-activity').create_tool_item(), -1)
        box.pack_start(bar, expand=False)

        self._vnc = VNCWidget(path)
        self._vnc.connect('vnc-desktop-resize', self._vnc_resize)
        self._vnc.connect('vnc-disconnected',
                lambda _obj: self.emit('vnc-disconnect'))
        box.pack_start(self._vnc)
        self._vnc.grab_focus()

        statusbar = StatusBarWidget(self._vnc)
        box.pack_end(statusbar, expand=False)

    def connect_vnc(self):
        self._vnc.connect_vnc()

    def show_activity(self, enabled):
        self._activity.set_visible(enabled)

    def _vnc_resize(self, wid, width, height):
        # Resize the window to the minimum allowed by its geometry
        # constraints
        self.resize(1, 1)

    def _destroy(self, _wid):
        self._activity.destroy()
gobject.type_register(VMWindow)


class VMActionGroup(gtk.ActionGroup):
    __gsignals__ = {
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.ActionGroup.__init__(self, 'vmnetx-global')
        self.add_actions((
            ('quit', 'gtk-quit', None, None, 'Quit', self._quit),
        ), user_data=parent)
        self.add_toggle_actions((
            ('show-activity', 'gtk-properties', 'Activity', None,
                    'Show virtual machine activity', self._show_activity),
        ), user_data=parent)

    def _quit(self, _action, parent):
        dlg = gtk.MessageDialog(parent=parent,
                type=gtk.MESSAGE_WARNING,
                buttons=gtk.BUTTONS_OK_CANCEL,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                message_format='Really quit?  All changes will be lost.')
        dlg.set_default_response(gtk.RESPONSE_OK)
        result = dlg.run()
        dlg.destroy()
        if result == gtk.RESPONSE_OK:
            self.emit('user-quit')

    def _show_activity(self, action, parent):
        parent.show_activity(action.get_active())
gobject.type_register(VMActionGroup)


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
    def __init__(self, monitor, parent):
        gtk.Dialog.__init__(self, parent.get_title(), parent, gtk.DIALOG_MODAL)
        self.set_resizable(False)
        self.set_deletable(False)

        box = self.get_content_area()

        label = gtk.Label()
        label.set_markup('<b>Loading...</b>')
        label.set_alignment(0, 0.5)
        label.set_padding(5, 5)
        box.pack_start(label)

        bin = gtk.Alignment()
        bin.add(LoadProgressWidget(monitor))
        bin.set_padding(6, 0, 3, 3)
        box.pack_start(bin)


class ErrorBuffer(object):
    def __init__(self):
        self.exception = str(sys.exc_info()[1])
        self.detail = traceback.format_exc()


class ErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, error=None):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_OK,
                message_format='Fatal Error')
        if error is None:
            error = ErrorBuffer()
        self.format_secondary_text(error.exception)

        content = self.get_message_area()
        expander = gtk.Expander('Details')
        content.pack_start(expander)

        view = gtk.TextView()
        view.get_buffer().set_text(error.detail)
        view.set_editable(False)
        scroller = gtk.ScrolledWindow(view.get_hadjustment(),
                view.get_vadjustment())
        scroller.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        scroller.add(view)
        scroller.set_size_request(600, 150)
        expander.add(scroller)

        self.get_widget_for_response(gtk.RESPONSE_OK).grab_focus()
        content.show_all()
