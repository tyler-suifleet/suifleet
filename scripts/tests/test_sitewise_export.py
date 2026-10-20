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
import sitewise_export as fe


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
            assert self.path == '/properties'
            assert 'Credential=local-test-access/' in self.headers['Authorization']
            assert '/us-east-1/iotsitewise/aws4_request' in self.headers['Authorization']
            assert self.headers['X-Amz-Security-Token'] == 'local-test-session'
            raw = self.rfile.read(int(self.headers['Content-Length']))
            auth = self.headers['Authorization']
            signed = auth.split('SignedHeaders=')[1].split(',')[0]
            scope = auth.split('Credential=')[1].split(',')[0].split('/', 1)[1]
            canonical_headers = ''.join(name + ':' + ' '.join(self.headers[name].split()) + '\n'
                                        for name in signed.split(';'))
            canonical = '\n'.join(('POST', '/properties', '', canonical_headers, signed, hashlib.sha256(raw).hexdigest()))
            string_to_sign = '\n'.join(('AWS4-HMAC-SHA256', self.headers['X-Amz-Date'], scope,
                                        hashlib.sha256(canonical.encode()).hexdigest()))
            key = b'AWS4local-test-secret'
            for part in scope.split('/'):
                key = hmac.new(key, part.encode(), hashlib.sha256).digest()
            expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected, auth.split('Signature=')[1])
            body = json.loads(raw)
            assert body['enablePartialEntryProcessing'] is False
            entries = body['entries']
            assert 1 <= len(entries) <= 10
            for entry in entries:
                assert len(entry['propertyValues']) == 1
                tqv = entry['propertyValues'][0]
                assert tqv['quality'] in ('GOOD', 'BAD', 'UNCERTAIN')
                assert 0 <= tqv['timestamp']['offsetInNanos'] < 1000000000
                assert len(tqv['value']) == 1
            state['requests'].append(entries)
            if state['responses']:
                code, value = state['responses'].pop(0)
                if value == 'disconnect':
                    self.connection.shutdown(2)
                    return
                return self.reply(code, value)
            self.reply(200, dict(errorEntries=[]))

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
        self.exported = -1

    def stream_describe(self, name):
        return dict(persistence='File', export=None, oldest=0, corrupt_segments=0, external_export=True,
                    strategy_on_full='RejectNewData', time_to_live_seconds=None, exported=self.exported)

    def stream_export_ack(self, name, sequence):
        self.exported = max(self.exported, sequence)

    def stream_read(self, name, start):
        if start >= len(self.payloads):
            raise fe.IpcError('not-enough-messages')
        return [dict(sequence=start, payload=self.payloads[start])]


def configuration(directory):
    return dict(version=1, socket='/run/suifleet/ipc.sock', component='exporter', stream='telemetry',
                region='us-east-1', state_dir=str(directory / 'export'),
                mappings=[dict(source='plc', point='temperature', propertyAlias='/plant/temperature', scale=2, offset=1)])


def client(cfg, url):
    # Production factory deliberately offers no endpoint override. Only the test
    # swaps create_client's endpoint to the network-none local emulator.
    import botocore.session
    original = botocore.session.Session.create_client

    def local(session, *args, **kwargs):
        from botocore.config import Config
        kwargs['config'] = kwargs['config'].merge(Config(inject_host_prefix=False))
        return original(session, *args, **kwargs, endpoint_url=url)

    with patch.dict(os.environ, {'AWS_CONTAINER_CREDENTIALS_FULL_URI': url + '/credentials',
                                 'AWS_CONTAINER_AUTHORIZATION_TOKEN': 'local-scoped-token'}), \
            patch.object(botocore.session.Session, 'create_client', local):
        return fe.aws_client(cfg)


def sample(**changes):
    return dict(dict(kind='sample', source='plc', point='temperature', type='float32',
                     value=20, quality='GOOD', timestamp_ns=1700000000123456789), **changes)


def payload(*records):
    return dict(schema=1, collector='industrial', batch='boot:0', records=list(records or [sample()]))


