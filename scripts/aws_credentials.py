#!/usr/bin/env python3
"""Linux scoped AWS IoT credential adapter. Python 3.10+ and system libacl; no third-party Python runtime.

Run as a dedicated trusted service, never as a component. Local control uses
SO_PEERCRED; HTTP requests additionally match the kernel's TCP connection UID.
"""
import argparse
import ctypes
import datetime
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import socketserver
import ssl
import stat
import struct
import threading
import time
import urllib.parse

PATH = '/2016-11-01/credentialprovider/'
MAX_FRAME = 16384
# Linux uapi SO_PEERPIDFD (6.5+); Python 3.10 does not expose the name.
SO_PEERPIDFD = getattr(socket, 'SO_PEERPIDFD', 77)


def allow_principals(path, uids, access):
    """POSIX ACL grants to exact UIDs; no group or world access.

    libacl is a platform dependency. Failure to apply an ACL is fatal, never a
    fallback to a globally accessible socket/directory.
    """
    if access not in ('rw-', '--x'):
        raise ValueError('invalid ACL access')
    library = ctypes.CDLL('libacl.so.1', use_errno=True)
    library.acl_from_text.argtypes = [ctypes.c_char_p]
    library.acl_from_text.restype = ctypes.c_void_p
    library.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.acl_set_file.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    owner = 'rw-' if access == 'rw-' else 'rwx'
    entries = [f'u::{owner}', 'g::---', 'o::---', f'm::{access}']
    entries.extend(f'u:{uid}:{access}' for uid in sorted(set(uids)))
    acl = library.acl_from_text(','.join(entries).encode('ascii'))
    if not acl:
        raise OSError(ctypes.get_errno(), 'ACL creation failed')
    try:
        if library.acl_set_file(os.fsencode(path), 0x8000, acl) != 0:  # ACL_TYPE_ACCESS
            raise OSError(ctypes.get_errno(), 'ACL application failed')
    finally:
        library.acl_free(acl)


def private_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute private path required')
    # Do not follow links; protect both contents and replacement of the file.
    for item in [path, *path.parents[:-1]]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise ValueError('unsafe private path')
    if not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise ValueError('private file must be owner-only')
    return path


def tcp_uid(connection):
    """Exact reversed TCP tuple in the same Linux network namespace; fail closed.

    /proc/net/tcp reports the kernel socket UID. Never trust supplied caller
    headers, a PID from a client, or a bearer token by itself.
    """
    local = connection.getpeername()
    remote = connection.getsockname()
    def encoded(endpoint):
        return socket.inet_aton(endpoint[0])[::-1].hex().upper() + f':{endpoint[1]:04X}'
    with open('/proc/net/tcp', encoding='ascii') as source:
        matches = [int(fields[7]) for line in source.readlines()[1:]
                   if len(fields := line.split()) > 9 and fields[1] == encoded(local)
                   and fields[2] == encoded(remote) and fields[3] == '01']
    return matches[0] if len(matches) == 1 else None


