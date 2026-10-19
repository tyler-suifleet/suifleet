#!/usr/bin/env python3
"""Actual boto3 HTTP/XML/JSON protocols + real daemon durable IPC, no AWS network."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'sdk/python'), str(ROOT / '.ci-cache/aws-export-python')]
from defusedxml import ElementTree as ET
from suifleet_stream_export import Exporter, validate
from suifleet_ipc import Client, IpcError
from test_ota import Simulator, start_grpc, wait_for, PACKAGE, REGISTRY, DEVICE, CAP, UPGRADE_CAP, TYPE_ORIGINS


class Emulator:
    def __init__(self):
        self.records, self.uploads, self.objects, self.calls = [], {}, {}, []
        self.mode, self.counter, self.signatures = 'ok', 0, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status=200, body=b'', headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, str(value))
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def error(self, code, status=404):
                self.reply(status, ('<Error><Code>' + code + '</Code></Error>').encode())

            def do_GET(self):
                if self.path == '/credentials':
                    self.reply(body=json.dumps(dict(AccessKeyId='LOCALACCESS', SecretAccessKey='local-secret',
                                                    Token='local-token', Expiration='2099-01-01T00:00:00Z')).encode())
                    return
                self.execute()

            def do_HEAD(self):
                self.execute()

            def do_PUT(self):
                self.execute()

            def do_POST(self):
                self.execute()

            def do_DELETE(self):
                self.execute()

            def execute(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.calls.append((self.command, parsed.path, query, body))
                signature = self.headers.get('Authorization', '')
                owner.signatures.append(signature)
                if (not signature.startswith('AWS4-HMAC-SHA256 Credential=LOCALACCESS/')
                        or self.headers.get('X-Amz-Security-Token') != 'local-token'):
                    self.error('AccessDenied', 403)
                    return
                if owner.mode == 'expired' and self.headers.get('X-Amz-Target'):
                    self.reply(400, b'{"__type":"ExpiredTokenException","message":"expired"}')
                    return
                if owner.mode == 'denied':
                    self.error('AccessDenied', 403)
                    return
                if self.headers.get('X-Amz-Target', '').endswith('PutRecords'):
                    request = json.loads(body)
                    assert request['StreamName'] == 'target'
                    results = []
                    for index, record in enumerate(request['Records']):
                        payload = json.loads(base64.b64decode(record['Data']))
                        assert len(record['PartitionKey']) == 64
                        if owner.mode == 'partial' and index == 1:
                            results.append(dict(ErrorCode='ProvisionedThroughputExceededException', ErrorMessage='retry'))
                        else:
                            owner.records.append(payload)
                            results.append(dict(SequenceNumber=str(len(owner.records)), ShardId='shardId-000'))
                    if owner.mode == 'malformed':
                        self.reply(body=b'{"Records":[],"FailedRecordCount":0}')
                        return
                    if owner.mode == 'lost-kinesis':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.reply(body=json.dumps(dict(FailedRecordCount=sum('ErrorCode' in r for r in results), Records=results)).encode())
                    return
                key = parsed.path
                if self.command == 'HEAD':
                    if key not in owner.objects:
                        self.error('NoSuchKey')
                    else:
                        data, digest = owner.objects[key]
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(data)))
                        self.send_header('x-amz-meta-sha256', digest)
                        self.end_headers()
                elif self.command == 'GET' and 'uploads' in query:
                    uploads = ''.join('<Upload><Key>' + k.removeprefix('/bucket/') +
                                      '</Key><UploadId>' + uid + '</UploadId></Upload>'
                                      for uid, (k, _, _) in owner.uploads.items())
                    self.reply(body=('<ListMultipartUploadsResult><IsTruncated>false</IsTruncated>' +
                                     uploads + '</ListMultipartUploadsResult>').encode())
                elif self.command == 'POST' and 'uploads' in query:
                    owner.counter += 1
                    uid = str(owner.counter)
                    owner.uploads[uid] = (key, {}, self.headers['x-amz-meta-sha256'])
                    if owner.mode == 'lost-create':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.reply(body=('<InitiateMultipartUploadResult><UploadId>' + uid +
                                     '</UploadId></InitiateMultipartUploadResult>').encode())
                elif 'uploadId' in query:
                    uid = query['uploadId'][0]
                    if uid not in owner.uploads:
                        self.error('NoSuchUpload')
                        return
                    key, parts, digest = owner.uploads[uid]
                    if self.command == 'DELETE':
                        del owner.uploads[uid]
                        self.reply(204)
                    elif self.command == 'PUT':
                        number = int(query['partNumber'][0])
                        if owner.mode == 'fail-part2' and number == 2:
                            self.error('InternalError', 500)
                            return
                        tag = '"' + hashlib.md5(body).hexdigest() + '"'
                        parts[number] = (body, tag)
                        if owner.mode == 'lost-part':
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                        self.reply(headers={'ETag': tag})
                    elif self.command == 'POST':
                        if owner.mode == 'embedded-error':
                            self.reply(body=b'<Error><Code>InternalError</Code><Message>retry</Message></Error>')
                            return
                        completed = ET.fromstring(body)
                        requested = []
                        for part in completed:
                            fields = {element.tag.split('}')[-1]: element.text for element in part}
                            number = int(fields['PartNumber'])
                            assert fields['ETag'] == parts[number][1]
                            requested.append(number)
                        assert requested == list(range(1, len(parts) + 1))
                        data = b''.join(parts[i][0] for i in requested)
                        assert hashlib.sha256(data).hexdigest() == digest
                        owner.objects[key] = (data, digest)
                        del owner.uploads[uid]
                        if owner.mode == 'lost-complete':
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                        self.reply(body=b'<CompleteMultipartUploadResult><ETag>"complete"</ETag></CompleteMultipartUploadResult>')
                else:
                    self.error('Unsupported', 400)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ActualExporters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getuid() != 0:
            raise RuntimeError('run in disposable root network-none container')
        cls.temporary = tempfile.TemporaryDirectory(prefix='sf058-daemon-')
        cls.root = Path(cls.temporary.name)
        cls.root.chmod(0o755)
        # Dispatcher checkouts are private. Copy only public SDK code/dependencies
        # into this disposable runtime so distinct test UIDs can execute them.
        cls.library = cls.root / 'public-sdk'
        shutil.copytree(ROOT / '.ci-cache/aws-export-python', cls.library)
        for name in ('suifleet_ipc.py', 'suifleet_stream_export.py'):
            shutil.copyfile(ROOT / 'sdk/python' / name, cls.library / name)
        for entry in [cls.library, *cls.library.rglob('*')]:
            entry.chmod(0o755 if entry.is_dir() else 0o644)
        cls.sim = Simulator()
        sim = cls.sim

        class Chain(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    result = dict(jsonrpc='2.0', id=request['id'], result=sim.rpc(request['method'], request['params']))
                except Exception as error:
                    result = dict(jsonrpc='2.0', id=request['id'], error=dict(code=-32000, message=str(error)))
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.chain = ThreadingHTTPServer(('127.0.0.1', 0), Chain)
        threading.Thread(target=cls.chain.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{cls.chain.server_port}'
        cls.shim, rpc = start_grpc(url)
        key = cls.root / 'device.key'
        key.write_text('07' * 32)
        key.chmod(0o600)
        (cls.root / 'deployment.json').write_text(json.dumps(dict(schema_version=1, protocol_version=1, network='localnet',
            chain_identifier=sim.chain, rpc_url=rpc, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP,
            package_version='3', type_origins=TYPE_ORIGINS,
            shared_objects={'registry': {'id': REGISTRY, 'initial_shared_version': '1'}},
            supported_clients={'dapp': {'min': '0.0.1', 'max_exclusive': '0.1.0'},
                               'edge': {'min': '0.1.0', 'max_exclusive': '0.3.0'}},
            walrus={'publisher_url': url, 'aggregator_url': url})))
        cls.socket = cls.root / 'ipc.sock'
        cls.config_path = cls.root / 'config.toml'
        cls.config_path.write_text('[sui]\nmanifest_path = "deployment.json"\n' +
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n' +
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n' +
            f'[runtime]\nstate_dir = "{cls.root}/state"\npoll_seconds = 1\n' +
            f'[ipc]\nsocket = "{cls.socket}"\nadministrators = [0]\n' +
            '[ipc.components.untrusted]\nuid = 62003\noperations = ["stream_read", "stream_describe"]\nstreams = ["*"]\n' +
            '[ipc.components.exporter]\nuid = 62004\noperations = ["stream_read", "stream_describe", "stream_export_ack"]\nstreams = ["*"]\n' +
            '[streams]\nmax_streams = 32\nmax_total_bytes = 536870912\n' +
            ''.join(f'[[streams.definitions]]\nname = "test{i}"\nmax_size = 16777216\n' +
                    'strategy_on_full = "RejectNewData"\nexternal_export = true\ndiscard_exported = true\n'
                    for i in range(24)))
        cls.log = (cls.root / 'daemon.log').open('a')
        cls.start()
        cls.index = 0

    @classmethod
    def start(cls):
        cls.daemon = subprocess.Popen([str(ROOT / 'suifleet-edge/target/debug/suifleet')],
            env={**os.environ, 'EDGE_CONFIG': str(cls.config_path)}, stdout=cls.log, stderr=cls.log)
        wait_for(lambda: cls.socket.exists(), 'no IPC socket', cls.daemon)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(timeout=30)
        cls.shim.terminate()
        cls.shim.wait(timeout=10)
        cls.shim.stdout.close()
        cls.chain.shutdown()
        cls.chain.server_close()
        cls.log.close()
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sf058-export-')
        self.emu = Emulator()
        self.previous_env = dict(os.environ)
        for name in list(os.environ):
            if name.startswith('AWS_'):
                del os.environ[name]
        os.environ.update(AWS_CONTAINER_CREDENTIALS_FULL_URI=self.emu.url + '/credentials',
                          AWS_EC2_METADATA_DISABLED='true')
        self.source = Client(str(self.socket))
        self.stream = 'test' + str(type(self).index)
        type(self).index += 1
        self.config = dict(service='kinesis', region='us-east-1', stream=self.stream, component='exporter',
                           socket=str(self.socket), state_dir=self.temp.name, destination='target', source_id='device-fixture',
                           batch_messages=500, batch_bytes=5 << 20, bytes_per_second=1 << 30,
                           poll_seconds=1, max_backoff_seconds=8, endpoint=self.emu.url)
        self.delays = []
        self.exporter = None

    def tearDown(self):
        if self.exporter:
            self.exporter.lock.close()
        os.environ.clear()
        os.environ.update(self.previous_env)
        self.emu.close()
        self.temp.cleanup()

    def open(self, s3=False):
        if self.exporter:
            self.exporter.lock.close()
        if s3:
            self.config.update(service='s3', destination='bucket/exclusive/', batch_bytes=16 << 20)
        self.exporter = Exporter(self.config, sleep=self.delays.append)
        return self.exporter

    def append(self, values):
        return [self.source.stream_append(self.stream, value) for value in values]

    def checkpoint(self):
        return self.source.stream_describe(self.stream)['exported']

    def test_partial_batch_restart_only_retries_failed_records(self):
        self.append([{'n': n} for n in range(3)])
        exporter = self.open()
        self.emu.mode = 'partial'
        with self.assertRaises(RuntimeError):
            exporter.step()
        self.assertEqual(self.checkpoint(), -1)
        self.assertEqual([r['payload'] for r in self.emu.records], [{'n': 0}, {'n': 2}])
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual([r['payload'] for r in self.emu.records], [{'n': 0}, {'n': 2}, {'n': 1}])
        self.assertEqual(self.checkpoint(), 2)
        self.assertEqual(self.source.stream_describe(self.stream)['messages'], 0)

    def test_lost_kinesis_response_duplicates_identifiable_not_lost(self):
        self.append(['a', 'b'])
        self.emu.mode = 'lost-kinesis'
        with self.assertRaises(Exception):
            self.open().step()
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual([r['sequence'] for r in self.emu.records], [0, 1, 0, 1])
        self.assertEqual(self.checkpoint(), 1)

    def test_permissions_and_limits_do_not_ack(self):
        self.append(['first', 'second', 'third'])
        self.config['batch_messages'] = 2
        self.emu.mode = 'denied'
        with self.assertRaises(Exception):
            self.open().step()
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual(self.checkpoint(), 1)
        self.assertEqual(len(self.emu.records), 2)
        self.assertGreater(sum(self.delays), 0)
        self.assertTrue(self.exporter.step())
        self.assertEqual(self.checkpoint(), 2)
        self.assertTrue(all('/us-east-1/kinesis/aws4_request' in s for s in self.emu.signatures))

    def test_local_ack_lost_does_not_resend_cloud(self):
        self.append(['once'])
        exporter = self.open()
        original = exporter.source.stream_export_ack
        def lost(*args):
            original(*args)
            raise OSError('lost local reply')
        exporter.source.stream_export_ack = lost
        with self.assertRaises(OSError):
            exporter.step()
        self.assertTrue(self.open().step())
        self.assertEqual(len(self.emu.records), 1)

    def test_s3_multipart_resume_after_process_and_daemon_crash(self):
        # 120 IPC-sized records: >5 MiB, exactly two multipart parts.
        values = [{'n': i, 'data': 'x' * 48000} for i in range(120)]
        self.append(values)
        self.emu.mode = 'fail-part2'
        with self.assertRaises(Exception):
            self.open(s3=True).step()
        self.assertEqual(self.checkpoint(), -1)
        self.assertEqual(len(self.exporter.state['pending']['parts']), 1)
        type(self).daemon.kill()
        type(self).daemon.wait(timeout=30)
        self.socket.unlink(missing_ok=True)
        type(self).start()
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual(self.checkpoint(), 119)
        objects = list(self.emu.objects.values())
        self.assertEqual(len(objects), 1)
        self.assertEqual([json.loads(line)['payload'] for line in objects[0][0].splitlines()], values)
        part1 = [call for call in self.emu.calls if call[2].get('partNumber') == ['1']]
        self.assertEqual(len(part1), 1, 'durably acknowledged part must not be resent')
        self.assertEqual(len(part1[0][3]), 5 << 20, 'non-final S3 part minimum')
        sequence = self.source.stream_append(self.stream, {'after': 'crash'})
        self.assertGreater(sequence, 119)
        self.assertTrue(self.exporter.step())
        last_body = next(data for data, _ in self.emu.objects.values() if b'after' in data)
        last_record = json.loads(last_body)
        self.assertEqual(last_record['sequence_gap_before'], sequence - 120)
        self.assertEqual(self.checkpoint(), sequence)

    def test_s3_lost_complete_reconciles_object(self):
        self.append([{'real': 'payload'}])
        self.emu.mode = 'lost-complete'
        with self.assertRaises(Exception):
            self.open(s3=True).step()
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual(len(self.emu.objects), 1)
        self.assertEqual(self.emu.counter, 1)
        self.assertEqual(self.checkpoint(), 0)

    def test_s3_lost_initiation_abort_cleanup_then_resume(self):
        self.append(['retained'])
        self.emu.mode = 'lost-create'
        with self.assertRaises(Exception):
            self.open(s3=True).step()
        self.assertEqual(len(self.emu.uploads), 1)
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.emu.uploads['foreign'] = ('/bucket/exclusive/unrelated', {}, 'other')
        self.open().abort()
        self.assertEqual(set(self.emu.uploads), {'foreign'}, 'cleanup must not touch other keys')
        self.assertIsNotNone(self.exporter.state['pending'])
        self.assertTrue(self.exporter.step())
        self.assertEqual(self.checkpoint(), 0)

    def test_s3_lost_part_response_reuploads_same_part(self):
        self.append(['content'])
        self.emu.mode = 'lost-part'
        with self.assertRaises(Exception):
            self.open(s3=True).step()
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())
        self.assertEqual(len(self.emu.objects), 1)
        self.assertEqual(self.emu.counter, 1)

    def test_s3_http200_embedded_error_is_failure(self):
        self.append(['keep'])
        self.emu.mode = 'embedded-error'
        with self.assertRaises(Exception):
            self.open(s3=True).step()
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())

    def test_config_binding_and_single_writer(self):
        self.append(['data'])
        exporter = self.open()
        with self.assertRaises(BlockingIOError):
            Exporter(self.config, sleep=self.delays.append)
        self.emu.mode = 'denied'
        with self.assertRaises(Exception):
            exporter.step()
        exporter.lock.close()
        self.config['destination'] = 'other'
        with self.assertRaises(ValueError):
            self.open()
        self.assertEqual(self.checkpoint(), -1)

    def test_untrusted_component_cannot_ack_or_select_export(self):
        code = """import os,sys
