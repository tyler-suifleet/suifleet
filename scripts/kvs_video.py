#!/usr/bin/env python3
"""Clean-room H.264 media spool and Kinesis Video PutMedia component (Linux).

No AWS SDK/source is embedded. Input is the versioned binary media contract in
interfaces/compat/video/contract.json. Credentials are resolved only via SF-057.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

from aws_credentials import tcp_uid


class Full(Exception):
    pass


class Rejected(Exception):
    pass


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('integer outside media contract')
    return value


def endpoint(value, local=False):
    p = urlsplit(value)
    if (p.username or p.password or p.query or p.fragment or p.path not in ('', '/')
            or not p.hostname or p.scheme not in ('http', 'https')):
        raise ValueError('invalid endpoint')
    if local:
        if p.scheme != 'http' or not ipaddress.ip_address(p.hostname).is_loopback:
            raise ValueError('emulator endpoints must be literal loopback HTTP')
    elif p.scheme != 'https' or not p.hostname.endswith('.amazonaws.com') or p.port not in (None, 443):
        raise ValueError('AWS HTTPS endpoint required')
    return p


class Config:
    def __init__(self, value):
        required = {'version', 'stream_name', 'region', 'state_dir', 'width', 'height',
                    'codec_private_hex', 'max_spool_bytes', 'max_fragment_bytes',
                    'max_fragments', 'session_seconds', 'credential_provider_uid'}
        if set(value) - required - {'emulator', 'control_endpoint'} or not required <= value.keys():
            raise ValueError('unknown or missing configuration')
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError('unsupported video version')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', value['stream_name']):
            raise ValueError('invalid stream name')
        if not re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', value['region']):
            raise ValueError('invalid region')
        self.v = value
        for name, low, high in [('width', 1, 8192), ('height', 1, 8192),
                                ('max_fragment_bytes', 1024, 8*1024*1024),
                                ('max_spool_bytes', 2048, 1024*1024*1024),
                                ('max_fragments', 1, 10000), ('session_seconds', 1, 300),
                                ('credential_provider_uid', 0, 2**32-1)]:
            integer(value[name], low, high)
        if value['max_fragment_bytes'] + 256 > value['max_spool_bytes']:
            raise ValueError('spool must hold one maximum fragment')
        if type(value.get('emulator', False)) is not bool:
            raise ValueError('invalid emulator flag')
        self.local = value.get('emulator', False)
        self.control = endpoint(value.get('control_endpoint',
            f"https://kinesisvideo.{value['region']}.amazonaws.com"), self.local)
        if not self.local and self.control.hostname != f"kinesisvideo.{value['region']}.amazonaws.com":
            raise ValueError('configured regional KVS control endpoint required')
        self.codec = bytes.fromhex(value['codec_private_hex'])
        # AVCDecoderConfigurationRecord: one SPS, one PPS, four-byte NAL lengths.
        c = self.codec
        if len(c) < 11 or len(c) > 4096 or c[0] != 1 or c[1] != 66 or c[4:6] != b'\xff\xe1':
            raise ValueError('AVCC configuration required')
        n = int.from_bytes(c[6:8], 'big')
        off = 8+n
        if n < 4 or off+3 > len(c) or c[8] & 31 != 7 or c[off] != 1 or c[1:4] != c[9:12]:
            raise ValueError('one SPS/PPS required')
        m = int.from_bytes(c[off+1:off+3], 'big')
        if m < 1 or off+3+m != len(c) or c[off+3] & 31 != 8:
            raise ValueError('invalid PPS')
        binding = {k: value[k] for k in ('version', 'stream_name', 'region', 'width', 'height', 'codec_private_hex', 'credential_provider_uid')}
        binding.update(emulator=self.local, control_endpoint=self.control.geturl())
        self.identity = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def vint(n):
    for size in range(1, 9):
        if n < (1 << (7*size))-1:
            return ((1 << (7*size)) | n).to_bytes(size, 'big')
    raise ValueError('EBML size overflow')


def elem(tag, data):
    return bytes.fromhex(tag) + vint(len(data)) + data


def uint(tag, n):
    return elem(tag, n.to_bytes(max(1, (n.bit_length()+7)//8), 'big'))


def mkv_header(config):
    ebml = (uint('4286', 1) + uint('42f7', 1) + uint('42f2', 4) + uint('42f3', 8)
            + elem('4282', b'matroska') + uint('4287', 4) + uint('4285', 2))
    info = uint('2ad7b1', 1000000) + elem('4d80', b'SuiFleet') + elem('5741', b'SuiFleet')
    video = uint('b0', config.v['width']) + uint('ba', config.v['height'])
    track = (uint('d7', 1) + uint('73c5', 1) + uint('83', 1) + uint('9c', 0)
             + elem('86', b'V_MPEG4/ISO/AVC') + elem('63a2', config.codec) + elem('e0', video))
    return (elem('1a45dfa3', ebml) + bytes.fromhex('1853806701ffffffffffffff')
            + elem('1549a966', info) + elem('1654ae6b', elem('ae', track)))


def cluster(frames, config):
    if not frames or len(frames) > 1000:
        raise ValueError('1..1000 access units required')
    start = integer(frames[0][0], 1, 2**53-1)
    previous = start-1
    blocks = []
    total = 0
    for index, (timestamp, key, data) in enumerate(frames):
        integer(timestamp, start, min(start+10000, 2**53-1))
        if timestamp <= previous or type(key) is not bool or (index == 0 and not key):
            raise ValueError('monotonic, IDR-first fragment required')
        previous = timestamp
        total += len(data)
        if total > config.v['max_fragment_bytes']-256:
            raise ValueError('fragment too large')
        off, nal_types = 0, []
        while off < len(data):
            if off+4 > len(data):
                raise ValueError('truncated AVCC')
            size = int.from_bytes(data[off:off+4], 'big')
            off += 4
            if not 1 <= size <= len(data)-off or data[off] & 128:
                raise ValueError('invalid NAL')
            nal_types.append(data[off] & 31)
            off += size
        if not set(nal_types) <= {1, 5, 6, 9} or not ({1, 5} & set(nal_types)) or key != (5 in nal_types):
            raise ValueError('AVC access unit/keyframe mismatch')
        blocks.append(elem('a3', b'\x81' + struct.pack('>hB', timestamp-start, 128 if key else 0) + data))
    result = elem('1f43b675', uint('e7', start) + b''.join(blocks))
    if len(result) > config.v['max_fragment_bytes']:
        raise ValueError('fragment too large')
    return start, previous, result


class Spool:
    def __init__(self, config):
        self.config = config
        root = Path(config.v['state_dir'])
        if not root.is_absolute():
            raise ValueError('absolute spool path required')
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise ValueError('private spool directory required')
        if root.resolve() != root:
            raise ValueError('symlink spool ancestors forbidden')
        self.root = root
        for name in ('media.sqlite', 'media.sqlite-journal', 'uploader.lock'):
            path = root/name
            if path.is_symlink():
                raise ValueError('spool symlink forbidden')
        self.db = sqlite3.connect(root/'media.sqlite', timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA auto_vacuum=FULL')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS binding (identity TEXT, last_end INTEGER, last_start INTEGER, last_hash TEXT);
            CREATE TABLE IF NOT EXISTS fragments (start INTEGER PRIMARY KEY, end INTEGER, digest TEXT, media BLOB);
            CREATE TABLE IF NOT EXISTS failure (code TEXT);''')
        with self.db:
            row = self.db.execute('SELECT identity FROM binding').fetchone()
            if row is None:
                self.db.execute('INSERT INTO binding VALUES (?,0,0,?)', (config.identity, ''))
            elif row[0] != config.identity:
                raise ValueError('spool belongs to different stream/codec configuration')
        usage = self.db.execute('SELECT coalesce(sum(length(media)+256),0),count(*),coalesce(max(length(media)),0) FROM fragments').fetchone()
        if (usage[0] > config.v['max_spool_bytes'] or usage[1] > config.v['max_fragments']
                or usage[2] > config.v['max_fragment_bytes']):
            raise ValueError('configured quota below retained media')
        os.chmod(root/'media.sqlite', 0o600)

    def enqueue(self, frames):
        start, end, media = cluster(frames, self.config)
        digest = hashlib.sha256(media).hexdigest()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            last_end, last_start, last_hash = self.db.execute('SELECT last_end,last_start,last_hash FROM binding').fetchone()
            old = self.db.execute('SELECT digest FROM fragments WHERE start=?', (start,)).fetchone()
            if (old and old[0] == digest) or (start == last_start and digest == last_hash):
                self.db.rollback()
                return 'duplicate'
            if start <= last_end or (last_start and start-last_start < 200):
                raise ValueError('overlap or conflicting replay')
            used, count = self.db.execute('SELECT coalesce(sum(length(media)+256),0),count(*) FROM fragments').fetchone()
            if used+len(media)+256 > self.config.v['max_spool_bytes'] or count >= self.config.v['max_fragments']:
                raise Full('media spool full')
            self.db.execute('INSERT INTO fragments VALUES (?,?,?,?)', (start, end, digest, media))
            self.db.execute('UPDATE binding SET last_end=?,last_start=?,last_hash=?', (end, start, digest))
            self.db.commit()
            return 'queued'
        except BaseException:
            self.db.rollback()
            raise

    def pending(self):
        return self.db.execute('SELECT start FROM fragments ORDER BY start').fetchall()

    def close(self):
        self.db.close()


def credentials(config):
    uri = urlsplit(os.environ['AWS_CONTAINER_CREDENTIALS_FULL_URI'])
    if (uri.scheme != 'http' or uri.hostname != '127.0.0.1' or uri.username or uri.password
            or uri.path != '/2016-11-01/credentialprovider/' or uri.query or uri.fragment):
        raise ValueError('SF-057 loopback credential URI required')
    conn = http.client.HTTPConnection(uri.hostname, uri.port, timeout=5)
    timer = None
    try:
        conn.connect()
        active = conn.sock
        # Authenticate the local issuer before disclosing the grant token. A
        # different UID must not substitute credentials after broker port reuse.
        identity_deadline = time.monotonic()+5
        while True:
            uid = tcp_uid(active, require_accepted=True)
            if uid is not None:
                if uid != config.v['credential_provider_uid']:
                    raise ValueError('unexpected credential broker UID')
                break
            if time.monotonic() >= identity_deadline:
                raise ValueError('credential broker identity unavailable')
            time.sleep(0.01)
        def expire():
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(5, expire)
        timer.daemon = True
        timer.start()
        conn.request('GET', uri.path, headers={'Authorization': os.environ['AWS_CONTAINER_AUTHORIZATION_TOKEN']})
        r = conn.getresponse()
        raw = r.read(16385)
        if r.status != 200 or len(raw) > 16384:
            raise ValueError('credentials unavailable')
        c = json.loads(raw)
        for key in ('AccessKeyId', 'SecretAccessKey', 'Token', 'Expiration'):
            if not isinstance(c.get(key), str) or not 1 <= len(c[key]) <= 8192:
                raise ValueError('invalid credentials')
        expiration = dt.datetime.fromisoformat(c['Expiration'].replace('Z', '+00:00'))
        if expiration.tzinfo is None or expiration.timestamp() < time.time()+10:
            raise ValueError('expired credentials')
        c['expires'] = expiration.timestamp()
        return c
    finally:
        if timer:
            timer.cancel()
        conn.close()


def sign(host, path, headers, payload_hash, c, region, now=None):
    date = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    headers = {k.lower(): v for k, v in headers.items()}
    headers.update({'host': host, 'x-amz-date': date, 'x-amz-security-token': c['Token']})
    names = ';'.join(sorted(headers))
    canonical = ''.join(k+':'+ ' '.join(headers[k].split())+'\n' for k in sorted(headers))
    request = 'POST\n'+path+'\n\n'+canonical+'\n'+names+'\n'+payload_hash
    scope = date[:8]+'/'+region+'/kinesisvideo/aws4_request'
    key = ('AWS4'+c['SecretAccessKey']).encode()
    for part in (date[:8], region, 'kinesisvideo', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, ('AWS4-HMAC-SHA256\n'+date+'\n'+scope+'\n'+hashlib.sha256(request.encode()).hexdigest()).encode(), hashlib.sha256).hexdigest()
    headers['authorization'] = f"AWS4-HMAC-SHA256 Credential={c['AccessKeyId']}/{scope}, SignedHeaders={names}, Signature={signature}"
    return headers


def connection(p, timeout):
    if p.scheme == 'http':
        return http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return http.client.HTTPSConnection(p.hostname, p.port, timeout=timeout, context=context)


def discover(config, c):
    p = config.control
    body = json.dumps({'StreamName': config.v['stream_name'], 'APIName': 'PUT_MEDIA'}, separators=(',', ':')).encode()
    headers = sign(p.netloc, '/getDataEndpoint', {'content-type': 'application/json'},
                   hashlib.sha256(body).hexdigest(), c, config.v['region'])
    conn = connection(p, 5)
    timer = None
    try:
        conn.connect()
        active = conn.sock
        def expire():
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(5, expire)
        timer.daemon = True
        timer.start()
        conn.request('POST', '/getDataEndpoint', body, headers)
        r = conn.getresponse()
        raw = r.read(16385)
        if r.status != 200 or len(raw) > 16384:
            raise OSError('endpoint unavailable')
        result = endpoint(json.loads(raw)['DataEndpoint'], config.local)
        if not config.local and not result.hostname.endswith(f".kinesisvideo.{config.v['region']}.amazonaws.com"):
            raise ValueError('unexpected KVS data endpoint')
        return result
    finally:
        if timer:
            timer.cancel()
        conn.close()


def upload(spool):
    """One bounded, duplex multi-fragment session. Only PERSISTED frees media."""
    cfg = spool.config
    pending = [r[0] for r in spool.db.execute('SELECT start FROM fragments ORDER BY start LIMIT 1000')]
    if not pending:
        return 0
    failure = spool.db.execute('SELECT code FROM failure').fetchone()
    if failure:
        raise Rejected('operator inspection required for retained rejected media')
    c = credentials(cfg)  # no static-key/default-chain fallbacks; refresh every session
    p = discover(cfg, c)
    duration = min(cfg.v['session_seconds'], c['expires']-time.time()-5)
    if duration <= 0:
        raise OSError('credentials expiring')
    headers = sign(p.netloc, '/putMedia', {
        'content-type': 'application/octet-stream', 'x-amzn-stream-name': cfg.v['stream_name'],
        'x-amzn-fragment-timecode-type': 'ABSOLUTE',
        'x-amzn-producer-start-timestamp': f'{pending[0]/1000:.3f}', 'x-amzn-fragment-acknowledgment-required': '1',
        'x-amz-content-sha256': hashlib.sha256(b'').hexdigest()}, hashlib.sha256(b'').hexdigest(), c, cfg.v['region'])
    conn = connection(p, 5)
    conn.connect()
    sock = conn.sock
    def expire():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(duration, expire)
    timer.daemon = True
    timer.start()
    sent, errors = set(), []
    gate = threading.Lock()
    worker = None
    count = 0
    try:
        conn.putrequest('POST', '/putMedia', skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader('Transfer-Encoding', 'chunked')
        conn.endheaders()
        def send():
            db = sqlite3.connect(spool.root/'media.sqlite', timeout=5)
            def chunk(data):
                sock.sendall(f'{len(data):x}\r\n'.encode()+data+b'\r\n')
            try:
                chunk(mkv_header(cfg))
                next_send = time.monotonic()
                for timestamp in pending:
                    row = db.execute('SELECT media,digest FROM fragments WHERE start=?', (timestamp,)).fetchone()
                    if row is None or hashlib.sha256(row[0]).hexdigest() != row[1]:
                        raise ValueError('media integrity failure')
                    if c['expires'] <= time.time()+5:
                        raise OSError('credentials expired')
                    time.sleep(max(0, next_send-time.monotonic()))
                    next_send = time.monotonic()+max(0.2, len(row[0])/12000000)
                    with gate:
                        sent.add(timestamp)
                    chunk(row[0])
                sock.sendall(b'0\r\n\r\n')
            except Exception:
                errors.append(True)
                expire()
            finally:
                db.close()
        worker = threading.Thread(target=send)
        worker.start()
        response = conn.getresponse()
        if response.status != 200:
            raise OSError('PutMedia rejected HTTP request')
        # ACKs are newline-delimited JSON on the simultaneous response stream.
        # A hard session deadline bounds drip-fed and missing-final-ACK peers.
        acked = set()
        retire_index = 0
        ack_count = 0
        while True:
            raw = response.readline(4097)
            if not raw:
                break
            ack_count += 1
            if len(raw) > 4096 or not raw.endswith(b'\n') or ack_count > len(pending)*8+100:
                raise ValueError('invalid ACK framing')
            ack = json.loads(raw)
            if not isinstance(ack, dict):
                raise ValueError('invalid ACK object')
            event = ack.get('EventType', ack.get('AckEventType'))
            if event == 'IDLE':
                continue
            timestamp = ack.get('FragmentTimecode')
            with gate:
                known = type(timestamp) is int and timestamp in sent
            if not known:
                raise ValueError('ACK for unsent fragment')
            if event == 'ERROR':
                code = ack.get('ErrorId')
                if code in (4000, 4003, 5000, 5001):
                    raise OSError('retryable fragment rejection')
                with spool.db:
                    spool.db.execute('INSERT INTO failure VALUES (?)', ('fragment-rejected',))
                raise Rejected('fragment rejected; spool retained for inspection')
            if event not in ('BUFFERING', 'RECEIVED', 'PERSISTED'):
                raise ValueError('unknown ACK event')
            if event == 'PERSISTED' and timestamp not in acked:
                if not isinstance(ack.get('FragmentNumber'), str) or not ack['FragmentNumber']:
                    raise ValueError('missing fragment number')
                acked.add(timestamp)
                count += 1
                # Retire only a contiguous persisted prefix. Later ACKs cannot
                # discard media across an unacknowledged gap (SF-064 v1).
                with spool.db:
                    while retire_index < len(pending) and pending[retire_index] in acked:
                        spool.db.execute('DELETE FROM fragments WHERE start=?', (pending[retire_index],))
                        retire_index += 1
        if errors or count != len(pending):
            raise OSError('session incomplete; replay unacknowledged fragments')
        return count
    finally:
        expire()
        if worker:
            worker.join(6)
        timer.cancel()
        conn.close()


def read_exact(source, n):
    data = bytearray()
    while len(data) < n:
        part = source.read(n-len(data))
        if not part:
            raise ValueError('truncated media input')
        data.extend(part)
    return bytes(data)


def ingest(source, spool, output):
    while True:
        prefix = source.read(4)
        if not prefix:
            return
        prefix += read_exact(source, 4-len(prefix))
        size = integer(int.from_bytes(prefix, 'big'), 1, 65536)
        meta = json.loads(read_exact(source, size))
        if set(meta) != {'version', 'frames'} or type(meta['version']) is not int or meta['version'] != 1:
            raise ValueError('unsupported input version')
        if not isinstance(meta['frames'], list) or not 1 <= len(meta['frames']) <= 1000:
            raise ValueError('invalid input frames')
        frames, total = [], 0
        for f in meta['frames']:
            if set(f) != {'timestamp_ms', 'keyframe', 'size'}:
                raise ValueError('unknown frame metadata')
            n = integer(f['size'], 1, spool.config.v['max_fragment_bytes'])
            total += n
            if total > spool.config.v['max_fragment_bytes']:
                raise ValueError('input fragment limit')
            frames.append((f['timestamp_ms'], f['keyframe'], read_exact(source, n)))
        # Backpressure: one bounded fragment held in memory; stop reading source
        # until the independent uploader makes room. No silent eviction.
        while True:
            try:
                result = spool.enqueue(frames)
                break
            except Full:
                time.sleep(0.25)
        output.write(json.dumps({'start_ms': frames[0][0], 'status': result})+'\n')
        output.flush()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('operation', choices=['ingest', 'upload', 'upload-once'])
    args = parser.parse_args()
    spool = None
    lock = None
    try:
        cfg = Config(json.loads(Path(args.config).read_text()))
        spool = Spool(cfg)
        if args.operation == 'ingest':
            ingest(sys.stdin.buffer, spool, sys.stdout)
        else:
            lock = open(spool.root/'uploader.lock', 'a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backoff = 1
            while True:
                try:
                    count = upload(spool)
                    backoff = 1
                    if args.operation == 'upload-once':
                        print(json.dumps({'persisted': count}))
                        break
                    time.sleep(1)
                except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                    if args.operation == 'upload-once':
                        raise
                    # No exception text, credentials, media or server body in logs.
                    print('video session unavailable; retained media will retry', file=sys.stderr)
                    time.sleep(backoff)
                    backoff = min(60, backoff*2)
    except (OSError, ValueError, KeyError, TypeError, Rejected, sqlite3.Error, http.client.HTTPException):
        print('video adapter stopped; inspect configuration and retained spool', file=sys.stderr)
        return 1
    finally:
        if spool:
            spool.close()
        if lock:
            lock.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