class Provider:
    def __init__(self, settings, clock=time.time, monotonic=time.monotonic):
        self.clock, self.monotonic = clock, monotonic
        self.settings = settings
        thing = settings.get('thing_name', '')
        if not isinstance(thing, str) or len(thing) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in thing):
            raise ValueError('invalid thing name')
        endpoint = urllib.parse.urlsplit(settings['endpoint'])
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('explicit HTTPS provider endpoint required')
        if not endpoint.path.startswith('/role-aliases/') or not endpoint.path.endswith('/credentials'):
            raise ValueError('AWS IoT role-alias credentials path required')
        self.endpoint = endpoint
        # Explicit context avoids create_default_context's SSLKEYLOGFILE hook.
        # Even an inherited debug environment must not write TLS session keys.
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.load_verify_locations(cafile=str(private_file(settings['ca'])))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(private_file(settings['certificate']), private_file(settings['key']))
        self.cache = None
        self.expires = 0
        self.retry_at = 0
        self.lock = threading.Lock()

    def credentials(self):
        # One refresh at a time, without allowing queued callers to hold all
        # HTTP workers indefinitely while an upstream is unavailable.
        if not self.lock.acquire(timeout=1):
            return None, 1
        try:
            return self._credentials()
        finally:
            self.lock.release()

    def _credentials(self):
        now = self.clock()
        if self.cache and self.expires - now > 300:
            return self.cache, 0
        if self.monotonic() >= self.retry_at:
            retry = 300
            connection = None
            timer = None
            try:
                connection = http.client.HTTPSConnection(self.endpoint.hostname, self.endpoint.port,
                                                        context=self.context, timeout=5)
                # Bound the whole exchange, including a drip-fed body; socket
                # idle timeouts alone do not impose a total deadline.
                connection.connect()
                upstream_socket = connection.sock
                def expire():
                    try:
                        upstream_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = threading.Timer(5, expire)
                timer.daemon = True
                timer.start()
                # Explicit connection means environment proxies cannot intercept identity.
                headers = {}
                if thing := self.settings.get('thing_name'):
                    headers['x-amzn-iot-thingname'] = thing
                connection.request('GET', self.endpoint.path, headers=headers)
                response = connection.getresponse()
                retry = 120 if 400 <= response.status < 500 else 60 if response.status >= 500 else 300
                if response.status != 200:
                    raise ValueError('upstream rejected')
                raw = response.read(MAX_FRAME + 1)
                if len(raw) > MAX_FRAME:
                    raise ValueError('oversize upstream response')
                value = json.loads(raw)['credentials']
                fields = ('accessKeyId', 'secretAccessKey', 'sessionToken', 'expiration')
                if any(not isinstance(value.get(k), str) or not 0 < len(value[k]) <= 8192 for k in fields):
                    raise ValueError('invalid upstream credentials')
                expiration = datetime.datetime.fromisoformat(value['expiration'].replace('Z', '+00:00'))
                if expiration.tzinfo is None or not now < expiration.timestamp() <= now + 43200:
                    raise ValueError('invalid credential expiry')
                self.cache = dict(zip(('AccessKeyId', 'SecretAccessKey', 'Token', 'Expiration'),
                                      (value[k] for k in fields)))
                # The Rust SDK requires UTC Z, rather than a +00:00 offset.
                self.cache['Expiration'] = expiration.astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
                self.expires = expiration.timestamp()
                self.retry_at = 0
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                self.retry_at = self.monotonic() + retry
            finally:
                if timer:
                    timer.cancel()
                if connection:
                    connection.close()
        if self.cache and self.expires > self.clock():
            return self.cache, 0
        self.cache = None
        return None, max(1, int(self.retry_at - self.monotonic()))


