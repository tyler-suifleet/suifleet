#!/usr/bin/env python3
"""Actual metrics adapter, durable queue and HTTP-observed fixture execution."""
import datetime
import json
import os
import socket
import subprocess
import sys
import time
import struct
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cloudwatch_metrics import Exporter, Transport, body, receive
from aws_credentials import allow_principals

ROOT = Path(__file__).resolve().parents[1]


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = queue.Queue()
        cls.observed = []
        cls.bad_credentials = False
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'Expiration': 123} if cls.bad_credentials else dict(AccessKeyId="FIXTURE", SecretAccessKey="fixture-secret", Token="fixture-token",
                    Expiration="2099-01-01T00:00:00Z")).encode())
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                cls.observed.append((urllib.parse.parse_qs(raw.decode()), dict(self.headers)))
                result = cls.results.get_nowait()
                self.send_response(dict(ok=200, invalid=400, expired=400, throttled=429, **{'server-error':503, 'connection-error':403})[result])
                self.end_headers()
                code = "InvalidParameterValue" if result == "invalid" else "ExpiredToken" if result == "expired" else "Other"
                self.wfile.write(f"<Error><Code>{code}</Code><Message>Throttl-like metric name</Message></Error>".encode() if result != 'ok' else b'<PutMetricDataResponse/>')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def transport(self):
        url = f'http://127.0.0.1:{self.server.server_port}'
        return Transport('us-east-1', dict(AWS_CONTAINER_CREDENTIALS_FULL_URI=url+'/2016-11-01/credentialprovider/',
                          AWS_CONTAINER_AUTHORIZATION_TOKEN='fixture-bootstrap'), url, emulator=True)

    def test_all_pinned_fixtures_against_http_and_sqlite(self):
        cases = json.loads((ROOT / 'interfaces/compat/cloudwatch-metrics-cases.json').read_text())
        count = 0
        for case in cases:
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as directory:
                now = [0]
                path = Path(directory)/'metrics.sqlite'
                adapter = Exporter(path, self.transport(), 'core-fixture', clock=lambda: now[0])
                grants = set()
                for step in case['steps']:
                    op = step['op']
                    now[0] = op.get('now', now[0])
                    kind = op['op']
                    if kind == 'grant':
                        grants.add(op['publisher']); actual = {}
                    elif kind == 'revoke':
                        grants.discard(op['publisher']); actual = {}
                    elif kind == 'configure':
                        interval = op.get('publish_interval', adapter.interval)
                        retain = op.get('max_metrics_to_retain', adapter.retain)
                        try:
                            adapter.configure(interval, retain)
                            actual = dict(status='ok')
                        except ValueError:
                            actual = dict(status='invalid-config')
                    elif kind == 'upstream':
                        for result in op['results']:
                            self.results.put(result)
                        actual = {}
                    elif kind == 'restart':
                        adapter.close()
                        adapter = Exporter(path, self.transport(), 'core-fixture', adapter.interval, adapter.retain, lambda: now[0])
                        actual = adapter.state()
                    elif kind == 'put':
                        for _ in range(op.get('count', 1)):
                            client, server = socket.socketpair()
                            try:
                                client.sendall(json.dumps(op['message']).encode()+b'\n')
                                actual = receive(server, op['publisher'], grants, adapter)
                            finally:
                                client.close()
                                server.close()
                    elif kind == 'tick':
                        start = len(self.observed)
                        actual = adapter.tick()
                        for (fields, headers), call in zip(self.observed[start:], actual['calls']):
                            self.assertEqual(fields['Action'], ['PutMetricData'])
                            self.assertEqual(fields['Namespace'], [call['namespace']])
                            self.assertEqual(fields['MetricData.member.1.MetricName'], [call['first']])
                            self.assertIn('AWS4-HMAC-SHA256', headers['Authorization'])
                            self.assertEqual(headers['x-amz-security-token'], 'fixture-token')
                            names = [v for k,v in fields.items() if k.endswith('.Name')]
                            self.assertIn(['coreName'], names)
                    else:
                        self.fail(kind)
                    self.assertEqual(actual, step['expect'], (case['id'], count, op))
                    count += 1
                adapter.close()
        self.assertEqual(count, 90)
        self.assertTrue(self.results.empty())

    def test_independent_botocore_signature(self):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        with tempfile.TemporaryDirectory() as directory:
            adapter = Exporter(Path(directory)/"queue", self.transport(), "core", interval=0)
            adapter.put(dict(request=dict(namespace="Test", metricData=dict(metricName="x", value=1))))
            entry = json.loads(adapter.db.execute("SELECT data FROM metrics").fetchone()[0])
            self.results.put("ok")
            adapter.tick()
            _, headers = self.observed[-1]
            payload = body("Test", [entry])
            request = AWSRequest(method="POST", url=f"http://127.0.0.1:{self.server.server_port}/", data=payload,
                headers={k:v for k,v in headers.items() if k.lower() in ("content-type", "host", "x-amz-date", "x-amz-security-token")})
            signer = SigV4Auth(Credentials("FIXTURE", "fixture-secret", "fixture-token"), "monitoring", "us-east-1")
            request.context["timestamp"] = headers["x-amz-date"]
            canonical = signer.canonical_request(request)
            signature = signer.signature(signer.string_to_sign(request, canonical), request)
            self.assertEqual(headers["Authorization"].split("Signature=")[1], signature)
            adapter.close()

    @unittest.skipUnless(os.geteuid() == 0, "distinct UIDs require disposable root container")
    def test_kernel_uid_grants_and_frame_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            allow_principals(directory, (61001, 61002), "--x")
            path = directory + "/metrics.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            allow_principals(path, (61001, 61002), "rw-")  # kernel policy still denies 61002
            listener.listen()
            adapter = Exporter(Path(directory)/"queue", self.transport(), "device")
            message = dict(request=dict(namespace="Device", metricData=dict(metricName="cpu", value=1)))
            client = """import socket,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);s.sendall(sys.argv[2].encode()+b'\\n');print(s.recv(16384).decode())
"""
            for uid, payload, expected in [(61001, json.dumps(message), "success"),
                    (61002, json.dumps(message), "Unauthorized"), (61001, "x"*17000, "InvalidInputException"), (61001, "["*2000, "InvalidInputException")]:
                process = subprocess.Popen([sys.executable, "-c", client, path, payload], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, user=uid, group=uid, extra_groups=[])
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(1)
                    _, actual_uid, _ = struct.unpack("iII", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    result = receive(conn, actual_uid, [61001], adapter)
                    conn.sendall(json.dumps(result).encode()+b"\n")
                out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, "client failed")
                self.assertEqual(result.get("error", result["status"]), expected)
            self.assertEqual(adapter.state()["buffered"], 1)
            listener.close()
            adapter.close()

    @unittest.skipUnless(os.geteuid() == 0, "production service runs in disposable root container")
    def test_production_cli_restart_and_private_queue(self):
        with tempfile.TemporaryDirectory(dir="/run", prefix="sf059-") as directory:
            root = Path(directory)
            allow_principals(root, (61001,), "--x")
            state = root/"state"
            state.mkdir(mode=0o700)
            for marker in (root/".owner", state/".owner"):
                marker.write_text("")
                marker.chmod(0o600)
            bootstrap = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bootstrap.bind(str(root/"credentials.sock"))
            bootstrap.listen()
            settings = dict(socket=str(root/"metrics.sock"), queue=str(state/"queue"), core_name="device", region="us-east-1",
                credential_socket=str(root/"credentials.sock"), credential_component="metrics", publishers=[61001], daemon_uid=61003, interval=900)
            config = root/"config.json"
            config.write_text(json.dumps(settings))
            config.chmod(0o600)
            process = subprocess.Popen([sys.executable, str(ROOT/"scripts/cloudwatch_metrics.py"), "--config", str(config)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                connection, _ = bootstrap.accept()
                with connection:
                    request = json.loads(connection.makefile("rb").readline())
                    self.assertEqual(request["component"], "metrics")
                    url = f"http://127.0.0.1:{self.server.server_port}/2016-11-01/credentialprovider/"
                    connection.sendall(json.dumps(dict(ok=True, data=dict(env=dict(AWS_CONTAINER_CREDENTIALS_FULL_URI=url,
                        AWS_CONTAINER_AUTHORIZATION_TOKEN="fixture-bootstrap")))).encode()+b"\n")
                for _ in range(100):
                    if (root/"metrics.sock").exists():
                        break
                    if process.poll() is not None:
                        self.fail("production service startup failed")
                    time.sleep(.05)
                code = """import socket,json,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);s.sendall(json.dumps({"request":{"namespace":"Device","metricData":{"metricName":"cpu","value":1}}}).encode()+b'\\n');print(s.recv(16384).decode())
"""
                output = subprocess.check_output([sys.executable, "-c", code, str(root/"metrics.sock")], user=61001, group=61001, extra_groups=[])
                self.assertEqual(json.loads(output)["status"], "success")
                denied = subprocess.run([sys.executable, "-c", "import sys;open(sys.argv[1]).read()", str(state/"queue")],
                    user=61001, group=61001, extra_groups=[], capture_output=True)
                self.assertNotEqual(denied.returncode, 0)
            finally:
                process.terminate()
                out, err = process.communicate(timeout=10)
                bootstrap.close()
            self.assertEqual(process.returncode, 0)
            self.assertEqual(out+err, b"")
            self.assertFalse((root/"metrics.sock").exists())
            adapter = Exporter(state/"queue", self.transport(), "device", interval=0)
            self.assertEqual(adapter.state()["buffered"], 1)
            self.results.put("ok")
            self.assertEqual(adapter.tick()["buffered"], 0)
            adapter.close()

    def test_payload_limits_singleton_and_unavailable_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"queue"
            adapter = Exporter(path, self.transport(), "device", interval=0)
            with self.assertRaises(BlockingIOError):
                Exporter(path, self.transport(), "device")
            bad = dict(request=dict(namespace="Test", metricData=dict(metricName="x", value=1,
                dimensions=[dict(name="huge", value="x"*40000)])))
            self.assertEqual(adapter.put(bad)["error"], "InvalidInputException")
            normal = dict(request=dict(namespace="Test", metricData=dict(metricName="x", value=1,
                dimensions=[dict(name="large", value="x"*10000)])))
            for _ in range(5):
                adapter.put(normal)
            for _ in range(2):
                self.results.put("ok")
            calls = adapter.tick()["calls"]
            self.assertEqual([c["count"] for c in calls], [3, 2])
            adapter.put(dict(request=dict(namespace="Test", metricData=dict(metricName="x", value=1))))
            type(self).bad_credentials = True
            try:
                self.assertEqual(adapter.tick()["buffered"], 1)
            finally:
                type(self).bad_credentials = False
            adapter.transport.provider = urllib.parse.urlsplit("http://127.0.0.1:1/2016-11-01/credentialprovider/")
            self.assertEqual(adapter.tick()["buffered"], 1)
            adapter.close()

    def test_sf019_native_snapshot_and_tls_redaction(self):
        from cloudwatch_telemetry import messages
        source = json.loads((ROOT/"interfaces/telemetry-cases.json").read_text())["signed"]["snapshot"]
        with tempfile.TemporaryDirectory() as directory:
            keylog = Path(directory)/"tls-keys"
            previous = os.environ.get("SSLKEYLOGFILE")
            os.environ["SSLKEYLOGFILE"] = str(keylog)
            try:
                transport = self.transport()
            finally:
                if previous is None:
                    del os.environ["SSLKEYLOGFILE"]
                else:
                    os.environ["SSLKEYLOGFILE"] = previous
            self.assertIsNone(transport.context.keylog_filename)
            self.assertFalse(keylog.exists())
            adapter = Exporter(Path(directory)/"queue", transport, "device", interval=0)
            for message in messages(source, "Native"):
                self.assertEqual(adapter.put(message)["status"], "success")
            self.results.put("ok")
            self.assertEqual(adapter.tick()["buffered"], 0)
            fields, _ = self.observed[-1]
            self.assertEqual(fields["MetricData.member.3.MetricName"], ["TelemetryDropped"])
            self.assertEqual(fields["MetricData.member.3.Value"], ["2"])
            self.assertEqual(fields["MetricData.member.8.MetricName"], ["Restarts"])
            self.assertEqual(fields["MetricData.member.8.Value"], ["2"])
            wire = json.dumps(fields)
            for sensitive in ("last_error", "last_exit", "signer", "registry", "exit status"):
                self.assertNotIn(sensitive, wire)
            with self.assertRaises(ValueError):
                messages(dict(source, schema=2), "Native")
            adapter.close()

    def test_full_batch_outage_retry_pacing(self):
        for interval in (0, 10, 900):
            for failure in ('connection-error', 'throttled', 'server-error'):
                with self.subTest(interval=interval, failure=failure), tempfile.TemporaryDirectory() as directory:
                    now = [0]
                    path = Path(directory)/'queue'
                    adapter = Exporter(path, self.transport(), 'device', interval=interval, clock=lambda: now[0])
                    def put(namespace):
                        return adapter.put(dict(request=dict(namespace=namespace, metricData=dict(metricName='x', value=1))))
                    for _ in range(60):
                        put('Outage')
                    put('Healthy')
                    self.results.put(failure)
                    self.results.put('ok')
                    first = adapter.tick()
                    self.assertEqual([(c['namespace'], c['count'], c['result']) for c in first['calls']],
                                     [('Outage', 20, failure), ('Healthy', 1, 'ok')])
                    self.assertEqual(first['buffered'], 60)
                    deadline = max(1, interval)
                    for instant in (0, deadline/2, deadline-.001):
                        now[0] = instant
                        self.assertEqual(put('Outage')['status'], 'success')
                        self.assertEqual(adapter.tick()['calls'], [])
                    # A newly full healthy namespace proceeds during the outage cooldown.
                    for _ in range(20):
                        put('Healthy')
                    self.results.put('ok')
                    self.assertEqual([c['namespace'] for c in adapter.tick()['calls']], ['Healthy'])
                    now[0] = deadline
                    self.results.put(failure)
                    self.assertEqual(len(adapter.tick()['calls']), 1)
                    self.assertEqual(adapter.tick()['calls'], [])
                    now[0] += deadline
                    for _ in range(4):
                        self.results.put('ok')
                    recovered = adapter.tick()
                    self.assertEqual([c['count'] for c in recovered['calls']], [20, 20, 20, 3])
                    self.assertEqual(recovered['buffered'], 0)
                    adapter.close()
        self.assertTrue(self.results.empty())

    def test_wire_values_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'queue'
            now = [0]
            adapter = Exporter(path, self.transport(), 'device', interval=0, clock=lambda: now[0])
            message = dict(request=dict(namespace='Device', metricData=dict(metricName='cpu', value=12.5, unit='Percent',
                           timestamp=1539027324, dimensions=[dict(name='component', value='sensor')])))
            self.assertEqual(adapter.put(message)['status'], 'success')
            self.results.put('server-error')
            self.assertEqual(adapter.tick()['buffered'], 1)
            adapter.close()
            adapter = Exporter(path, self.transport(), 'device', interval=0, clock=lambda: now[0])
            self.results.put('expired')
            self.assertEqual(adapter.tick()['buffered'], 1)
            now[0] += 1
            self.results.put('ok')
            self.assertEqual(adapter.tick()['buffered'], 0)
            fields, _ = self.observed[-1]
            self.assertEqual(fields['MetricData.member.1.Value'], ['12.5'])
            self.assertEqual(fields['MetricData.member.1.Unit'], ['Percent'])
            self.assertEqual(fields['MetricData.member.1.Timestamp'], ['2018-10-08T19:35:24Z'])
            adapter.close()


if __name__ == '__main__':
    unittest.main()
