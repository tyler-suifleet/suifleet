#!/usr/bin/env python3
"""Actual component subprocesses, independent MKV parsing, offline AWS emulator."""
import datetime as dt
import hashlib
import hmac
import http.server
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import kvs_video as video
from kvs_h264_source import records, encode_record

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT/'interfaces/compat/video'
BASE = json.loads((FIX/'example.json').read_text())


def verify_signature(handler, body):
    """Independent SigV4 verification; never call the implementation signer."""
    auth = handler.headers['Authorization']
    match = re.fullmatch(r'AWS4-HMAC-SHA256 Credential=([^/]+)/([^,]+), SignedHeaders=([^,]+), Signature=([a-f0-9]{64})', auth)
    assert match, 'missing SigV4'
    access, scope, names, signature = match.groups()
    assert access == 'test-access'
    day, region, service, terminal = scope.split('/')
    assert region == 'us-west-2' and service == 'kinesisvideo' and terminal == 'aws4_request'
    assert handler.headers['x-amz-security-token'] == handler.server.token
    canonical_headers = ''.join(n+':'+ ' '.join(handler.headers[n].split())+'\n' for n in names.split(';'))
    digest = hashlib.sha256(body).hexdigest() if handler.path == '/getDataEndpoint' else hashlib.sha256(b'').hexdigest()
    canonical = '\n'.join(('POST', handler.path, '', canonical_headers, names, digest))
    to_sign = '\n'.join(('AWS4-HMAC-SHA256', handler.headers['x-amz-date'], scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = b'AWS4test-secret'
    for value in (day, region, service, terminal):
        key = hmac.digest(key, value.encode(), 'sha256')
    assert hmac.compare_digest(hmac.digest(key, to_sign.encode(), 'sha256').hex(), signature)


def ebml(data):
    """Independent finite EBML element reader for emulator assertions."""
    offset = 0
    while offset < len(data):
        first = data[offset]
        tag_size = next(i for i in range(1,5) if first & (0x80 >> (i-1)))
        tag = data[offset:offset+tag_size].hex()
        offset += tag_size
        first = data[offset]
        size_len = next(i for i in range(1,9) if first & (0x80 >> (i-1)))
        size = int.from_bytes(data[offset:offset+size_len], 'big') & ((1 << (7*size_len))-1)
        offset += size_len
        assert offset+size <= len(data)
        yield tag, data[offset:offset+size]
        offset += size


def inspect_cluster(data):
    [(tag, payload)] = list(ebml(data))
    assert tag == '1f43b675'
    fields = list(ebml(payload))
    assert fields[0][0] == 'e7'
    stamp = int.from_bytes(fields[0][1], 'big')
    timestamps = []
    for index, (tag, block) in enumerate(fields[1:]):
        assert tag == 'a3' and block[0] == 0x81
        delta = struct.unpack('>h', block[1:3])[0]
        assert delta >= 0
        assert index != 0 or block[3] & 128
        timestamps.append(stamp+delta)
        pos = 4
        while pos < len(block):
            n = int.from_bytes(block[pos:pos+4], 'big')
            pos += 4
            assert n > 0 and pos+n <= len(block)
            pos += n
        assert pos == len(block)
    assert timestamps == sorted(set(timestamps))
    return stamp, timestamps


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass

    def reply(self, code, body):
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == '/2016-11-01/credentialprovider/'
        assert self.headers['Authorization'] == 'local-test-token'
        self.server.credential_calls += 1
        self.reply(200, json.dumps({'AccessKeyId': 'test-access', 'SecretAccessKey': 'test-secret',
            'Token': self.server.token, 'Expiration': (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=self.server.ttl)).isoformat()}).encode())

    def do_POST(self):
        try:
            self.request.settimeout(4)
            if self.path == '/getDataEndpoint':
                body = self.rfile.read(int(self.headers['Content-Length']))
                verify_signature(self, body)
                assert json.loads(body) == {'StreamName': 'camera', 'APIName': 'PUT_MEDIA'}
                self.reply(200, json.dumps({'DataEndpoint': self.server.url}).encode())
                return
            assert self.path == '/putMedia'
            verify_signature(self, b'')
            assert self.headers['x-amzn-stream-name'] == 'camera'
            assert self.headers['Transfer-Encoding'] == 'chunked'
            assert self.headers['x-amzn-fragment-timecode-type'] == 'ABSOLUTE'
            if self.server.mode == 'http-reject':
                self.reply(403, b'')
                return
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            def ack(event, stamp, **kw):
                raw = json.dumps(dict(EventType=event, FragmentTimecode=stamp, FragmentNumber=str(stamp), **kw)).encode()+b'\n'
                # Split ACK across HTTP chunks, not one read == one ACK.
                for part in (raw[:7], raw[7:]):
                    self.wfile.write(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
                    self.wfile.flush()
            chunks = []
            while True:
                line = self.rfile.readline(100)
                if not line:
                    return  # expected client disconnect on rejected/corrupt media
                n = int(line.strip(), 16)
                if not n:
                    assert self.rfile.read(2) == b'\r\n'
                    break
                assert n <= BASE['max_fragment_bytes']
                data = self.rfile.read(n)
                assert len(data) == n and self.rfile.read(2) == b'\r\n'
                if not chunks:
                    assert data.startswith(bytes.fromhex('1a45dfa3'))
                    assert data.count(bytes.fromhex('18538067')) == 1
                    assert b'V_MPEG4/ISO/AVC' in data
                else:
                    stamp, times = inspect_cluster(data)
                    self.server.received.append((stamp, hashlib.sha256(data).hexdigest(), times))
                    self.server.arrived.set()
                    if self.server.mode == 'disconnect' or (self.server.mode == 'partial-disconnect' and len(chunks) == 2):
                        self.request.shutdown(socket.SHUT_RDWR)
                        return
                    if self.server.mode == 'expiry-hang':
                        for _ in range(30):
                            ack('IDLE', stamp)
                            time.sleep(.5)
                        return
                    if self.server.mode == 'hang':
                        self.server.release.wait(4)
                        return
                    if self.server.mode == 'reject':
                        ack('ERROR', stamp, ErrorId=4006)
                    elif self.server.mode == 'transient':
                        ack('ERROR', stamp, ErrorId=5000)
                    elif self.server.mode == 'unknown':
                        ack('PERSISTED', stamp+1)
                    elif self.server.mode == 'oversize':
                        self.wfile.write(b'1002\r\n'+b'X'*4098+b'\r\n')
                        self.wfile.flush()
                    else:
                        ack('BUFFERING', stamp)
                        ack('RECEIVED', stamp)
                        if self.server.mode not in ('received-only', 'gap'):
                            ack('PERSISTED', stamp)
                            ack('PERSISTED', stamp)  # duplicate ACK is harmless
                chunks.append(data)
            if self.server.mode == 'gap':
                ack('PERSISTED', inspect_cluster(chunks[-1])[0])
            self.server.sessions.append(b''.join(chunks))
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass
        except BaseException as error:
            self.server.errors.append(repr(error))
        finally:
            self.close_connection = True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server_port}'
        self.token, self.ttl = 'session-one', 3600
        self.mode = 'success'
        self.credential_calls = 0
        self.received, self.sessions, self.errors = [], [], []
        self.arrived, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.serve_forever)
        self.thread.start()

    def close(self):
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join()


class VideoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server()
        self.v = dict(BASE, state_dir=str(self.root/'spool'), emulator=True,
                      control_endpoint=self.server.url, session_seconds=2)
        self.cfg = video.Config(self.v)
        self.spool = video.Spool(self.cfg)
        self.frames = list(records(io.BytesIO((FIX/'test-pattern.h264').read_bytes()), self.cfg, 1700000000000, 5))
        self.assertEqual([len(f) for f in self.frames], [5, 5])
        self.env = dict(os.environ, AWS_CONTAINER_CREDENTIALS_FULL_URI=self.server.url+'/2016-11-01/credentialprovider/', AWS_CONTAINER_AUTHORIZATION_TOKEN='local-test-token', PYTHONDONTWRITEBYTECODE='1')
        self.path = self.root/'config.json'
        self.path.write_text(json.dumps(self.v))

    def tearDown(self):
        self.spool.close()
        self.server.close()
        self.assertEqual(self.server.errors, [])
        self.temp.cleanup()

    def enqueue(self):
        for f in self.frames:
            self.assertEqual(self.spool.enqueue(f), 'queued')

    def run_upload(self, success=True):
        run = subprocess.run([sys.executable, str(ROOT/'scripts/kvs_video.py'), '--config', str(self.path), 'upload-once'], env=self.env, capture_output=True, timeout=12)
        self.assertEqual(run.returncode, 0 if success else 1, run.stderr)
        self.assertNotIn(b'test-secret', run.stdout+run.stderr)
        self.assertNotIn(self.server.token.encode(), run.stdout+run.stderr)
        return run

    def test_real_source_ingest_multifragment_upload(self):
        source = subprocess.run([sys.executable, str(ROOT/'scripts/kvs_h264_source.py'), '--config', str(self.path), '--fps', '5', '--start-ms', '1700000000000'], input=(FIX/'test-pattern.h264').read_bytes(), capture_output=True, check=True)
        ingest = subprocess.run([sys.executable, str(ROOT/'scripts/kvs_video.py'), '--config', str(self.path), 'ingest'], input=source.stdout, capture_output=True, check=True)
        self.assertEqual(len(ingest.stdout.splitlines()), 2)
        self.run_upload()
        self.assertEqual(self.spool.pending(), [])
        self.assertEqual(len(self.server.sessions), 1)
        self.assertEqual([r[0] for r in self.server.received], [1700000000000, 1700000001000])
        self.assertEqual(self.server.received[1][2], [1700000001000+i*200 for i in range(5)])
        self.assertEqual(self.spool.enqueue(self.frames[-1]), 'duplicate')
        # Independent codec/container execution, not merely matching our muxer.
        media = self.root/'captured.mkv'
        media.write_bytes(self.server.sessions[0])
        ffmpeg = os.environ.get('SUIFLEET_FFMPEG', 'ffmpeg')
        decoded = subprocess.run([ffmpeg, '-v', 'error', '-i', str(media), '-vsync', '0', '-f', 'framemd5', '-'], capture_output=True, check=True, timeout=10)
        rows = [line for line in decoded.stdout.splitlines() if not line.startswith(b'#')]
        expected = json.loads((FIX/'fixture.json').read_text())['decoded_frame_md5']
        self.assertEqual([r.split(b',')[-1].strip().decode() for r in rows], expected)
        self.assertEqual(decoded.stderr, b'')

    def test_disconnect_restart_replays_identical_media(self):
        self.enqueue()
        self.server.mode = 'disconnect'
        self.run_upload(False)
        first = self.server.received[0][:2]
        self.assertEqual(len(self.spool.pending()), 2)
        self.spool.close()
        self.spool = video.Spool(self.cfg)
        self.server.mode = 'success'
        self.server.token = 'rotated-session'
        self.run_upload()
        self.assertEqual(self.server.received[1][:2], first)
        self.assertEqual(self.spool.pending(), [])
        self.assertEqual(self.server.credential_calls, 2)

    def test_out_of_order_ack_does_not_retire_across_gap(self):
        self.enqueue()
        self.server.mode = 'gap'
        self.run_upload(False)
        self.assertEqual(len(self.spool.pending()), 2)
        self.server.mode = 'success'
        self.run_upload()
        self.assertEqual(self.spool.pending(), [])

    def test_partial_persistence_restarts_from_unacknowledged_fragment(self):
        self.enqueue()
        self.server.mode = 'partial-disconnect'
        self.run_upload(False)
        self.assertEqual(self.spool.pending(), [(1700000001000,)])
        self.server.mode = 'success'
        self.run_upload()
        self.assertEqual([r[0] for r in self.server.received], [1700000000000, 1700000001000, 1700000001000])

    def test_byte_quota_and_reclaimed_disk(self):
        self.spool.close()
        self.v.update(max_fragment_bytes=2048, max_spool_bytes=2304)
        self.cfg = video.Config(self.v)
        self.spool = video.Spool(self.cfg)
        self.path.write_text(json.dumps(self.v))
        self.spool.enqueue(self.frames[0])
        with self.assertRaises(video.Full):
            self.spool.enqueue(self.frames[1])
        self.run_upload()
        self.spool.enqueue(self.frames[1])
        self.run_upload()
        self.assertEqual(self.spool.pending(), [])
        self.assertLess((self.spool.root/'media.sqlite').stat().st_size, 65536)

    def test_received_does_not_release_without_persisted(self):
        self.enqueue()
        self.server.mode = 'received-only'
        self.run_upload(False)
        self.assertEqual(len(self.spool.pending()), 2)
        self.server.mode = 'success'
        self.run_upload()

    def test_permanent_reject_is_durable_and_stops_replay(self):
        self.enqueue()
        self.server.mode = 'reject'
        self.run_upload(False)
        self.assertEqual(len(self.spool.pending()), 2)
        self.server.mode = 'success'
        before = self.server.credential_calls
        self.run_upload(False)
        self.assertEqual(self.server.credential_calls, before)
        self.assertEqual(len(self.spool.pending()), 2)

    def test_retryable_errors_unknown_ack_and_http_rejection_retain(self):
        self.enqueue()
        for mode in ('http-reject', 'transient', 'unknown', 'oversize'):
            self.server.mode = mode
            self.run_upload(False)
            self.assertEqual(len(self.spool.pending()), 2)
        self.server.mode = 'success'
        self.run_upload()

    def test_expired_credentials_fail_before_media_and_rotate(self):
        self.enqueue()
        self.server.ttl = -1
        self.run_upload(False)
        self.assertEqual(self.server.received, [])
        self.assertEqual(len(self.spool.pending()), 2)
        self.server.ttl, self.server.token = 3600, 'fresh-session'
        self.run_upload()

    def test_active_session_ends_before_credential_expiry(self):
        self.enqueue()
        self.v['session_seconds'] = 20
        self.path.write_text(json.dumps(self.v))
        self.server.ttl = 11
        self.server.mode = 'expiry-hang'
        start = time.monotonic()
        self.run_upload(False)
        elapsed = time.monotonic()-start
        # Server keeps the ACK channel active; idle socket/session(20s) limits
        # cannot explain termination before the credential's 11-second expiry.
        self.assertGreater(elapsed, 4)
        self.assertLess(elapsed, 9)
        self.assertEqual(len(self.spool.pending()), 2)
        self.server.mode = 'success'
        self.server.ttl = 3600
        self.server.token = 'after-expiration'
        self.run_upload()

    def test_killed_uploader_releases_lock_and_recovers(self):
        self.enqueue()
        self.server.mode = 'hang'
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/kvs_video.py'), '--config', str(self.path), 'upload-once'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(self.server.arrived.wait(5))
            proc.kill()
            proc.communicate(timeout=5)
            self.assertEqual(len(self.spool.pending()), 2)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.server.release.set()
        self.server.mode = 'success'
        self.run_upload()

    def test_session_deadline_and_single_uploader(self):
        self.enqueue()
        self.server.mode = 'hang'
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/kvs_video.py'), '--config', str(self.path), 'upload-once'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(self.server.arrived.wait(5))
            self.run_upload(False)  # flock refuses overlapping sessions
            start = time.monotonic()
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 1)
            self.assertLess(time.monotonic()-start, 4)
            self.assertEqual(len(self.spool.pending()), 2)
        finally:
            self.server.release.set()
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_bounded_backpressure_and_ingester_restart(self):
        self.spool.close()
        self.v['max_fragments'] = 1
        self.cfg = video.Config(self.v)
        self.spool = video.Spool(self.cfg)
        self.path.write_text(json.dumps(self.v))
        self.spool.enqueue(self.frames[0])
        with self.assertRaises(video.Full):
            self.spool.enqueue(self.frames[1])
        # Real ingest blocks on a full spool; upload ACK releases the capacity.
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/kvs_video.py'), '--config', str(self.path), 'ingest'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(encode_record(self.frames[1]))
            proc.stdin.flush()
            time.sleep(.4)
            self.assertIsNone(proc.poll())
            self.assertEqual(len(self.spool.pending()), 1)
            self.run_upload()
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn(b'queued', out)
            self.assertEqual(len(self.spool.pending()), 1)
            self.run_upload()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_versions_codec_timestamps_overlap_and_quota(self):
        for changes in ({'version': 2}, {'version': True}, {'surprise': 1}, {'max_spool_bytes': 2048}, {'codec_private_hex': '01'}, {'control_endpoint': 'http://example.org', 'emulator': True}):
            with self.assertRaises((ValueError, TypeError)):
                video.Config(dict(self.v, **changes))
        self.enqueue()
        with self.assertRaises(ValueError):
            self.spool.enqueue(self.frames[0][1:])
        with self.assertRaises(ValueError):
            self.spool.enqueue([(self.frames[0][0][0], True, b'\x00\x00\x00\x05\x65')])
        with self.assertRaises(ValueError):
            self.spool.enqueue([(self.frames[0][0][0], False, self.frames[0][0][2])])
        with self.assertRaises(ValueError):
            video.Spool(video.Config(dict(self.v, stream_name='other')))
        with self.assertRaises(ValueError):
            video.ingest(io.BytesIO(b'\x00\x00\x00\x05abc'), self.spool, io.StringIO())

    def test_corrupt_media_retained(self):
        self.enqueue()
        with self.spool.db:
            self.spool.db.execute("UPDATE fragments SET media=x'0000' WHERE start=(SELECT min(start) FROM fragments)")
        self.run_upload(False)
        self.assertEqual(len(self.spool.pending()), 2)

    def test_truncated_ingest_has_no_commit_and_exact_retry_deduplicates(self):
        raw = encode_record(self.frames[0])
        with self.assertRaises(ValueError):
            video.ingest(io.BytesIO(raw[:-1]), self.spool, io.StringIO())
        self.assertEqual(self.spool.pending(), [])
        video.ingest(io.BytesIO(raw), self.spool, io.StringIO())
        self.spool.close()
        self.spool = video.Spool(self.cfg)
        output = io.StringIO()
        video.ingest(io.BytesIO(raw), self.spool, output)
        self.assertIn('duplicate', output.getvalue())
        self.assertEqual(len(self.spool.pending()), 1)

    def test_annexb_malformed_and_codec_change(self):
        from kvs_h264_source import access_units
        for data in (b'junk', b'\x00\x00\x01\x67bad', b'\x00\x00\x01\x65bad'):
            with self.assertRaises(ValueError):
                list(access_units(io.BytesIO(data), self.cfg))


if __name__ == '__main__':
    unittest.main()