def exercise_native(ipc, directory, restart):
    with emulator() as (state, url):
        cfg = dict(configuration(directory), stream='local-sitewise')
        exporter = fe.Exporter(cfg, ipc, client(cfg, url))
        try:
            for seq in range(3):
                assert exporter.step() == 'exported'
                assert exporter.checkpoint == seq
            assert state['requests'][0][0]['propertyValues'][0]['value'] == {'doubleValue': 1.0}
        finally:
            exporter.close()
        exporter = fe.Exporter(cfg, ipc, client(cfg, url))
        try:
            restart()
            assert exporter.step() == 'exported'
            assert exporter.checkpoint >= 1024
            assert exporter.step() == 'idle'
        finally:
            exporter.close()


class SiteWiseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = configuration(Path(self.temp.name))

    def exporter(self, source, url):
        result = fe.Exporter(self.cfg, source, client(self.cfg, url))
        self.addCleanup(result.close)
        return result

    def test_mapping_types_quality_time_and_chunks(self):
        with emulator() as (state, url):
            e = self.exporter(Source([payload(*[sample() for _ in range(23)])]), url)
            self.assertEqual(e.step(), 'exported')
            self.assertEqual([len(r) for r in state['requests']], [10, 10, 3])
            tqv = state['requests'][0][0]['propertyValues'][0]
            self.assertEqual(tqv, dict(value={'doubleValue': 41.0}, quality='GOOD',
                                     timestamp=dict(timeInSeconds=1700000000, offsetInNanos=123456789)))
            for typ, value, expected in [('boolean', True, {'booleanValue': True}),
                                         ('bool', False, {'booleanValue': False}),
                                         ('string', 'ready', {'stringValue': 'ready'}),
                                         ('float', None, {'nullValue': {'valueType': 'D'}})]:
                m = e.cfg['mappings'][0]
                m.pop('scale', None); m.pop('offset', None)
                record = sample(type=typ, value=value, quality='BAD')
                self.assertEqual(e.entries(payload(record))[0]['propertyValues'][0]['value'], expected)
            m.pop('propertyAlias')
            m.update(assetId='12345678-1234-1234-1234-123456789abc', propertyId='12345678-1234-1234-1234-123456789abd')
            entry = e.entries(payload())[0]
            self.assertIn('assetId', entry)
            self.assertNotIn('propertyAlias', entry)

    def test_partial_rejection_reopen_retains_whole_message(self):
        with emulator() as (state, url):
            source = Source([payload(*[sample() for _ in range(11)])])
            e = self.exporter(source, url)
            state['responses'] = [(200, {'errorEntries': []}), (200, {'errorEntries': [
                {'entryId': '10', 'errors': [{'errorCode': 'TimestampOutOfRangeException'}]}]})]
            self.assertEqual(e.step(), 'partial')
            self.assertEqual(e.checkpoint, -1)
            e.close()
            e = self.exporter(source, url)
            self.assertEqual(e.step(), 'exported')
            self.assertEqual(e.checkpoint, 0)
            self.assertEqual([len(r) for r in state['requests']], [10, 1, 10, 1])
            e.close()
            e = self.exporter(source, url)
            self.assertEqual(e.step(), 'idle')

    def test_outage_denied_expired_disconnect_and_malformed(self):
        cases = [(503, {}), (403, {'code': 'AccessDeniedException'}),
                 (400, {'code': 'ExpiredTokenException'}), (200, 'disconnect'),
                 (200, {}), (200, {'errorEntries': [{'entryId': 'unknown', 'errors': [{'errorCode': 'InvalidRequestException'}]}]}),
                 (200, {'errorEntries': [{'entryId': '0', 'errors': []}]})]
        with emulator() as (state, url):
            e = self.exporter(Source([payload()]), url)
            for case in cases:
                state['responses'] = [case]
                with self.assertRaises(Exception):
                    e.step()
                self.assertEqual(e.checkpoint, -1)
            self.assertEqual(e.step(), 'exported')

    def test_invalid_records_never_sent(self):
        records = [sample(value=True), sample(value=float('nan')), sample(value=2**54),
                   sample(timestamp_ns=True), sample(timestamp_ns=0), sample(quality='UNKNOWN'),
                   sample(source='other'), sample(type='int16', value=1.5), sample(type='uint16', value=-1), sample(value=None), {'kind': 'gap'}, sample(type='unknown')]
        with emulator() as (state, url):
            e = self.exporter(Source([]), url)
            for record in records:
                e.ipc = Source([payload(sample(), record)])
                with self.assertRaises(fe.ExportError):
                    e.step()
                self.assertEqual(e.checkpoint, -1)
            self.assertEqual(state['requests'], [])
            e.ipc = Source([payload({'kind': 'status', 'state': 'connected'}, sample())])
            self.assertEqual(e.step(), 'exported')
            self.assertEqual(len(state['requests'][0]), 1)
            e.ipc = Source([payload({'kind': 'status', 'state': 'connected'})])
            e.ipc.payloads.insert(0, payload())
            self.assertEqual(e.step(), 'exported')
            self.assertEqual(len(state['requests']), 1)

    def test_state_lock_binding_corruption_fsync(self):
        with emulator() as (state, url):
            e = self.exporter(Source([payload()]), url)
            with self.assertRaises(BlockingIOError):
                fe.Exporter(self.cfg, e.ipc, e.sitewise)
            with patch.object(fe.os, 'fsync', side_effect=OSError()):
                with self.assertRaises(OSError):
                    e.step()
            self.assertEqual(e.checkpoint, -1)
            self.assertEqual(e.step(), 'exported')
            e.close()
            changed = dict(self.cfg, region='us-west-2')
            with self.assertRaises(ValueError):
                fe.Exporter(changed, e.ipc, e.sitewise)
            e.path.write_text('{}')
            with self.assertRaises(ValueError):
                fe.Exporter(self.cfg, e.ipc, e.sitewise)

    def test_scoped_credentials_only_and_refresh_failure(self):
        with patch.dict(os.environ, {'AWS_CONTAINER_CREDENTIALS_FULL_URI': '', 'AWS_ACCESS_KEY_ID': 'ambient',
                                     'AWS_SECRET_ACCESS_KEY': 'ambient'}):
            with self.assertRaises(ValueError):
                fe.aws_client(self.cfg)
        with emulator() as (state, url):
            e = self.exporter(Source([payload()]), url)
            from datetime import datetime, timedelta, timezone
            e.sitewise._request_signer._credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
            state['credentials'] = False
            with self.assertRaises(Exception):
                e.step()
            self.assertEqual(e.checkpoint, -1)
            self.assertEqual(state['requests'], [])
            state['credentials'] = True
            self.assertEqual(e.step(), 'exported')

    def test_configuration_retention_and_sequence_gaps(self):
        import copy
        for field, value in [('version', True), ('version', 2), ('mappings', []), ('socket', 'relative')]:
            with self.assertRaises(ValueError):
                fe.config(dict(self.cfg, **{field: value}))
        for change in [dict(propertyAlias='bad\x00'), dict(scale=True), dict(assetId='bad'), dict(offset=float('inf'))]:
            bad = copy.deepcopy(self.cfg)
            bad['mappings'][0].update(change)
            with self.assertRaises(ValueError):
                fe.config(bad)
        with emulator() as (state, url):
            e = self.exporter(Source([payload()]), url)
            e.ipc.stream_describe = lambda name: dict(persistence='File', export=None, oldest=1, corrupt_segments=0)
            with self.assertRaisesRegex(fe.ExportError, 'requires-lossless'):
                e.step()
            e.ipc.stream_describe = lambda name: dict(persistence='File', export=None, oldest=0, corrupt_segments=1, external_export=True, strategy_on_full='RejectNewData', time_to_live_seconds=None)
            with self.assertRaisesRegex(fe.ExportError, 'corrupt-stream'):
                e.step()
            e.ipc = Source([payload()])
            e.ipc.stream_read = lambda name, start: [dict(sequence=1024, payload=payload())] if start <= 1024 else []
            self.assertEqual(e.step(), 'exported')
            self.assertEqual(e.checkpoint, 1024)

    def test_lost_ack_repaired_without_resend(self):
        with emulator() as (state, url):
            source = Source([payload()])
            e = self.exporter(source, url)
            with patch.object(source, 'stream_export_ack', side_effect=fe.IpcError('unavailable')):
                with self.assertRaises(fe.IpcError):
                    e.step()
            self.assertEqual(e.checkpoint, 0)
            e.close()
            e = self.exporter(source, url)
            self.assertEqual(e.step(), 'idle')
            self.assertEqual(source.exported, 0)
            self.assertEqual(len(state['requests']), 1)

if __name__ == '__main__':
    unittest.main()
