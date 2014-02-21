"""
This is a write-only FTP server. No files can be downloaded from this server.
All received files are sent to a HTTP URL endpoint for processing.

It uses temporary files because Django (our endpoint) does not support chunked
encoding for POST requests. It only supports multipart encoding, and requires a
content-length header. Because the FTP protocol does not send the size of a file
before it begins uploading, the entire file must be received before its size can
be determined. This means that the HTTP request only starts when the file is
100% uploaded.

"""

import errno
import hashlib
import os
import sys
import tempfile

from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS, FilesystemError
from pyftpdlib.handlers import _AsyncChatNewStyle, DTPHandler, FTPHandler, TLS_DTPHandler, TLS_FTPHandler
from pyftpdlib.log import logger
from pyftpdlib.servers import MultiprocessFTPServer

from swiftclient.client import http_connection


class UnexpectedHTTPResponse(Exception):
    pass


class PostFS(AbstractedFS):

    def validpath(self, path):
        """
        Check whether the path belongs to user's home directory.
        Expected argument is a "real" filesystem pathname.

        Pathnames escaping from user's root directory are considered
        not valid.

        Overridden to not access the filesystem at all.

        """
        assert isinstance(path, unicode), path
        root = os.path.normpath(self.root)
        path = os.path.normpath(path)
        if not root.endswith(os.sep):
            root = root + os.sep
        if not path.endswith(os.sep):
            path = path + os.sep
        if path[0:len(root)] == root:
            return True
        return False

    # --- Wrapper methods around open() and tempfile.mkstemp

    def open(self, filename, mode):
        assert isinstance(filename, unicode), filename
        if mode not in ('w', 'wb'):
            raise FilesystemError('open mode %s: filesystem operations are disabled.' % mode)
        return self.post_file(filename)

    def mkstemp(self, suffix='', prefix='', dir=None, mode='wb'):
        raise FilesystemError('mkstemp: filesystem operations are disabled.')

    # --- Wrapper methods around os.* calls

    def chdir(self, path):
        pass

    def mkdir(self, path):
        raise FilesystemError('mkdir: filesystem operations are disabled.')

    def listdir(self, path):
        return []

    def rmdir(self, path):
        raise FilesystemError('rmdir: filesystem operations are disabled.')

    def remove(self, path):
        raise FilesystemError('remove: filesystem operations are disabled.')

    def rename(self, src, dst):
        raise FilesystemError('rename: filesystem operations are disabled.')

    def chmod(self, path, mode):
        raise FilesystemError('chmod: filesystem operations are disabled.')

    def stat(self, path):
        raise FilesystemError('stat: filesystem operations are disabled.')

    def lstat(self, path):
        raise FilesystemError('lstat: filesystem operations are disabled.')

    def readlink(self, path):
        raise FilesystemError('readlink: filesystem operations are disabled.')

    # --- Wrapper methods around os.path.* calls

    def isfile(self, path):
        assert isinstance(path, unicode), path
        return False

    def islink(self, path):
        assert isinstance(path, unicode), path
        return False

    def isdir(self, path):
        assert isinstance(path, unicode), path
        return path == self.root

    def getsize(self, path):
        raise FilesystemError('getsize: filesystem operations are disabled.')

    def getmtime(self, path):
        raise FilesystemError('getmtime: filesystem operations are disabled.')

    def realpath(self, path):
        assert isinstance(path, unicode), path
        return path

    def lexists(self, path):
        assert isinstance(path, unicode), path
        return False

    def get_user_by_uid(self, uid):
        return 'owner'

    def get_group_by_gid(self, gid):
        return 'group'

    # --- Listing utilities

    def get_list_dir(self, path):
        """"
        Return an iterator object that yields a directory listing
        in a form suitable for LIST command.

        """
        assert isinstance(path, unicode), path
        return self.format_list(path, [])


class MultipartPostFile(object):

    url = None

    BOUNDARY = '----------ThIs_Is_tHe_bouNdaRY_$'
    CRLF = '\r\n'

    def __init__(self, name):
        directory, filename = os.path.split(name)
        self.username = os.path.basename(directory)
        self.name = filename
        self.closed = False

    def write(self, data):

        if not hasattr(self, 'request_body'):

            self.request_body = tempfile.SpooledTemporaryFile()

            self.request_body.write('--' + self.BOUNDARY)
            self.request_body.write(self.CRLF)
            self.request_body.write('Content-Disposition: form-data; name="%s"; filename="%s"' % (self.username, self.name))
            self.request_body.write(self.CRLF)
            self.request_body.write('Content-Type: application/octet-stream')
            self.request_body.write(self.CRLF)
            self.request_body.write(self.CRLF)

        self.request_body.write(data)

    def close(self):

        if not self.closed and hasattr(self, 'request_body'):

            try:

                self.request_body.write(self.CRLF)
                self.request_body.write('--' + self.BOUNDARY + '--')
                self.request_body.write(self.CRLF)

                length = self.request_body.tell()

                parsed_url, connection = http_connection(self.url)

                connection.putrequest('POST', parsed_url.path)

                connection.putheader('Content-Type', 'multipart/form-data; boundary=%s' % self.BOUNDARY)
                connection.putheader('Content-Length', str(length))

                connection.endheaders()

                self.request_body.seek(0)
                connection.send(self.request_body)

                response = connection.getresponse()

                if response.status // 100 != 2:
                    raise UnexpectedHTTPResponse('%d: %s' % (response.status, response.reason))

            finally:
                self.closed = True
                self.request_body.close()