os.setgroups([]); os.setgid(62003); os.setuid(62003)
sys.path.insert(0, sys.argv[1])
from suifleet_ipc import Client,IpcError
client=Client(sys.argv[2], 'untrusted')
try:
    client.stream_export_ack(sys.argv[3], 0)
except IpcError as error:
    assert str(error)=='request_failed'
else:
    raise AssertionError('untrusted ack allowed')
try:
    client.stream_create('forged', 4096)
except IpcError as error:
    assert str(error)=='request_failed'
else:
    raise AssertionError('untrusted create allowed')
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.library), str(self.socket), self.stream],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.checkpoint(), -1)

    def test_cli_sigkill_restart_with_dedicated_uid(self):
        self.append([{'cli': 1}, {'cli': 2}])
        self.config['state_dir'] = str(Path(self.temp.name) / 'private')
        Path(self.temp.name).chmod(0o755)
        private = Path(self.config['state_dir'])
        private.mkdir(mode=0o700)
        os.chown(private, 62004, 62004)
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        config.chmod(0o644)
        self.emu.mode = 'lost-kinesis'
        def drop():
            os.setgroups([])
            os.setgid(62004)
            os.setuid(62004)
        command = [sys.executable, str(self.library / 'suifleet_stream_export.py'), '--config', str(config)]
        env = {**os.environ, 'PYTHONPATH': str(self.library)}
        first = subprocess.Popen(command, env=env, preexec_fn=drop, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: len(self.emu.records) >= 2, 'CLI did not send records', first)
        finally:
            first.kill()
            first.communicate(timeout=10)
        self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        second = subprocess.Popen(command, env=env, preexec_fn=drop, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: self.checkpoint() == 1, 'CLI restart did not acknowledge', second)
        finally:
            second.kill()
            output = b''.join(second.communicate(timeout=10))
        self.assertNotIn(b'local-secret', output)
        self.assertEqual({r['sequence'] for r in self.emu.records}, {0, 1})
        self.assertTrue(all(r['source_id'] == 'device-fixture' for r in self.emu.records))

    def test_exponential_backoff_caps_without_checkpoint_advancement(self):
        self.append(['denied'])
        self.emu.mode = 'denied'
        exporter = self.open()
        backoffs = []
        def sleep(delay):
            if delay >= 1:
                backoffs.append(delay)
                if len(backoffs) == 5:
                    raise KeyboardInterrupt()
        exporter.sleep = sleep
        with self.assertRaises(KeyboardInterrupt):
            exporter.run()
        self.assertEqual(backoffs, [1, 2, 4, 8, 8])
        self.assertEqual(self.checkpoint(), -1)

    def test_oversized_record_blocks_instead_of_bypassing_budget(self):
        self.append(['x' * 2048])
        self.config['batch_bytes'] = 1024
        with self.assertRaises(ValueError):
            self.open().step()
        self.assertFalse(self.emu.records)
        self.assertEqual(self.checkpoint(), -1)

    def test_expired_credentials_and_malformed_results_preserve_checkpoint(self):
        self.append(['buffered'])
        exporter = self.open()
        for mode in ('expired', 'malformed'):
            self.emu.mode = mode
            with self.assertRaises(Exception):
                exporter.step()
            self.assertEqual(self.checkpoint(), -1)
        self.emu.mode = 'ok'
        self.assertTrue(self.open().step())

    def test_snapshot_disk_failure_prevents_cloud_mutation(self):
        self.append(['durable'])
        exporter = self.open()
        temporary = Path(self.temp.name) / 'checkpoint.tmp'
        temporary.mkdir()
        with self.assertRaises(OSError):
            exporter.step()
        self.assertFalse(self.emu.records)
        self.assertEqual(self.checkpoint(), -1)
        temporary.rmdir()
        self.assertTrue(self.open().step())
        self.assertEqual(self.checkpoint(), 0)

    def test_moto_readback_and_reference_checkpoint_fixture(self):
        import boto3
        from botocore.config import Config
        from moto.server import ThreadedMotoServer
        server = ThreadedMotoServer(ip_address='127.0.0.1', port=0, verbose=False)
        server.start()
        try:
            host, port = server.get_host_and_port()
            endpoint = f'http://{host}:{port}'
            session = boto3.Session(region_name='us-east-1')
            kinesis = session.client('kinesis', endpoint_url=endpoint,
                                     config=Config(proxies={}, retries={'total_max_attempts': 1}))
            kinesis.create_stream(StreamName='target', ShardCount=1)
            shard = kinesis.get_shard_iterator(StreamName='target', ShardId='shardId-000000000000',
                                              ShardIteratorType='TRIM_HORIZON')['ShardIterator']
            cases = json.loads((ROOT / 'interfaces/compat/stream-cases.json').read_bytes())
            fixture = next(case for case in cases if case['id'] == 'export-checkpoint-idempotent')
            self.config['endpoint'] = endpoint
            self.config['batch_messages'] = 1
            exporter = self.open()
            for step in fixture['steps']:
                operation, expect = step['op'], step['expect']
                if operation['op'] == 'append':
                    sequence = self.source.stream_append(self.stream, 'x' * operation['size'])
                    self.assertEqual(sequence, expect['sequence'])
                elif operation['op'] == 'export-ack':
                    if 'error' in expect:
                        with self.assertRaises(IpcError) as raised:
                            self.source.stream_export_ack(self.stream, operation['sequence'])
                        self.assertEqual(str(raised.exception), expect['error'])
                    elif self.checkpoint() < operation['sequence']:
                        self.assertTrue(exporter.step())
                        self.assertEqual(self.checkpoint(), expect['exported'])
                        exporter = self.open()
                    else:
                        result = self.source.stream_export_ack(self.stream, operation['sequence'])
                        self.assertEqual(result['exported'], expect['exported'])
                elif operation['op'] == 'describe':
                    self.assertEqual(self.checkpoint(), expect['exported'])
            received = kinesis.get_records(ShardIterator=shard)['Records']
            self.assertEqual([json.loads(r['Data'])['payload'] for r in received], ['x', 'x'])
            self.assertEqual([json.loads(r['Data'])['sequence'] for r in received], [0, 1])

            # Same durable source, independent second export to S3 and SDK read-back.
            s3 = session.client('s3', endpoint_url=endpoint,
                                config=Config(proxies={}, s3={'addressing_style': 'path'}))
            s3.create_bucket(Bucket='bucket')
            self.append([{'moto': 's3 exact bytes'}, ['array', 42]])
            self.config['state_dir'] = str(Path(self.temp.name) / 's3-state')
            self.config['batch_messages'] = 500
            self.assertTrue(self.open(s3=True).step())
            objects = s3.list_objects_v2(Bucket='bucket', Prefix='exclusive/')['Contents']
            self.assertEqual(len(objects), 1)
            body = s3.get_object(Bucket='bucket', Key=objects[0]['Key'])['Body']
            try:
                records = [json.loads(line) for line in body.read().splitlines()]
            finally:
                body.close()
            self.assertEqual([r['payload'] for r in records], [{'moto': 's3 exact bytes'}, ['array', 42]])
            self.assertEqual([r['sequence'] for r in records], [2, 3])
            self.assertEqual(self.checkpoint(), 3)
        finally:
            server.stop()

    def test_crash_between_all_record_acks_and_delivered_marker(self):
        self.append(['already-cloud-confirmed'])
        exporter = self.open()
        self.assertTrue(exporter.snapshot())
        exporter.kinesis(exporter.state['pending'])
        self.assertEqual(self.checkpoint(), -1)
        self.assertEqual(exporter.state['pending']['done'], [True])
        self.assertFalse(exporter.state['pending']['delivered'])
        self.assertTrue(self.open().step())
        self.assertEqual(len(self.emu.records), 1)
        self.assertEqual(self.checkpoint(), 0)

    def test_environment_cannot_override_explicit_endpoint_policy(self):
        # Construct only: never send a request to a real AWS service.
        self.config.pop('endpoint')
        os.environ['AWS_ENDPOINT_URL'] = 'http://127.0.0.1:9/implicit-global'
        os.environ['AWS_ENDPOINT_URL_KINESIS'] = 'http://127.0.0.1:9/implicit-kinesis'
        os.environ['AWS_ENDPOINT_URL_S3'] = 'http://127.0.0.1:9/implicit-s3'
        for service, endpoint in [('kinesis', 'https://kinesis.us-east-1.amazonaws.com'),
                                  ('s3', 'https://s3.amazonaws.com')]:
            exporter = self.open(s3=service == 's3')
            self.assertEqual(exporter.aws.meta.endpoint_url, endpoint)
            self.assertFalse(self.emu.calls)

    def test_no_default_profile_credentials_and_invalid_limits(self):
        os.environ.pop('AWS_CONTAINER_CREDENTIALS_FULL_URI')
        os.environ.update(AWS_ACCESS_KEY_ID='WRONG', AWS_SECRET_ACCESS_KEY='WRONG')
        with self.assertRaises(ValueError):
            self.open()
        with self.assertRaises(ValueError):
            validate({**self.config, 'service': 's3', 'destination': 'bucket/private/', 'part_bytes': 4194304})
        for change in ({'batch_messages': 501}, {'bytes_per_second': 0}, {'endpoint': 'https://aws.example'},
                       {'batch_bytes': 5 << 21}, {'service': 'sitewise'}):
            with self.assertRaises(ValueError):
                validate({**self.config, **change})


if __name__ == '__main__':
    unittest.main(verbosity=2)
