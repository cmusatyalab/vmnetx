#
# vmnetx.source - Data sources
#
# Copyright (C) 2012-2014 Carnegie Mellon University
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

from cookielib import Cookie
from datetime import datetime
import dateutil.parser
from dateutil.tz import tzutc
import os
import re
import requests
from urllib import pathname2url
from urlparse import urlsplit, urlunsplit

from .util import NeedAuthentication, get_requests_session

class SourceError(Exception):
    '''_HttpSource would like to raise IOError on errors, but ZipFile swallows
    the error message.  So it raises this instead.'''
    pass


class _HttpSource(object):
    '''A read-only file-like object backed by HTTP Range requests.'''

    def __init__(self, url, scheme=None, username=None, password=None,
            buffer_size=64 << 10):
        if scheme == 'Basic':
            self._auth = (username, password)
        elif scheme == 'Digest':
            self._auth = requests.auth.HTTPDigestAuth(username, password)
        elif scheme is None:
            self._auth = None
        else:
            raise ValueError('Unknown authentication scheme')

        self.url = url
        self._offset = 0
        self._closed = False
        self._buffer = ''
        self._buffer_offset = 0
        self._buffer_size = buffer_size
        self._session = get_requests_session()

        # Debugging
        self._last_case = None
        self._last_network = None

        # Perform HEAD request
        try:
            resp = self._session.head(self.url, auth=self._auth)

            # Check for missing credentials
            if resp.status_code == 401:
                # Assumes a single challenge.
                scheme, parameters = resp.headers['WWW-Authenticate'].split(
                        None, 1)
                if scheme != 'Basic' and scheme != 'Digest':
                    raise SourceError('Server requested unknown ' +
                            'authentication scheme: %s' % scheme)
                host = urlsplit(self.url).netloc
                for param in parameters.split(', '):
                    match = re.match('^realm=\"([^"]*)\"$', param)
                    if match:
                        raise NeedAuthentication(host, match.group(1), scheme)
                raise SourceError('Unknown authentication realm')

            # Check for other errors
            resp.raise_for_status()
            # 2xx codes other than 200 are unexpected
            if resp.status_code != 200:
                raise SourceError('Unexpected status code %d' %
                        resp.status_code)

            # Store object length
            try:
                self.length = int(resp.headers['Content-Length'])
            except (IndexError, ValueError):
                raise SourceError('Server did not provide Content-Length')

            # Store validators
            self.etag = self._get_etag(resp)
            self.last_modified = self._get_last_modified(resp)

            # Record cookies
            if hasattr(self._session.cookies, 'extract_cookies'):
                # CookieJar
                self.cookies = tuple(c for c in self._session.cookies)
            else:
                # dict (requests < 0.12.0)
                parsed = urlsplit(self.url)
                self.cookies = tuple(Cookie(version=0,
                        name=name, value='"%s"' % value,
                        port=None, port_specified=False,
                        domain=parsed.netloc, domain_specified=False,
                        domain_initial_dot=False,
                        path=parsed.path, path_specified=True,
                        secure=False, expires=None, discard=True,
                        comment=None, comment_url=None, rest={})
                        for name, value in self._session.cookies.iteritems())
        except requests.exceptions.RequestException, e:
            raise SourceError(str(e))

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    @property
    def name(self):
        return '<%s>' % self.url

    def _get_etag(self, resp):
        etag = resp.headers.get('ETag')
        if etag is None or etag.startswith('W/'):
            return None
        return etag

    def _get_last_modified(self, resp):
        last_modified = resp.headers.get('Last-Modified')
        if last_modified is None:
            return None
        try:
            return dateutil.parser.parse(last_modified)
        except ValueError:
            return None

    def _get(self, offset, size):
        range = '%d-%d' % (offset, offset + size - 1)
        self._last_network = range
        range = 'bytes=' + range

        try:
            resp = self._session.get(self.url, auth=self._auth, headers={
                'Range': range,
            })
            resp.raise_for_status()
            if resp.status_code != 206:
                raise SourceError('Server ignored range request')
            if (self._get_etag(resp) != self.etag or
                    self._get_last_modified(resp) != self.last_modified):
                raise SourceError('Resource changed on server')
            return resp.content
        except requests.exceptions.RequestException, e:
            raise SourceError(str(e))

    def read(self, size=None):
        if self.closed:
            raise SourceError('File is closed')
        if size is None:
            size = self.length - self._offset
        buf_start = self._buffer_offset
        buf_end = self._buffer_offset + len(self._buffer)
        if self._offset >= buf_start and self._offset + size <= buf_end:
            # Case B: Satisfy entirely from buffer
            self._last_case = 'B'
            start = self._offset - self._buffer_offset
            ret = self._buffer[start:start + size]
        elif self._offset >= buf_start and self._offset < buf_end:
            # Case C: Satisfy head from buffer
            # Buffer becomes _buffer_size bytes after requested region
            self._last_case = 'C'
            ret = self._buffer[self._offset - buf_start:]
            remaining = size - len(ret)
            data = self._get(self._offset + len(ret), remaining +
                    self._buffer_size)
            ret += data[:remaining]
            self._buffer = data[remaining:]
            self._buffer_offset = self._offset + size
        elif (self._offset < buf_start and
                self._offset + size >= buf_start):
            # Case D: Satisfy tail from buffer
            # Buffer becomes _buffer_size bytes before requested region
            # plus requested region
            self._last_case = 'D'
            tail = self._buffer[:self._offset + size - buf_start]
            start = max(self._offset - self._buffer_size, 0)
            data = self._get(start, buf_start - start)
            self._buffer = data + tail
            self._buffer_offset = start
            ret = self._buffer[self._offset - start:]
        else:
            # Buffer is useless
            if self._offset + size >= self.length:
                # Case E: Reading at the end of the file.
                # Assume zipfile is probing for the central directory.
                # Buffer becomes _buffer_size bytes before requested
                # region plus requested region
                self._last_case = 'E'
                start = max(self._offset - self._buffer_size, 0)
                self._buffer = self._get(start,
                        self._offset + size - start)
                self._buffer_offset = start
                ret = self._buffer[self._offset - start:]
            else:
                # Case F: Read unrelated to previous reads.
                # Buffer becomes _buffer_size bytes after requested region
                self._last_case = 'F'
                data = self._get(self._offset, size + self._buffer_size)
                ret = data[:size]
                self._buffer = data[size:]
                self._buffer_offset = self._offset + size
        self._offset += len(ret)
        return ret

    def seek(self, offset, whence=0):
        if self.closed:
            raise SourceError('File is closed')
        if whence == 0:
            self._offset = offset
        elif whence == 1:
            self._offset += offset
        elif whence == 2:
            self._offset = self.length + offset
        self._offset = max(self._offset, 0)

    def tell(self):
        if self.closed:
            raise SourceError('File is closed')
        return self._offset

    def close(self):
        self._closed = True
        self._buffer = ''
        self._session.close()

    @property
    def closed(self):
        return self._closed


