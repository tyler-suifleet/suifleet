#!/usr/bin/env python3
"""Opt-in Linux Cloudwatch 4.0.0 companion. No AWS default credential chain."""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from aws_credentials import allow_principals, private_file
METRIC_UNITS = frozenset(
    ["Seconds", "Microseconds", "Milliseconds", "Bytes", "Kilobytes", "Megabytes", "Gigabytes", "Terabytes",
     "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits", "Percent", "Count", "Bytes/Second",
     "Kilobytes/Second", "Megabytes/Second", "Gigabytes/Second", "Terabytes/Second", "Bits/Second",
     "Kilobits/Second", "Megabits/Second", "Gigabits/Second", "Terabits/Second", "Count/Second", "None"])
METRIC_BATCH = 20
RETRYABLE = {"throttled", "server-error", "connection-error"}


def _number(value) -> bool:
    """A finite JSON number; booleans, NaN and infinities are not numbers here.

    JSON integers are unbounded in Python and are never converted to float, so
    an integer too large for a float is still classified without raising;
    callers compare it against their exact bounds."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _metric_valid(message) -> bool:
    request = message.get("request") if isinstance(message, dict) else None
    if not isinstance(request, dict) or set(request) != {"namespace", "metricData"}:
        return False
    namespace, data = request["namespace"], request["metricData"]
    if not isinstance(namespace, str) or not 0 < len(namespace) <= 255 or namespace[0] == ":" or namespace.startswith("AWS/"):
        return False
    if not isinstance(data, dict) or set(data) - {"metricName", "value", "dimensions", "timestamp", "unit"}:
        return False
    name, value = data.get("metricName"), data.get("value")
    if not isinstance(name, str) or not 0 < len(name) <= 255 or not _number(value):
        return False
    if value != 0 and not 8.515920e-109 <= abs(value) <= 1.174271e+108:
        return False
    dimensions = data.get("dimensions", [])
    if not isinstance(dimensions, list) or len(dimensions) > 10:
        return False
    for dimension in dimensions:
        if not isinstance(dimension, dict) or set(dimension) != {"name", "value"}:
            return False
        if not all(isinstance(dimension[k], str) and dimension[k] for k in ("name", "value")) or dimension["name"] == "coreName":
            return False
    if "timestamp" in data and (not _number(data["timestamp"]) or data["timestamp"] < 0):
        return False
    unit = data.get("unit", "None")
    return isinstance(unit, str) and unit in METRIC_UNITS


MAX_FRAME = 16384
MAX_BODY = 40000


def body(namespace, entries):
    fields = {'Action': 'PutMetricData', 'Version': '2010-08-01', 'Namespace': namespace}
    for i, entry in enumerate(entries, 1):
        prefix = f'MetricData.member.{i}.'
        for key in ('MetricName', 'Value', 'Unit', 'Timestamp'):
            fields[prefix + key] = entry[key]
        for j, dimension in enumerate(entry['Dimensions'], 1):
            for key in ('Name', 'Value'):
                fields[f'{prefix}Dimensions.member.{j}.{key}'] = dimension[key]
    return urllib.parse.urlencode(fields).encode()


@contextmanager
def deadline(connection):
    connection.connect()
    sock = connection.sock
    def expire():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(5, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        connection.close()


class Transport:
    def __init__(self, region, credential_env, endpoint=None, *, emulator=False):
        if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', region):
            raise ValueError('invalid region')
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.load_default_certs()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.region = region
        self.endpoint = urllib.parse.urlsplit(endpoint or f'https://monitoring.{region}.amazonaws.com/')
        if self.endpoint.scheme != 'https' and not (emulator and self.endpoint.scheme == 'http' and self.endpoint.hostname == '127.0.0.1'):
            raise ValueError('HTTPS required')
        if self.endpoint.path not in ('', '/') or self.endpoint.query or self.endpoint.fragment or self.endpoint.username or not self.endpoint.hostname:
            raise ValueError('invalid endpoint')
        # Only the explicit SF-057 loopback provider. Never read profiles, IMDS or static keys.
        self.env = credential_env
        uri = urllib.parse.urlsplit(credential_env['AWS_CONTAINER_CREDENTIALS_FULL_URI'])
        if uri.scheme != 'http' or uri.hostname != '127.0.0.1' or uri.path != '/2016-11-01/credentialprovider/' or uri.query or uri.fragment or uri.username:
            raise ValueError('scoped credential endpoint required')
        self.provider = uri

    def credentials(self):
        conn = http.client.HTTPConnection(self.provider.hostname, self.provider.port, timeout=5)
        with deadline(conn):
            conn.request('GET', self.provider.path, headers={'Authorization': self.env['AWS_CONTAINER_AUTHORIZATION_TOKEN']})
            response = conn.getresponse()
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                raise ValueError('credentials unavailable')
            value = json.loads(raw)
            if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in ('AccessKeyId', 'SecretAccessKey', 'Token', 'Expiration')):
                raise ValueError('invalid credentials')
            expires = datetime.datetime.fromisoformat(value['Expiration'].replace('Z', '+00:00'))
            if expires.tzinfo is None or expires.timestamp() <= time.time():
                raise ValueError('invalid credentials')
            return value

    def send(self, namespace, entries):
        try:
            credentials = self.credentials()
            payload = body(namespace, entries)
            now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            date = now[:8]
            endpoint = self.endpoint
            headers = {'content-type': 'application/x-www-form-urlencoded', 'host': endpoint.netloc,
                       'x-amz-date': now, 'x-amz-security-token': credentials['Token']}
            signed = ';'.join(sorted(headers))
            canonical = 'POST\n/\n\n' + ''.join(f'{k}:{headers[k]}\n' for k in sorted(headers)) + '\n' + signed + '\n' + hashlib.sha256(payload).hexdigest()
            scope = f'{date}/{self.region}/monitoring/aws4_request'
            to_sign = f'AWS4-HMAC-SHA256\n{now}\n{scope}\n' + hashlib.sha256(canonical.encode()).hexdigest()
            key = ('AWS4' + credentials['SecretAccessKey']).encode()
            for part in (date, self.region, 'monitoring', 'aws4_request'):
                key = hmac.new(key, part.encode(), hashlib.sha256).digest()
            signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
            headers['Authorization'] = f"AWS4-HMAC-SHA256 Credential={credentials['AccessKeyId']}/{scope}, SignedHeaders={signed}, Signature={signature}"
            cls = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
            conn = cls(endpoint.hostname, endpoint.port, timeout=5, **({'context': self.context} if endpoint.scheme == 'https' else {}))
            with deadline(conn):
                conn.request('POST', '/', payload, headers)
                response = conn.getresponse()
                raw = response.read(16385)
                if response.status == 200:
                    return 'ok'
                try:
                    code = next((node.text for node in ET.fromstring(raw).iter() if node.tag.split('}')[-1] == 'Code'), '')
                except ET.ParseError:
                    code = ''
                if response.status == 429 or code in {'Throttling', 'ThrottlingException', 'ThrottledException', 'TooManyRequestsException', 'RequestLimitExceeded'}:
                    return 'throttled'
                if response.status >= 500:
                    return 'server-error'
                # Permission/credential errors may be repairable: retain instead of losing data.
                if response.status in (401, 403):
                    return 'connection-error'
                if response.status == 400 and code in {'InvalidParameterValue', 'InvalidParameterCombination', 'MissingParameter', 'ValidationError', 'InvalidRequest'}:
                    return 'invalid'
                return 'connection-error'
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            return 'connection-error'


class Exporter:
    def __init__(self, path, transport, core_name, interval=10, retain=5000, clock=time.monotonic):
        self.configure(interval, retain)
        if not isinstance(core_name, str) or not 0 < len(core_name) <= 255:
            raise ValueError('invalid core name')
        self.transport, self.core, self.interval, self.retain, self.clock = transport, core_name, interval, retain, clock
        self.last = clock()
        self.lock = open(str(path) + '.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA max_page_count=131072')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        for name in ('dropped', 'rejected'):
            self.db.execute('INSERT OR IGNORE INTO counters VALUES (?,0)', (name,))
        if self.db.execute('SELECT count(*) FROM metrics').fetchone()[0] > retain:
            raise ValueError('queue exceeds configured retention')
        self.db.commit()

    def configure(self, interval, retain):
        if type(interval) is not int or not 0 <= interval <= 900 or type(retain) is not int or not 2000 <= retain <= 10000:
            raise ValueError('invalid limits')
        self.interval, self.retain = interval, retain

    def close(self):
        self.db.close()
        self.lock.close()

    def state(self, **extra):
        return dict(extra, buffered=self.db.execute('SELECT count(*) FROM metrics').fetchone()[0],
                    **dict(self.db.execute('SELECT name,value FROM counters')))

    def put(self, message):
        if not _metric_valid(message):
            return {'status': 'fail', 'error': 'InvalidInputException'}
        request = message['request']
        data = request['metricData']
        try:
            timestamp = datetime.datetime.fromtimestamp(data.get('timestamp', time.time()), datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        except (ValueError, OverflowError, OSError):
            return {'status': 'fail', 'error': 'InvalidInputException'}
        entry = dict(MetricName=data['metricName'], Value=data['value'], Unit=data.get('unit', 'None'), Timestamp=timestamp,
                     Dimensions=[dict(Name=d['name'], Value=d['value']) for d in data.get('dimensions', [])] + [dict(Name='coreName', Value=self.core)])
        if len(body(request['namespace'], [entry])) > MAX_BODY:
            return {'status': 'fail', 'error': 'InvalidInputException'}
        with self.db:
            self.db.execute('INSERT INTO metrics(namespace,data) VALUES (?,?)', (request['namespace'], json.dumps(entry)))
            if self.state()['buffered'] > self.retain:
                self.db.execute('DELETE FROM metrics WHERE id=(SELECT min(id) FROM metrics WHERE namespace=?)', (request['namespace'],))
                self.db.execute("UPDATE counters SET value=value+1 WHERE name='dropped'")
        return self.state(status='success')

    def tick(self):
        namespaces = list(self.db.execute('SELECT namespace,count(*) FROM metrics GROUP BY namespace ORDER BY min(id)'))
        now = self.clock()
        if now - self.last < self.interval and not any(count >= 20 for _, count in namespaces):
            return self.state(calls=[])
        self.last = now
        calls = []
        for namespace, _ in namespaces:
            rows = list(self.db.execute('SELECT id,data FROM metrics WHERE namespace=? ORDER BY id', (namespace,)))
            while rows:
                batch = []
                for row in rows[:20]:
                    candidate = batch + [(row[0], json.loads(row[1]))]
                    if len(body(namespace, [e for _, e in candidate])) > MAX_BODY:
                        break
                    batch = candidate
                rows = rows[len(batch):]
                time.sleep(0.01)  # below pinned 150 requests/second, even on loopback
                result = self.transport.send(namespace, [e for _, e in batch])
                calls.append(dict(namespace=namespace, count=len(batch), first=batch[0][1]['MetricName'], last=batch[-1][1]['MetricName'], result=result))
                if result in ('ok', 'invalid'):
                    with self.db:
                        self.db.executemany('DELETE FROM metrics WHERE id=?', [(i,) for i, _ in batch])
                        if result == 'invalid':
                            self.db.execute("UPDATE counters SET value=value+? WHERE name='rejected'", (len(batch),))
        return self.state(calls=calls)


def receive(connection, uid, publishers, exporter):
    if uid not in publishers:
        return {'status': 'fail', 'error': 'Unauthorized'}
    try:
        with connection.makefile('rb') as source:
            raw = source.readline(MAX_FRAME + 1)
        if len(raw) <= MAX_FRAME and raw.endswith(b'\n'):
            return exporter.put(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return {'status': 'fail', 'error': 'InvalidInputException'}


def serve(settings):
    allowed = {'socket', 'queue', 'core_name', 'region', 'credential_socket', 'credential_component', 'publishers', 'daemon_uid', 'interval', 'retain'}
    if set(settings) - allowed:
        raise ValueError('unknown settings')
    publishers = settings['publishers']
    daemon_uid = settings['daemon_uid']
    if type(daemon_uid) is not int or daemon_uid <= 0 or daemon_uid == os.geteuid():
        raise ValueError('explicit daemon UID required')
    if not isinstance(publishers, list) or any(type(uid) is not int or uid <= 0 or uid in (os.geteuid(), daemon_uid) for uid in publishers) or len(set(publishers)) != len(publishers):
        raise ValueError('distinct dedicated publisher UIDs required')
    # The credential wrapper execs this service; only this trusted adapter gets AWS tokens.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
    from suifleet_aws import environment
    env = environment(settings['credential_socket'], settings['credential_component'])
    transport = Transport(settings['region'], env)
    queue = Path(settings['queue'])
    private_file(queue.parent / '.owner')  # preprovisioned owner-only marker in private state directory
    if queue.parent.stat().st_mode & 0o077:
        raise ValueError('private queue directory required')
    if queue.exists() or queue.is_symlink():
        private_file(queue)
    os.umask(0o077)
    exporter = Exporter(queue, transport, settings['core_name'], settings.get('interval', 10), settings.get('retain', 5000))
    path = Path(settings['socket'])
    private_file(path.parent / '.owner')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))  # never unlink an unknown existing socket
    allow_principals(path, publishers, 'rw-')
    listener.listen(16)
    listener.settimeout(.2)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        while not stopped:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                exporter.tick()
                continue
            with connection:
                connection.settimeout(1)
                _, uid, _ = struct.unpack('iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                result = receive(connection, uid, publishers, exporter)
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass
            exporter.tick()
    finally:
        listener.close()
        path.unlink()
        exporter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    serve(json.loads(private_file(args.config).read_text()))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('metrics adapter failed') from None
