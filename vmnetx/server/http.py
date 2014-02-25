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

from ..package import Package
from ..source import source_open
from ..util import NeedAuthentication

_log = logging.getLogger(__name__)

DEFAULT_PORT = 18923


class ServerUnavailableError(Exception):
    pass


class HttpServer(Flask):
    def __init__(self, options, server):
        Flask.__init__(self, __name__)
        self._options = options
        self._server = server
        self.add_url_rule('/instance', 'status', self._status)
        self.add_url_rule('/instance', 'create-instance',
                self._create_instance, methods=['POST'])
        self.add_url_rule('/instance/<instance_id>', 'destroy-instance',
                self._destroy_instance, methods=['DELETE'])

    # We are a decorator, accessing protected members of our own class
    # pylint: disable=no-self-argument,protected-access
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
    # pylint: enable=no-self-argument,protected-access

    # We are a decorator, accessing protected members of our own class
    # pylint: disable=no-self-argument,protected-access
    def _need_auth(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                secret_key = request.headers['X-Secret-Key']
            except KeyError:
                return Response('Missing secret key', 403)
            if secret_key != self._options['secret_key']:
                return Response('Incorrect secret key', 403)
            return func(self, *args, **kwargs)
        return wrapper
    # pylint: enable=no-self-argument,protected-access

    @_check_running
    @_need_auth
    def _status(self):
        current_time = datetime.now(tzutc()).isoformat()
        instances = self._server.get_status()
        return jsonify(current_time=current_time, instances=instances)

    @_check_running
    @_need_auth
    def _create_instance(self):
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
            source = source_open(url)
        except NeedAuthentication, e:
            source = source_open(url, scheme=e.scheme, username=username,
                    password=password)
        package = Package(source)
        id, token = self._server.create_instance(package, user_ident)

        host = self._options['host']
        port = self._options['port']
        hostname = host
        if port != DEFAULT_PORT:
            hostname += ':%d' % port

        r = urlunsplit(('vmnetx', hostname, '/' + token, '', ''))

        _log.info("Preparing instance %s at %s", id, url)
        return jsonify(url=r, id=id)

    @_check_running
    @_need_auth
    def _destroy_instance(self, instance_id):
        try:
            self._server.destroy_instance(instance_id)
            return Response('', 204)
        except KeyError:
            return Response('Not found', 404)