#
# Using chunked transfer encoding would be preferable over multipart form
# data, but it is not supported by Django or WSGI or whatever. Keeping it
# here just in case.
#
#class ChunkedPostFile(object):
#
#    url = None
#
#    def __init__(self, name):
#        self.name = name
#        self.closed = False
#
#    def write(self, data):
#
#        if not hasattr(self, 'connection'):
#
#            parsed_url, self.connection = http_connection(self.url)
#
#            self.connection.putrequest('POST', parsed_url.path + '?path=' + self.name)
#
#            self.connection.putheader('Content-Type', 'application/octet-stream')
#            self.connection.putheader('Transfer-Encoding', 'chunked')
#
#            self.connection.endheaders()
#
#        length = len(data)
#        self.connection.send('%X\r\n' % length)
#        self.connection.send(data)
#        self.connection.send('\r\n')
#
#    def close(self):
#
#        if not self.closed and hasattr(self, 'connection'):
#
#            try:
#
#                self.connection.send('0\r\n\r\n')
#
#                response = self.connection.getresponse()
#
#                if response.status // 100 != 2:
#                    raise UnexpectedHTTPResponse('%d: %s' % (response.status, response.reason))
#
#            finally:
#                self.closed = True
#                self.connection.close()


class PostDTPHandlerMixin(object):

    def close(self):
        """
        Extend the class to close the file early, triggering the HTTP upload
        before a response is returned to the FTP client. If the event of an
        unsuccessful HTTP upload, pass the error message on to the FTP client.

        """

        if self.receive and self.transfer_finished and not self._closed:
            if self.file_obj is not None and not self.file_obj.closed:
                try:
                    self.file_obj.close()
                except UnexpectedHTTPResponse as error:
                    self._resp = ('550 Error transferring to HTTP - %s' % error, logger.error)

        return super(PostDTPHandlerMixin, self).close()


class PostDTPHandler(PostDTPHandlerMixin, _AsyncChatNewStyle, DTPHandler):
    pass


class TLS_PostDTPHandler(PostDTPHandlerMixin, TLS_DTPHandler):
    pass


class AccountAuthorizer(DummyAuthorizer):

    hash_funcs = {
        'md5': hashlib.md5,
        'sha1': hashlib.sha1,
        'sha224': hashlib.sha224,
        'sha256': hashlib.sha256,
        'sha384': hashlib.sha384,
        'sha512': hashlib.sha512,
    }

    def __init__(self, accounts):
        super(AccountAuthorizer, self).__init__()
        for name, password in accounts.items():
            self.add_user(name, password)

    def add_user(self, username, password, perm='elw', msg_login='Login successful.', msg_quit='Goodbye.'):

        if self.has_user(username):
            raise ValueError('user %r already exists' % username)

        self._check_permissions(username, perm)

        homedir = username
        if not isinstance(homedir, unicode):
            homedir = homedir.decode('utf8')

        self.user_table[username] = {
            'pwd': str(password),
            'home': homedir,
            'perm': perm,
            'operms': {},
            'msg_login': str(msg_login),
            'msg_quit': str(msg_quit)
        }

    def validate_authentication(self, username, password, handler):
        """Raises AuthenticationFailed if supplied username and
        password don't match the stored credentials, else return
        None.
        """

        msg = 'Authentication failed.'
        if not self.has_user(username):
            if username == 'anonymous':
                msg = 'Anonymous access not allowed.'
            raise AuthenticationFailed(msg)

        if username != 'anonymous':

            stored_password = self.user_table[username]['pwd']
            if stored_password.startswith('plain:'):
                password = 'plain:%s' % password
            else:
                for hash_format, hash_func in self.hash_funcs.items():
                    if stored_password.startswith('%s:' % hash_format):
                        password = '%s:%s' % (
                            hash_format,
                            hash_func(password).hexdigest(),
                        )
                        break
                else:
                    raise AuthenticationFailed('Unexpected password format in configuration file.')

            if password != stored_password:
                raise AuthenticationFailed(msg)


def read_configuration_file(path):
    config = {
        'accounts': {}
    }
    try:
        with open(path) as conf_file:
            print 'Using configuration file %s' % path
            for line in conf_file:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split(':', 1)
                    value = value.strip()
                    if key == 'user':
                        name, password = value.split(':', 1)
                        config['accounts'][name] = password
                    else:
                        if key == 'listen_port':
                            value = int(value)
                        config[key] = value
    except IOError as error:
        if error.errno == errno.ENOENT:
            sys.stderr.write('Cannot find configuration file: %s\n' % path)
            sys.exit(1)
        raise
    else:
        return config


def start_ftp_server(listen_host, listen_port, http_url, accounts, ssl_cert_path=None):

    if ssl_cert_path:

        if not os.path.exists(ssl_cert_path):
            sys.stderr.write('Cannot find SSL certificate file: %s\n' % ssl_cert_path)
            sys.exit(2)

        handler = TLS_FTPHandler
        handler.dtp_handler = TLS_PostDTPHandler

        handler.certfile = ssl_cert_path
        handler.tls_control_required = True
        handler.tls_data_required = True

    else:

        handler = FTPHandler
        handler.dtp_handler = PostDTPHandler

    handler.abstracted_fs = PostFS
    handler.authorizer = AccountAuthorizer(accounts)
    handler.use_sendfile = False

    PostFS.post_file = MultipartPostFile
    PostFS.post_file.url = http_url

    server = MultiprocessFTPServer((listen_host, listen_port), handler)
    server.max_cons = 256
    server.max_cons_per_ip = 5
    server.serve_forever()