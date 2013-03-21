#
# vmnetx.package - Handling of .nxpk files
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

import requests
import struct
import zipfile

class BadPackageError(Exception):
    pass


class HttpFile(object):
    '''A read-only file-like object backed by HTTP Range requests.'''

    def __init__(self, url, buffer_size=64 << 10):
        self._url = url
        self._offset = 0
        self._length = None  # Unknown
        self._closed = False
        self._buffer = ''
        self._buffer_offset = 0
        self._buffer_size = buffer_size
        self._validators = {}
        self._session = requests.Session()

        # Debugging
        self._last_case = None
        self._last_network = None

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    @property
    def name(self):
        return '<%s>' % self._url

    def _process_response(self, resp):
        resp.raise_for_status()

        if self._length is None:
            try:
                if resp.status_code == 206:
                    content_range = resp.headers.get('Content-Range')
                    if content_range is not None:
                        self._length = int(content_range.split('/')[1])
                elif resp.status_code == 200:
                    self._length = int(resp.headers['Content-Length'])
            except (IndexError, ValueError):
                pass

        for header in 'ETag', 'Last-Modified':
            value = resp.headers.get(header)
            if header not in self._validators:
                if value is not None:
                    self._validators[header] = value
            else:
                if self._validators[header] != value:
                    raise IOError('Resource changed on server')

    def _get(self, offset, size=None):
        if size is None:
            range = '%d-' % offset
        else:
            range = '%d-%d' % (offset, offset + size - 1)
        self._last_network = range
        range = 'bytes=' + range

        try:
            resp = self._session.get(self._url, headers={
                'Range': range,
            })
            self._process_response(resp)
            if resp.status_code != 206:
                raise IOError('Server ignored range request')
            return resp.content
        except requests.exceptions.RequestException, e:
            raise IOError(str(e))

    def read(self, size=None):
        if self.closed:
            raise IOError('File is closed')
        if size is None and self._length is not None:
            size = self._length - self._offset
        if size is None:
            # Case A: this is our first read call (we don't have the
            # length yet) and the caller wants the rest of the file.
            # Buffer becomes _buffer_size bytes before requested region
            # plus requested region
            self._last_case = 'A'
            start = max(self._offset - self._buffer_size, 0)
            self._buffer = self._get(start)
            self._buffer_offset = start
            ret = self._buffer[self._offset - start:]
        else:
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
                # self._length must be valid
                if self._offset + size >= self._length:
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
            raise IOError('File is closed')
        if whence == 0:
            self._offset = offset
        elif whence == 1:
            self._offset += offset
        elif whence == 2:
            self._offset = self.length + offset
        self._offset = max(self._offset, 0)

    def tell(self):
        if self.closed:
            raise IOError('File is closed')
        return self._offset

    @property
    def length(self):
        if self._length is None:
            self._last_network = 'HEAD'
            try:
                resp = self._session.head(self._url)
                self._process_response(resp)
                if self._length is None:
                    raise IOError('No Content-Length in response')
            except requests.exceptions.RequestException, e:
                raise IOError(str(e))
        return self._length

    def close(self):
        self._closed = True
        self._buffer = ''
        self._session.close()

    @property
    def closed(self):
        return self._closed


def get_member_pos(zipf, name):
    '''Return (offset, length) tuple for the named member.'''
    info = zipf.getinfo(name)
    # ZipInfo.extra is the extra field from the central directory file
    # header, which may be different from the extra field in the local
    # file header.  So we need to read the local file header to determine
    # its size.
    header_fmt = '<4s5H3I2H'
    header_len = struct.calcsize(header_fmt)
    zipf.fp.seek(info.header_offset)
    magic, _, flags, compression, _, _, _, size, _, name_len, extra_len = \
            struct.unpack(header_fmt, zipf.fp.read(header_len))
    if magic != zipfile.stringFileHeader:
        raise BadPackageError('Requested member has invalid local header')
    if compression != zipfile.ZIP_STORED:
        raise BadPackageError('Requested member is compressed')
    if flags & 0x1:
        raise BadPackageError('Requested member is encrypted')
    return (info.header_offset + header_len + name_len + extra_len, size)


# We access protected members in assertions.
# pylint is confused by Popen.terminate().
# pylint: disable=W0212,E1101
def _main():
    import os
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

    # fh is a legitimate argument name
    # pylint: disable=C0103
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
    # pylint: enable=C0103

    try:
        with HttpFile('http://localhost:8080/test.txt', buffer_size=4) as fh:
            # Case A
            try_read(fh, None, 'A', data, len(data), data, 0, '0-')

            # EOF
            try_read(fh, None, 'B', '', len(data), data, 0)

        with HttpFile('http://localhost:8080/test.txt', buffer_size=4) as fh:
            # Case A
            fh.seek(42)
            try_read(fh, None, 'A', data[-10:], len(data), data[-14:], 38,
                    '38-')

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

        with HttpFile('http://localhost:8080/test.txt', buffer_size=4) as fh:
            # HEAD
            fh.seek(-10, 2)
            assert fh._last_network == 'HEAD'

            # Case E near beginning of file
            fh.seek(2)
            try_read(fh, 100, 'E', data[2:], len(data), data, 0, '0-101')

        with HttpFile('http://localhost:8080/test.txt', buffer_size=4) as fh:
            # Change detection
            fh.read(1)
            with open(tfile, 'a') as tf:
                tf.write('xyzzy')
            try:
                fh.read(5)
            except IOError:
                pass
            else:
                assert False
    finally:
        proc.terminate()
        os.unlink(tfile)
        os.rmdir(tdir)
# pylint: enable=W0212,E1101


if __name__ == '__main__':
    _main()
