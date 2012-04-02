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
import os
import socket
import sys
import traceback

from vmnetx.status import ImageStatusWidget, LoadProgressWidget

class VNCWidget(gtkvnc.Display):
    def __init__(self, path):
        gtkvnc.Display.__init__(self)
        self._path = path

        self.keyboard_grabbed = False
        self.mouse_grabbed = False
        def set(_wid, attr, value):
            setattr(self, attr, value)
        self.connect('vnc-keyboard-grab', set, 'keyboard_grabbed', True)
        self.connect('vnc-keyboard-ungrab', set, 'keyboard_grabbed', False)
        self.connect('vnc-pointer-grab', set, 'mouse_grabbed', True)
        self.connect('vnc-pointer-ungrab', set, 'mouse_grabbed', False)
        self.set_pointer_grab(True)
        self.set_keyboard_grab(True)

        # Set initial widget size
        self.set_size_request(640, 480)

    def connect_vnc(self):
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(self._path)
            self.open_fd(os.dup(sock.fileno()))
        except socket.error:
            self.emit('vnc-disconnected')
        finally:
            sock.close()


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

    def _grabbed(self, _wid, icon, grabbed):
        icon.set_sensitive(grabbed)


class VMWindow(gtk.Window):
    __gsignals__ = {
        'vnc-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, name, path, monitor):
        gtk.Window.__init__(self)
        agrp = VMActionGroup(self)
        for sig in 'user-restart', 'user-quit':
            agrp.connect(sig, lambda _obj, s: self.emit(s), sig)

        self.set_title(name)
        self.connect('delete-event',
                lambda _wid, _ev: agrp.get_action('quit').activate() or True)
        self.connect('destroy', self._destroy)

        self._activity = ActivityWindow(name, monitor,
                agrp.get_action('show-activity'))

        box = gtk.VBox()
        self.add(box)

        tbar = gtk.Toolbar()
        tbar.insert(agrp.get_action('quit').create_tool_item(), -1)
        tbar.insert(agrp.get_action('restart').create_tool_item(), -1)
        tbar.insert(gtk.SeparatorToolItem(), -1)
        tbar.insert(agrp.get_action('show-activity').create_tool_item(), -1)
        box.pack_start(tbar, expand=False)

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

    def _vnc_resize(self, _wid, _width, _height):
        # Resize the window to the minimum allowed by its geometry
        # constraints
        self.resize(1, 1)

    def _destroy(self, _wid):
        self._activity.destroy()
gobject.type_register(VMWindow)


class VMActionGroup(gtk.ActionGroup):
    __gsignals__ = {
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.ActionGroup.__init__(self, 'vmnetx-global')
        self.add_actions((
            ('restart', 'gtk-refresh', None, None, 'Restart', self._restart),
            ('quit', 'gtk-quit', None, None, 'Quit', self._quit),
        ), user_data=parent)
        self.add_toggle_actions((
            ('show-activity', 'gtk-properties', 'Activity', None,
                    'Show virtual machine activity', self._show_activity),
        ), user_data=parent)

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

    def _restart(self, _action, parent):
        self._confirm(parent, 'user-restart',
                'Really reboot the guest?  Unsaved data will be lost.')

    def _quit(self, _action, parent):
        self._confirm(parent, 'user-quit',
                'Really quit?  All changes will be lost.')

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