class Policy:
    def __init__(self, settings, clock=time.monotonic):
        self.clock = clock
        self.ttl = settings.get('token_ttl_seconds', 3600)
        if type(self.ttl) is not int or not 1 <= self.ttl <= 86400:
            raise ValueError('token TTL must be 1..86400 seconds')
        self.principals = settings.get('components', {})
        grants = settings.get('grants', [])
        if not isinstance(self.principals, dict) or not isinstance(grants, list) or any(not isinstance(g, str) for g in grants):
            raise ValueError('invalid principals/grants')
        self.grants = set(grants)
        if not self.grants <= self.principals.keys() or len(self.principals) > 64:
            raise ValueError('invalid grants')
        if any(not isinstance(name, str) or not 1 <= len(name) <= 128
                or any(not (c.isascii() and (c.isalnum() or c in '._-')) for c in name) for name in self.principals):
            raise ValueError('invalid component names')
        daemon_uid = settings.get('daemon_uid', os.geteuid())
        if type(daemon_uid) is not int or not 0 <= daemon_uid < 2**32:
            raise ValueError('invalid daemon UID')
        uids = list(self.principals.values())
        if any(type(uid) is not int or not 0 < uid < 2**32 or uid in (os.geteuid(), daemon_uid) for uid in uids) or len(set(uids)) != len(uids):
            raise ValueError('distinct non-root/non-broker component UIDs required')
        self.active = {}
        self.lock = threading.Lock()

    def revoke(self, name):
        previous = self.active.pop(name, None)
        if previous:
            os.close(previous[2])

    def control(self, uid, peerfd, request, uri):
        with self.lock:
            if set(request) != {'version', 'operation', 'component'} or type(request['version']) is not int or request['version'] != 1:
                raise ValueError('invalid request')
            name, operation = request['component'], request['operation']
            if name not in self.principals:
                raise ValueError('unknown component')
            if operation in ('grant', 'revoke'):
                if uid != os.geteuid():
                    raise ValueError('administrator only')
                self.revoke(name)
                if operation == 'grant':
                    self.grants.add(name)
                else:
                    self.grants.discard(name)
                return {}
            if self.principals[name] != uid:
                raise ValueError('wrong caller')
            if operation == 'stop':
                self.revoke(name)
                return {}
            if operation != 'start':
                raise ValueError('unknown operation')
            self.revoke(name)
            if name not in self.grants:
                return {'env': {}}
            token = secrets.token_urlsafe(32)
            # Kernel peer task handle avoids PID-reuse and revokes on wrapper/exec exit.
            if select.select([peerfd], [], [], 0)[0]:
                raise ValueError('caller exited')
            fd = os.dup(peerfd)
            self.active[name] = (token, self.clock() + self.ttl, fd)
            return {'env': {'AWS_CONTAINER_CREDENTIALS_FULL_URI': uri,
                            'AWS_CONTAINER_AUTHORIZATION_TOKEN': token}}

    def authorize(self, uid, token):
        with self.lock:
            for name, (expected, deadline, fd) in list(self.active.items()):
                if self.clock() >= deadline or select.select([fd], [], [], 0)[0] or name not in self.grants:
                    self.revoke(name)
                elif uid == self.principals[name] and isinstance(token, str) and hmac.compare_digest(token, expected):
                    return True
            return False

    def close(self):
        with self.lock:
            for name in list(self.active):
                self.revoke(name)


class BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = True
    def gates(self, request):
        return [self.slots]

    def process_request(self, request, address):
        held = []
        for gate in self.gates(request):
            if not gate.acquire(blocking=False):
                for acquired in held:
                    acquired.release()
                request.close()
                return
            held.append(gate)
        if not held:
            request.close()
            return
        self.held[request] = held
        try:
            super().process_request(request, address)
        except BaseException:
            self.held.pop(request)
            for gate in held:
                gate.release()
            raise

    def process_request_thread(self, request, address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = threading.Timer(15, expire)
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, address)
        finally:
            deadline.cancel()
            for gate in self.held.pop(request):
                gate.release()

    def handle_error(self, request, address):
        pass  # Request/transport diagnostics must never disclose credentials.


class HttpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        code, value, retry = 400, {}, 0
        try:
            line = self.rfile.readline(4097)
            if len(line) > 4096:
                raise ValueError('oversize request')
            method, path, version = line.decode('ascii').strip().split(' ')
            headers = {}
            size = len(line)
            while True:
                line = self.rfile.readline(4097)
                size += len(line)
                if size > MAX_FRAME or not line:
                    raise ValueError('invalid headers')
                if line == b'\r\n':
                    break
                key, value = line.decode('ascii').rstrip('\r\n').split(':', 1)
                key = key.lower()
                if key in headers:
                    raise ValueError('duplicate header')
                headers[key] = value.strip()
            if version not in ('HTTP/1.0', 'HTTP/1.1') or 'transfer-encoding' in headers or headers.get('content-length', '0') != '0':
                raise ValueError('invalid framing')
            if method != 'GET':
                code = 405
            elif path != PATH:
                code = 404
            elif not self.server.policy.authorize(tcp_uid(self.request), headers.get('authorization')):
                code = 401
            else:
                value, retry = self.server.provider.credentials()
                # Recheck after the upstream wait: expiry/revocation must win.
                if not self.server.policy.authorize(tcp_uid(self.request), headers.get('authorization')):
                    code, value = 401, {}
                else:
                    code = 200 if value else 503
        except (OSError, ValueError, TypeError):
            code, value = 400, {}
        body = json.dumps(value if code == 200 else {'error': 'credentials_unavailable'}).encode()
        response = (f'HTTP/1.1 {code} Response\r\nContent-Type: application/json\r\n'
                    f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n')
        if retry:
            response += f'Retry-After: {retry}\r\n'
        self.wfile.write(response.encode() + b'\r\n' + body)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        peerfd = None
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            peerfd = self.request.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
            raw = self.rfile.readline(MAX_FRAME + 1)
            if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                raise ValueError('invalid frame')
            data = self.server.policy.control(uid, peerfd, json.loads(raw), self.server.uri)
            response = {'ok': True, 'data': data}
        except (OSError, ValueError, KeyError, TypeError):
            response = {'ok': False, 'error': 'request_failed'}
        finally:
            if peerfd is not None:
                os.close(peerfd)
        self.wfile.write(json.dumps(response).encode() + b'\n')


class HttpServer(BoundedServer, socketserver.TCPServer):
    pass


class ControlServer(BoundedServer, socketserver.UnixStreamServer):
    def gates(self, request):
        _, uid, _ = struct.unpack('3i', request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid == os.geteuid():
            return [self.admin_slots]
        if uid not in self.principal_slots:
            return []
        return [self.slots, self.principal_slots[uid]]


class Adapter:
    def __init__(self, settings, clock=time.time, monotonic=time.monotonic):
        allowed = {'socket', 'endpoint', 'ca', 'certificate', 'key', 'thing_name', 'components', 'grants', 'token_ttl_seconds', 'daemon_uid'}
        if set(settings) - allowed:
            raise ValueError('unknown credential settings')
        if 'daemon_uid' not in settings:
            raise ValueError('explicit SuiFleet daemon UID required')
        self.policy = Policy(settings, monotonic)
        self.provider = Provider(settings, clock, monotonic)
        path = Path(settings['socket'])
        parent = path.parent.lstat()
        if not path.is_absolute() or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise ValueError('unsafe socket directory')
        # bind refuses existing files, including stale sockets and symlinks.
        for ancestor in path.parents:
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError('unsafe socket ancestor')
        self.http = HttpServer(('127.0.0.1', 0), HttpHandler)
        try:
            self.control = ControlServer(str(path), ControlHandler)
        except BaseException:
            self.http.server_close()
            raise
        self.path, self.inode = path, path.lstat().st_ino
        os.chmod(path, 0o600)
        try:
            allow_principals(path, self.policy.principals.values(), 'rw-')
        except BaseException:
            self.control.server_close()
            self.http.server_close()
            if path.lstat().st_ino == self.inode:
                path.unlink()
            raise
        self.control.uri = f'http://127.0.0.1:{self.http.server_address[1]}' + PATH
        for server in (self.http, self.control):
            server.policy, server.provider = self.policy, self.provider
            server.slots = threading.BoundedSemaphore(16)
            server.held = {}
        self.control.admin_slots = threading.BoundedSemaphore(4)
        self.control.principal_slots = {uid: threading.BoundedSemaphore(2) for uid in self.policy.principals.values()}
        self.threads = []

    def start(self):
        for server in (self.http, self.control):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        for server in (self.http, self.control):
            server.shutdown()
            server.server_close()
        self.policy.close()
        if self.path.exists() and self.path.lstat().st_ino == self.inode:
            self.path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    # Root/dedicated service identity, config and ancestors are trusted, components are not.
    settings = json.loads(private_file(args.config).read_text())
    adapter = Adapter(settings)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    adapter.start()
    try:
        stopped.wait()
    finally:
        adapter.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never print TLS/config/HTTP errors: these may contain private material.
        raise SystemExit('credential adapter startup failed') from None
