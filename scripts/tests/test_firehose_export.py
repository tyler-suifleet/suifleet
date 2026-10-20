"""Actual signed SDK HTTP requests to a local Firehose/credential emulator."""
import base64
import hashlib
import hmac
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import firehose_export as fe


@contextmanager
def emulator():
    state = dict(requests=[], responses=[], credentials=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/x-amz-json-1.1')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not state['credentials']:
                return self.reply(503, {})
            if self.headers.get('Authorization') != 'local-scoped-token':
                return self.reply(403, {})
            self.reply(200, dict(AccessKeyId='local-test-access', SecretAccessKey='local-test-secret',
                                Token='local-test-session',
                                Expiration=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))

        def do_POST(self):
            assert self.headers['X-Amz-Target'] == 'Firehose_20150804.PutRecordBatch'
            assert 'Credential=local-test-access/' in self.headers['Authorization']
            assert '/us-east-1/firehose/aws4_request' in self.headers['Authorization']
            assert self.headers['X-Amz-Security-Token'] == 'local-test-session'
            raw = self.rfile.read(int(self.headers['Content-Length']))
            auth = self.headers['Authorization']
            signed = auth.split('SignedHeaders=')[1].split(',')[0]
            scope = auth.split('Credential=')[1].split(',')[0].split('/', 1)[1]
            canonical_headers = ''.join(name + ':' + ' '.join(self.headers[name].split()) + '\n'
                                        for name in signed.split(';'))
            canonical = '\n'.join(('POST', '/', '', canonical_headers, signed, hashlib.sha256(raw).hexdigest()))
            string_to_sign = '\n'.join(('AWS4-HMAC-SHA256', self.headers['X-Amz-Date'], scope,
                                        hashlib.sha256(canonical.encode()).hexdigest()))
            key = b'AWS4local-test-secret'
            for part in scope.split('/'):
                key = hmac.new(key, part.encode(), hashlib.sha256).digest()
            expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected, auth.split('Signature=')[1])
            body = json.loads(raw)
            assert body['DeliveryStreamName'] == 'delivery'
            records = [base64.b64decode(item['Data'], validate=True) for item in body['Records']]
            assert 1 <= len(records) <= 500
            assert all(0 < len(record) <= fe.MAX_RECORD for record in records)
            assert sum(map(len, records)) <= fe.MAX_BATCH
            state['requests'].append(records)
            if state['responses']:
                code, value = state['responses'].pop(0)
                if value == 'disconnect':
                    self.connection.shutdown(2)
                    return
                return self.reply(code, value)
            self.reply(200, dict(FailedPutCount=0, RequestResponses=[dict(RecordId=str(i)) for i in range(len(records))]))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Source:
    def __init__(self, payloads):
        self.payloads = payloads

    def stream_describe(self, name):
        return dict(persistence='File', export=None, oldest=0, corrupt_segments=0)

    def stream_read(self, name, start):
        if start >= len(self.payloads):
            raise fe.IpcError('not-enough-messages')
        return [dict(sequence=start, payload=self.payloads[start])]


def configuration(directory):
    return dict(version=1, socket='/run/suifleet/ipc.sock', component='exporter', stream='telemetry',
                region='us-east-1', delivery_stream='delivery', state_dir=str(directory / 'export'))


def client(cfg, url):
    # Production factory deliberately offers no endpoint override. Only the test
    # swaps create_client's endpoint to the network-none local emulator.
    import botocore.session
    original = botocore.session.Session.create_client

    def local(session, *args, **kwargs):
        return original(session, *args, **kwargs, endpoint_url=url)

    with patch.dict(os.environ, {'AWS_CONTAINER_CREDENTIALS_FULL_URI': url + '/credentials',
                                 'AWS_CONTAINER_AUTHORIZATION_TOKEN': 'local-scoped-token'}), \
            patch.object(botocore.session.Session, 'create_client', local):
        return fe.aws_client(cfg)


def exercise_native(ipc, directory, restart):
    """Called by the real daemon stream suite with its actual peer-UID client."""
    with emulator() as (state, url):
        cfg = dict(configuration(directory), stream='local-firehose')
        exporter = fe.Exporter(cfg, ipc, client(cfg, url))
        try:
            assert exporter.step() == 'exported'
            assert exporter.checkpoint == 2
            assert state['requests'] == [[b'{"native":0}\n', b'{"native":1}\n', b'{"native":2}\n']]
        finally:
            exporter.close()
        exporter = fe.Exporter(cfg, ipc, client(cfg, url))
        try:
            restart()
            assert exporter.step() == 'exported'
            assert exporter.checkpoint >= 1024
            assert state['requests'][-1] == [b'{"native":3}\n']
            assert exporter.step() == 'idle'
        finally:
            exporter.close()


class FirehoseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = configuration(Path(self.temp.name))

    def exporter(self, source, url):
        result = fe.Exporter(self.cfg, source, client(self.cfg, url))
        self.addCleanup(result.close)
        return result

    def test_partial_restart_and_duplicate_suffix(self):
        with emulator() as (state, url):
            state['responses'] = [(200, dict(FailedPutCount=1, RequestResponses=[
                dict(RecordId='first'), dict(ErrorCode='InternalFailure'), dict(RecordId='third')]))]
            source = Source([{'n': i} for i in range(3)])
            exporter = self.exporter(source, url)
            self.assertEqual(exporter.step(), 'partial')
            self.assertEqual(exporter.checkpoint, 0)
            exporter.close()
            resumed = self.exporter(source, url)
            self.assertEqual(resumed.step(), 'exported')
            self.assertEqual(resumed.checkpoint, 2)
            self.assertEqual(state['requests'][1], state['requests'][0][1:])
            self.assertEqual(resumed.step(), 'idle')

    def test_process_restart_reads_durable_partial_checkpoint(self):
        with emulator() as (state, url):
            state['responses'] = [(200, dict(FailedPutCount=1, RequestResponses=[
                dict(RecordId='first'), dict(ErrorCode='InternalFailure')]))]
            code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from test_firehose_export import fe, Source, client