class _FileSource(file):
    '''An _HttpSource-compatible file-like object for local files.'''

    def __init__(self, url):
        # Process URL
        parsed = urlsplit(url)
        if parsed.scheme != 'file':
            raise ValueError('Invalid URL scheme')
        self.url = url
        self.cookies = ()

        file.__init__(self, parsed.path)

        # Set length
        self.seek(0, 2)
        self.length = self.tell()
        self.seek(0)

        # Set validators.  We could synthesize an ETag from st_dev and
        # st_ino, but this would confuse vmnetfs since libcurl doesn't do
        # the same.
        self.etag = None
        self.last_modified = datetime.fromtimestamp(
                int(os.fstat(self.fileno()).st_mtime), tzutc())


def source_open(url=None, scheme=None, username=None, password=None,
        filename=None):
    if filename:
        url = urlunsplit(('file', '',
                pathname2url(os.path.abspath(filename)), '', ''))
        return _FileSource(url)
    else:
        parsed = urlsplit(url)
        if parsed.scheme == 'http' or parsed.scheme == 'https':
            return _HttpSource(url, scheme=scheme, username=username,
                    password=password)
        elif parsed.scheme == 'file':
            return _FileSource(url)
        else:
            raise ValueError('%s: URLs not supported' % parsed.scheme)


