#
# vmnetx.server.http - HTTP control interface for server
#
# Copyright (C) 2013 Carnegie Mellon University
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

from datetime import datetime
from dateutil.tz import tzutc
from flask import Flask, Response, request, jsonify
from functools import wraps
import json
import logging
from urlparse import urlunsplit

from ..package import Package, NeedAuthentication

_log = logging.getLogger(__name__)

DEFAULT_PORT = 18923


class ServerUnavailableError(Exception):
    pass


class HttpServer(Flask):
    def __init__(self, options, server):
        Flask.__init__(self, __name__)
        self._options = options
        self._server = server
        self.add_url_rule('/create-token', 'create-token',
                self._create_token, methods=['POST'])
        self.add_url_rule('/status', 'status', self._status)

    # We are a decorator, accessing protected members of our own class
    # pylint: disable=E0213,W0212
    def _check_running(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if not self._server.running:
                return Response('Server unavailable', 503)
            try:
                return func(self, *args, **kwargs)
            except ServerUnavailableError:
                return Response('Server unavailable', 503)
        return wrapper
    # pylint: enable=E0213,W0212

    @_check_running
    def _status(self):
        try:
            secret_key = request.headers['X-Secret-Key']
        except KeyError:
            return Response('Missing secret key', 403)
        if secret_key != self._options['secret_key']:
            return Response('Incorrect secret key', 403)

        current_time = datetime.now(tzutc()).isoformat()
        status = self._server.get_status()
        return jsonify(current_time=current_time, status=status)

    @_check_running
    def _create_token(self):
        try:
            secret_key = request.headers['X-Secret-Key']
        except KeyError:
            return Response('Missing secret key', 403)
        if secret_key != self._options['secret_key']:
            return Response('Incorrect secret key', 403)

        try:
            args = json.loads(request.data)
        except ValueError:
            return Response('Invalid request JSON', 400)
        try:
            url = args['url']
        except KeyError:
            return Response('Invalid or missing argument', 400)
        user_ident = args.get('user_ident')

        username = self._options['username']
        password = self._options['password']
        try:
            package = Package(url)
        except NeedAuthentication, e:
            package = Package(url, scheme=e.scheme, username=username,
                    password=password)
        token = self._server.create_token(package, user_ident)

        host = self._options['host']
        port = self._options['port']
        hostname = host
        if port != DEFAULT_PORT:
            hostname += ':%d' % port

        r = urlunsplit(('vmnetx', hostname, '/' + token, '', ''))

        _log.info("Preparing VM at %s with token %s", url, token)
        return jsonify(url=r)