cfg = json.loads(sys.argv[2])
exporter = fe.Exporter(cfg, Source([0, 1]), client(cfg, sys.argv[3]))
assert exporter.step() == 'partial'
assert exporter.checkpoint == 0
# Abrupt process exit after persisted partial success; no clean shutdown hook.
import os
os._exit(0)
"""
            subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts/tests'),
                            json.dumps(self.cfg), url], check=True, timeout=30)
            exporter = self.exporter(Source([0, 1]), url)
            self.assertEqual(exporter.checkpoint, 0)
            self.assertEqual(exporter.step(), 'exported')
            self.assertEqual(state['requests'], [[b'0\n', b'1\n'], [b'1\n']])

    def test_outage_expiry_ambiguous_and_malformed_retain(self):
        with emulator() as (state, url):
            exporter = self.exporter(Source(['payload']), url)
            for code, response in [(503, {'__type': 'ServiceUnavailableException'}),
                                   (400, {'__type': 'ExpiredTokenException'}),
                                   (200, 'disconnect'),
                                   (200, dict(FailedPutCount=0, RequestResponses=[])),
                                   (200, dict(FailedPutCount=1, RequestResponses=[dict(RecordId='x')])),
                                   (200, dict(FailedPutCount=0, RequestResponses=[{}]))]:
                state['responses'].append((code, response))
                with self.assertRaises(Exception):
                    exporter.step()
                self.assertEqual(exporter.checkpoint, -1)
                self.assertFalse(exporter.path.exists())
            # Mandatory refresh fails closed when credential endpoint is down.
            exporter.firehose._request_signer._credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
            state['credentials'] = False
            before = len(state['requests'])
            with self.assertNoLogs(), self.assertRaises(Exception):
                exporter.step()
            self.assertEqual(len(state['requests']), before)
            state['credentials'] = True
            with patch.dict(os.environ, {'AWS_CONTAINER_AUTHORIZATION_TOKEN': 'local-scoped-token'}):
                self.assertEqual(exporter.step(), 'exported')
            self.assertEqual(exporter.checkpoint, 0)

    def test_batch_record_limits(self):
        with emulator() as (state, url):
            source = Source(['x'] * 501)
            exporter = self.exporter(source, url)
            exporter.step()
            self.assertEqual(len(state['requests'][0]), 500)
            exporter.step()
            self.assertEqual(len(state['requests'][1]), 1)
            exporter.close()
        # Byte limit with serialized newline accounting.
        self.cfg['state_dir'] += '-bytes'
        with emulator() as (state, url):
            exporter = self.exporter(Source(['x' * (fe.MAX_RECORD - 3)] * 5), url)
            exporter.step()
            self.assertEqual(len(state['requests'][0]), 4)
            exporter.step()
            self.assertEqual(len(state['requests'][1]), 1)
            exporter.close()
        self.cfg['state_dir'] += '-oversize'
        with emulator() as (state, url):
            exporter = self.exporter(Source(['x' * fe.MAX_RECORD]), url)
            with self.assertRaisesRegex(ValueError, 'record-too-large'):
                exporter.step()
            self.assertEqual(state['requests'], [])

    def test_state_lock_binding_corruption_and_failed_sync(self):
        with emulator() as (state, url):
            exporter = self.exporter(Source([1]), url)
            with self.assertRaises(BlockingIOError):
                fe.Exporter(self.cfg, Source([1]), client(self.cfg, url))
            with patch.object(fe.os, 'fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    exporter.step()
            self.assertEqual(exporter.checkpoint, -1)
            exporter.step()
            exporter.close()
            changed = dict(self.cfg, delivery_stream='different')
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                fe.Exporter(changed, Source([1]), client(changed, url))
            exporter.path.write_text('{bad-json')
            with self.assertRaises(ValueError):
                fe.Exporter(self.cfg, Source([1]), client(self.cfg, url))

    def test_version_credentials_and_redacted_diagnostics(self):
        for value in [True, 0, 2, '1']:
            with self.assertRaises(ValueError):
                fe.config(dict(self.cfg, version=value))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'scoped credentials'):
                fe.aws_client(self.cfg)
        self.assertEqual(fe.diagnostic(ValueError('secret payload')), 'retry-pending')
        self.assertEqual(fe.diagnostic(fe.ExportError('record-too-large')), 'record-too-large')
        self.assertEqual(fe.diagnostic(fe.IpcError('evicted')), 'evicted')

    def test_reservation_gaps_partial_retry_and_fresh_state(self):
        source = Source([0, 1, 2, 1024, 1025])
        def read(name, start):
            for sequence in source.payloads:
                if sequence >= start:
                    return [dict(sequence=sequence, payload=sequence)]
            raise fe.IpcError('not-enough-messages')
        source.stream_read = read
        with emulator() as (state, url):
            exporter = self.exporter(source, url)
            state['responses'] = [(200, dict(FailedPutCount=1, RequestResponses=[
                dict(RecordId='ok'), dict(RecordId='ok'), dict(RecordId='ok'),
                dict(RecordId='ok'), dict(ErrorCode='failure')]))]
            self.assertEqual(exporter.step(), 'partial')
            self.assertEqual(exporter.checkpoint, 1024)
            exporter.close()
            resumed = self.exporter(source, url)
            self.assertEqual(resumed.step(), 'exported')
            self.assertEqual(resumed.checkpoint, 1025)
            self.assertEqual(state['requests'][-1], [b'1025\n'])
            self.assertEqual(resumed.step(), 'idle')

    def test_loss_and_unsupported_record_preserve_prefix(self):
        for condition in ('evicted', 'corrupt-stream', 'unsupported-payload', 'record-too-large'):
            with self.subTest(condition=condition), emulator() as (state, url):
                cfg = dict(self.cfg, state_dir=self.cfg['state_dir'] + condition)
                source = Source([0, 1])
                def read(name, start):
                    if start == 0:
                        return [dict(sequence=0, payload=0)]
                    if condition == 'evicted':
                        raise fe.IpcError('evicted')
                    if condition == 'corrupt-stream':
                        raise fe.ExportError('corrupt-stream')
                    if condition == 'unsupported-payload':
                        return [dict(sequence=1, data_base64='AA==')]
                    return [dict(sequence=1, payload='x' * fe.MAX_RECORD)]
                source.stream_read = read
                exporter = fe.Exporter(cfg, source, client(cfg, url))
                self.addCleanup(exporter.close)
                with self.assertRaisesRegex(ValueError if condition != 'evicted' else fe.IpcError,
                                            condition):
                    exporter.step()
                self.assertEqual(exporter.checkpoint, 0)
                self.assertEqual(state['requests'], [[b'0\n']])
                with self.assertRaises(Exception):
                    exporter.step()
                self.assertEqual(state['requests'], [[b'0\n']])

    def test_describe_loss_and_corruption_fail_closed(self):
        with emulator() as (state, url):
            source = Source([0])
            exporter = self.exporter(source, url)
            for fields, code in [({'oldest': 1}, 'evicted'), ({'corrupt_segments': 1}, 'corrupt-stream')]:
                source.stream_describe = lambda name: dict(persistence='File', export=None,
                    **dict(dict(oldest=0, corrupt_segments=0), **fields))
                with self.assertRaisesRegex(fe.ExportError, code):
                    exporter.step()
                self.assertEqual(state['requests'], [])


if __name__ == '__main__':
    unittest.main()