class SourceRange(object):
    def __init__(self, source, offset=0, length=None, load_data=False):
        self.source = source
        self.offset = offset
        self.length = length

        if self.length is None:
            source.seek(0, 2)
            self.length = source.tell()

        if load_data:
            # Eagerly read file data into memory, since _HttpSource likely
            # has it in cache.
            source.seek(self.offset)
            self.data = source.read(self.length)
        else:
            self.data = None

    def write_to_file(self, fh, buf_size=1 << 20):
        if self.data is not None:
            fh.write(self.data)
        else:
            self.source.seek(self.offset)
            count = self.length
            while count > 0:
                cur = min(count, buf_size)
                buf = self.source.read(cur)
                fh.write(buf)
                count -= len(buf)


# We access protected members in assertions.
# pylint: disable=protected-access
def _main():
    from tempfile import mkdtemp
    import subprocess
    import time

    tdir = mkdtemp()
    tfile = os.path.join(tdir, 'test.txt')
    data = ''.join([chr(c) for c in range(ord('a'), ord('z') + 1)] +
                [chr(c) for c in range(ord('A'), ord('Z') + 1)])
    with open(tfile, 'w') as fh:
        fh.write(data)
    with open('/dev/null', 'w') as null:
        proc = subprocess.Popen(['mongoose'], cwd=tdir, stdout=null)
    time.sleep(0.5)  # wait for mongoose to start

    def try_read(fh, size, case, result, new_offset, new_buffer,
            new_buffer_offset, network=None):
        data = fh.read(size)
        assert data == result
        assert fh._offset == new_offset
        assert fh._buffer == new_buffer
        assert fh._buffer_offset == new_buffer_offset

        assert fh._last_case == case
        assert fh._last_network == network
        fh._last_case = None
        fh._last_network = None

    try:
        with _HttpSource('http://localhost:8080/test.txt',
                buffer_size=4) as fh:
            # HEAD
            assert fh.length == len(data)

            # Case F
            fh.seek(12)
            try_read(fh, 6, 'F', data[12:18], 18, data[18:22], 18, '12-21')

            # Case B
            try_read(fh, 2, 'B', data[18:20], 20, data[18:22], 18)

            # Case C
            try_read(fh, 6, 'C', data[20:26], 26, data[26:30], 26, '22-29')

            # Case D
            fh.seek(-4, 1)
            try_read(fh, 6, 'D', data[22:28], 28, data[18:28], 18, '18-25')
            # near beginning of file
            fh.seek(2)
            try_read(fh, 20, 'D', data[2:22], 22, data[0:22], 0, '0-17')

            # Case E
            fh.seek(-5, 2)
            try_read(fh, 5, 'E', data[-5:], len(data), data[-9:],
                    len(data) - 9, '43-51')

            # Zero-length read
            fh.seek(0)
            try_read(fh, 0, 'F', '', 0, data[0:4], 0, '0-3')
            try_read(fh, 0, 'B', '', 0, data[0:4], 0)

        with _HttpSource('http://localhost:8080/test.txt',
                buffer_size=4) as fh:
            # Case E near beginning of file
            fh.seek(2)
            try_read(fh, 100, 'E', data[2:], len(data), data, 0, '0-101')

            # EOF
            try_read(fh, None, 'B', '', len(data), data, 0)

        with _HttpSource('http://localhost:8080/test.txt',
                buffer_size=4) as fh:
            # Change detection
            fh.read(1)
            with open(tfile, 'a') as tf:
                tf.write('xyzzy')
            try:
                fh.read(5)
            except SourceError:
                pass
            else:
                assert False
    finally:
        proc.terminate()
        os.unlink(tfile)
        os.rmdir(tdir)
# pylint: enable=protected-access


if __name__ == '__main__':
    _main()
