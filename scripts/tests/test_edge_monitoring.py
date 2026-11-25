"""Offline tests for scripts/edge_monitoring.py (SF-303).

A stand-in local console and OTLP collector run on loopback HTTP; the real
adapter code (configuration, console client, Prometheus endpoint, OTLP
transport, backoff and redaction) runs against them. The integration harness
scripts/test_edge_monitoring.py exercises the real daemon, real Prometheus and
a real OpenTelemetry Collector.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edge_monitoring as em  # noqa: E402

CONSOLE_TOKEN = "sfc_" + "A" * 43


def send_truncated(handler, truncation):
    """Start a reply, then close mid-body (a transient transport failure).

    truncation is an HTTP status (chunked framing: urllib raises IncompleteRead)
    or (status, "length"): a Content-Length body cut short by EOF, which
    http.client returns short without raising.
    """
    status, framing = truncation if isinstance(truncation, tuple) else (truncation, "chunked")
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    if framing == "chunked":
        handler.send_header("Transfer-Encoding", "chunked")
        prefix = b"100\r\n"
    else:
        handler.send_header("Content-Length", "100")
        prefix = b""
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(prefix + b"{\"version\":1,\"partialSuccess\":")
    handler.wfile.flush()
    handler.close_connection = True


# Every truncation shape: chunked and length-delimited, success and error status.
TRUNCATIONS = (200, 503, (200, "length"), (503, "length"))


class FakeConsole:
    def __init__(self):
        self.health = dict(health="degraded", reasons=["svc exited"], daemon_version="0.2.9", started_ms=1000,
                           uptime_ms=5500, pending_receipts=1, components=2,
                           sync=dict(last_success_ms=2000, consecutive_failures=0))
        self.components = [dict(component="svc", version="1.0", health="running", deployment="0x1", installed=True,
                                supervised=True, restarts=2, since_ms=1, last_exit=None),
                           dict(component='odd"name', version="1.0", health="exited", deployment="0x2",
                                installed=True, supervised=True, restarts=7, since_ms=1, last_exit="exit 1"),
                           dict(component="tool", version="1.0", health="installed", deployment="0x3",
                                installed=True, supervised=False, restarts=None, since_ms=None, last_exit=None)]
        self.deployments = [dict(deployment=f"0x{i:x}", status=[1, 2, None][i % 3], reported=i % 2 == 0,
                                 indeterminate=i == 4, error="password=hunter2" if i % 3 == 1 else None)
                            for i in range(250)]
        self.logs = {"svc": ["one", "two"]}
        self.requests = []
        self.status = None
        self.truncate = []          # queued HTTP statuses answered with a truncated body
        self.raw = None             # verbatim 200 response body, when set
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((dict(self.headers), body))
                if fake.truncate:
                    return send_truncated(self, fake.truncate.pop(0))
                if fake.raw is not None:
                    return self.send_raw(fake.raw)
                if self.headers.get("Authorization") != "Bearer " + CONSOLE_TOKEN:
                    return self.reply(401, dict(version=1, status="unauthenticated"))
                if fake.status:
                    return self.reply(503, dict(version=1, status=fake.status))
                op = body["operation"]
                if op == "health":
                    data = fake.health
                elif op in ("inventory", "deployments"):
                    items = fake.components if op == "inventory" else fake.deployments
                    key = "component" if op == "inventory" else "deployment"
                    rest = [x for x in items if body.get("after") is None or x[key] > body["after"]]
                    rest.sort(key=lambda x: x[key])
                    page = rest[:body["limit"]]
                    data = {("components" if op == "inventory" else "deployments"): page,
                            "next": page[-1][key] if len(rest) > len(page) else None}
                elif op == "logs":
                    if body["component"] not in fake.logs:
                        return self.reply(404, dict(version=1, status="not-found"))
                    data = dict(component=body["component"], lines=fake.logs[body["component"]][-body["limit"]:])
                else:
                    return self.reply(403, dict(version=1, status="denied"))
                self.reply(200, dict(version=1, status="ok", data=data))

            def send_raw(self, data):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def reply(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        return Handler


class FakeCollector:
    def __init__(self, require=None):
        self.require = require
        self.received = []
        self.responses = []         # queued (status, headers, body); default 200
        self.truncate = []          # queued HTTP statuses answered with a truncated body
        self.arrived = []           # request paths, recorded before any reply delay
        self.gate = None            # threading.Event that holds every reply until set
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{server.server_port}"

    def close(self):
        if self.gate:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()

    def handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.arrived.append(self.path)
                if fake.gate:
                    fake.gate.wait(30)
                if fake.truncate:
                    status = fake.truncate.pop(0)
                    fake.received.append((self.path, dict(self.headers), json.loads(body), status))
                    return send_truncated(self, status)
                if fake.require and self.headers.get("Authorization") != fake.require:
                    status, headers, reply = 401, {}, b""
                elif fake.responses:
                    status, headers, reply = fake.responses.pop(0)
                else:
                    status, headers, reply = 200, {}, b"{}"
                fake.received.append((self.path, dict(self.headers), json.loads(body), status))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
        return Handler


def names_of(otlp_metrics):
    return {m["name"]: m for m in otlp_metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sf303-unit-")
        self.dir = Path(self.tmp.name)
        os.chmod(self.dir, 0o700)
        self.token_file = self.write("console.token", CONSOLE_TOKEN + "\n", 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, mode=0o600):
        path = self.dir / name
        path.write_text(text)
        os.chmod(path, mode)
        return str(path)

    def with_mode(self, path, mode):
        """Report `path` to the adapter as a regular file with permission bits
        `mode`, so group/other-access checks run against the real file's
        metadata without creating a group- or world-accessible file."""
        real_lstat = os.lstat
        target = os.path.abspath(path)

        def fake(name, *args, **kwargs):
            info = real_lstat(name, *args, **kwargs)
            if os.path.abspath(name) != target:
                return info
            fields = list(info)
            fields[stat.ST_MODE] = stat.S_IFREG | mode
            return os.stat_result(fields)
        return unittest.mock.patch.object(em.os, "lstat", side_effect=fake)

    def config(self, **overrides):
        raw = dict(enabled=True, evidence="simulated", device="sim-1",
                   console=dict(url="http://127.0.0.1:1441", token_file=self.token_file),
                   prometheus=dict(listen="127.0.0.1:0"))
        raw.update(overrides)
        return raw


class ConfigTests(Base):
    def test_disabled_by_default(self):
        self.assertFalse(em.Config({}).enabled)
        self.assertFalse(em.Config(dict(enabled=False, otlp=dict(endpoint="ftp://x"))).enabled)
        path = self.write("config.json", "{}")
        self.assertEqual(em.main(["--config", path]), 0)

    def test_disabled_main_opens_no_socket(self):
        path = self.write("config.json", json.dumps(dict(enabled=False)))
        with unittest.mock.patch.object(em, "make_server", side_effect=AssertionError("listened")), \
                unittest.mock.patch.object(em, "Console", side_effect=AssertionError("polled")):
            self.assertEqual(em.main(["--config", path]), 0)

    def test_valid_configuration(self):
        digest = hashlib.sha256(b"x").hexdigest()
        headers = self.write("otlp.headers", "Authorization: Bearer abc\n# comment\nX-Scope-OrgID: t1\n")
        cfg = em.Config(self.config(prometheus=dict(listen="0.0.0.0:9469", allow_remote=True, token_sha256=digest),
                                    otlp=dict(endpoint="https://collector.example:4318/", headers_file=headers),
                                    logs=dict(components=["svc"], max_lines=20)))
        self.assertEqual(cfg.console_token, CONSOLE_TOKEN)
        self.assertEqual(cfg.otlp["metrics_url"], "https://collector.example:4318/v1/metrics")
        self.assertEqual(cfg.otlp["headers"], {"Authorization": "Bearer abc", "X-Scope-OrgID": "t1"})
        self.assertIsNone(cfg.otlp["context"].keylog_filename)

    def test_rejections(self):
        digest = hashlib.sha256(b"x").hexdigest()
        bad = [
            dict(evidence=None), dict(evidence="production"), dict(device="bad device"),
            dict(interval_seconds=5), dict(interval_seconds=True), dict(unknown=1),
            dict(console=dict(url="http://10.0.0.1:1441", token_file=self.token_file)),
            dict(console=dict(url="https://127.0.0.1:1441", token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1", token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1:1441")),
            dict(prometheus=dict(listen="0.0.0.0:9469")),
            dict(prometheus=dict(listen="0.0.0.0:9469", allow_remote=True)),
            dict(prometheus=dict(listen="example.com:9469", allow_remote=True, token_sha256=digest)),
            dict(prometheus=dict(listen="127.0.0.1:9469", token_sha256="ABC")),
            dict(prometheus=None),
            dict(otlp=dict(endpoint="http://collector.example:4318")),
            dict(otlp=dict(endpoint="https://user:pw@collector.example")),
            dict(device=None, otlp=dict(endpoint="http://127.0.0.1:4318")),   # OTLP needs a device id
            dict(otlp=dict(endpoint="https://collector.example?x=1")),
            dict(otlp=dict(endpoint="https://c.example", initial_backoff_seconds=10, max_backoff_seconds=5)),
            dict(logs=dict(components=["svc"])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318", logs=False), logs=dict(components=["svc"])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components=["../x"])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components=["a", "a"])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components=["a"], max_lines=201)),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(em.ConfigError):
                em.Config(self.config(**overrides))

    def test_malformed_types_and_ports_are_config_errors(self):
        # Every malformed value takes the sanitized ConfigError path, never an
        # AttributeError/TypeError/ValueError traceback, and an unreachable
        # destination is never reported as a valid configuration.
        bad = [
            dict(console=dict(url=123, token_file=self.token_file)),
            dict(console=dict(url=["http://127.0.0.1:1441"], token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1:abc", token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1:99999", token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1:0", token_file=self.token_file)),
            dict(console=dict(url="http://[::1:1441", token_file=self.token_file)),
            dict(otlp=dict(endpoint="http://127.0.0.1:abc")),
            dict(otlp=dict(endpoint="https://collector.example:70000")),
            dict(otlp=dict(endpoint="https://collector.example:0")),
            dict(otlp=dict(endpoint="https://[collector")),
            dict(otlp=dict(endpoint=4318)),
            dict(otlp=dict(endpoint="https://collector.example", headers_file=7)),
            dict(otlp=dict(endpoint="https://collector.example", ca_file=["x"])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components=[["svc"]])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components=[{"a": 1}])),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318"), logs=dict(components="svc")),
            dict(prometheus=dict(listen=9469)),
            dict(prometheus=dict(listen="127.0.0.1:\u00b2")),
            dict(prometheus=dict(listen="127.0.0.1:\u0669\u0664\u0666\u0669")),
            dict(prometheus=dict(listen="127.0.0.1:" + "9" * 5000)),
            dict(prometheus=dict(listen="127.0.0.1:094690")),
            dict(prometheus=dict(listen="127.0.0.1:65536")),
            dict(prometheus=dict(listen="127.0.0.1:")),
            dict(console=dict(url="http://127.0.0.1:\u00b2", token_file=self.token_file)),
            dict(console=dict(url="http://127.0.0.1:" + "9" * 5000, token_file=self.token_file)),
            dict(otlp=dict(endpoint="https://collector.example:" + "9" * 5000)),
            dict(console="http://127.0.0.1:1441"), dict(logs=["svc"]), dict(otlp="http://127.0.0.1:4318"),
            # Embedded NUL or lone-surrogate paths make os.lstat raise ValueError.
            dict(console=dict(url="http://127.0.0.1:1441", token_file=self.token_file + "\0x")),
            dict(console=dict(url="http://127.0.0.1:1441", token_file="/tmp/\ud800.token")),
            dict(otlp=dict(endpoint="http://127.0.0.1:4318", headers_file="/tmp/h\0x")),
            dict(otlp=dict(endpoint="https://collector.example", ca_file="/tmp/ca\0x")),
            dict(otlp=dict(endpoint="https://collector.example", ca_file="/tmp/\udcff.pem")),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(em.ConfigError):
                em.Config(self.config(**overrides))
            with self.subTest(check_config=overrides):
                path = self.write("config.json", json.dumps(self.config(**overrides)))
                with unittest.mock.patch("sys.stderr") as stderr:
                    self.assertEqual(em.main(["--config", path, "--check-config"]), 2)
                written = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn("configuration rejected", written)
                self.assertNotIn("Traceback", written)
        # An explicit valid port and the scheme default are still accepted.
        self.assertEqual(em.Config(self.config(otlp=dict(endpoint="https://collector.example"))).otlp["metrics_url"],
                         "https://collector.example/v1/metrics")
        em.Config(self.config(otlp=dict(endpoint="http://[::1]:4318")))
        # Supported listen settings are preserved.
        for listen, expected in (("127.0.0.1:9469", ("127.0.0.1", 9469)), ("127.0.0.1:0", ("127.0.0.1", 0)),
                                 ("localhost:9469", ("localhost", 9469)), ("[::1]:9469", ("[::1]", 9469))):
            with self.subTest(listen=listen):
                self.assertEqual(em._split_listen(listen), expected)
                em.Config(self.config(prometheus=dict(listen=listen)))

    def test_deeply_nested_configuration_is_rejected(self):
        # 1500 nested arrays fit within the 64 KiB limit but exceed the JSON
        # decoder's recursion limit; that is malformed input, not a crash.
        for text in ("[" * 1500 + "]" * 1500, '{"a":' * 1500 + "1" + "}" * 1500):
            with self.subTest(size=len(text)):
                path = self.write("config.json", text)
                with self.assertRaises(em.ConfigError):
                    em.load_config(path)
                with unittest.mock.patch("sys.stderr") as stderr:
                    self.assertEqual(em.main(["--config", path, "--check-config"]), 2)
                written = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn("configuration rejected", written)
                self.assertNotIn("Traceback", written)

    def test_secret_files_are_protected(self):
        link = self.dir / "link.token"
        link.symlink_to(self.token_file)
        wrong = self.write("wrong.token", "hunter2", 0o600)
        for path in (str(link), wrong, "relative.token"):
            with self.subTest(path=path), self.assertRaises(em.ConfigError):
                em.Config(self.config(console=dict(url="http://127.0.0.1:1441", token_file=path)))
        # Any group/other permission bit on a secret is rejected.
        for mode in (0o640, 0o620, 0o610, 0o604, 0o602, 0o601):
            with self.subTest(mode=oct(mode)), self.with_mode(self.token_file, mode), \
                    self.assertRaises(em.ConfigError):
                em.Config(self.config())
        em.Config(self.config())  # owner-only secret is still accepted

    def test_config_file_must_not_be_writable_by_others(self):
        path = self.write("config.json", json.dumps(dict(enabled=False)))
        with self.with_mode(path, 0o640):  # group-readable configuration is allowed
            self.assertEqual(em.main(["--config", path]), 0)
        for mode in (0o620, 0o602):
            with self.subTest(mode=oct(mode)), self.with_mode(path, mode), \
                    unittest.mock.patch("sys.stderr"):
                self.assertEqual(em.main(["--config", path]), 2)
        for text in ("Host: evil", "NoColon", "Authorization: a\nauthorization: b", "X: a\x01"):
            headers = self.write("h", text)
            with self.subTest(text=text), self.assertRaises(em.ConfigError):
                em.Config(self.config(otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers)))

    def test_non_ascii_header_value_is_rejected_by_check_config(self):
        # urllib cannot send a header value outside Latin-1; accepting it would
        # turn a permanent configuration error into endless "unreachable" retries.
        for value in ("t\u2603", "caf\u00e9"):
            headers = self.write("h", f"Authorization: Bearer ok\nX-Project: {value}\n")
            path = self.write("config.json", json.dumps(self.config(
                otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers))))
            with self.subTest(value=value):
                with self.assertRaisesRegex(em.ConfigError, "line 2 value must be printable ASCII"):
                    em.Config(self.config(otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers)))
                with unittest.mock.patch("sys.stderr") as stderr:
                    self.assertEqual(em.main(["--config", path, "--check-config"]), 2)
                written = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn("configuration rejected", written)
                self.assertIn("line 2", written)
                self.assertNotIn(value, written)
                self.assertNotIn("Bearer ok", written)

    def test_rejected_configuration_message_has_no_secret(self):
        headers = self.write("h", "Authorization: Bearer topsecretvalue\nHost: x\n")
        path = self.write("config.json", json.dumps(self.config(
            otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers))))
        with unittest.mock.patch("sys.stderr") as stderr:
            self.assertEqual(em.main(["--config", path]), 2)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("rejected", written)
        self.assertNotIn("topsecretvalue", written)

    def test_non_ascii_otlp_endpoint_is_rejected_by_check_config(self):
        # http.client cannot put non-ASCII on the request line; accepting it would
        # turn a permanent configuration error into endless "unreachable" retries.
        for endpoint in ("http://127.0.0.1:4318/caf\u00e9", "http://127.0.0.1:4318/a b",
                         "http://127.0.0.1:4318/x\ty", "https://coll\u00e9ctor.example"):
            path = self.write("config.json", json.dumps(self.config(otlp=dict(endpoint=endpoint))))
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(em.ConfigError, "otlp.endpoint must be printable ASCII"):
                    em.Config(self.config(otlp=dict(endpoint=endpoint)))
                with unittest.mock.patch("sys.stderr") as stderr:
                    self.assertEqual(em.main(["--config", path, "--check-config"]), 2)
                self.assertIn("configuration rejected",
                              "".join(call.args[0] for call in stderr.write.call_args_list))
        # The explicitly percent-encoded equivalent is accepted and kept verbatim.
        cfg = em.Config(self.config(otlp=dict(endpoint="http://127.0.0.1:4318/caf%C3%A9")))
        self.assertEqual(cfg.otlp["metrics_url"], "http://127.0.0.1:4318/caf%C3%A9/v1/metrics")
        self.assertEqual(cfg.otlp["logs_url"], "http://127.0.0.1:4318/caf%C3%A9/v1/logs")

    def test_empty_query_or_fragment_delimiter_is_rejected_by_check_config(self):
        # The route is appended to the raw endpoint, so a bare "?" or "#" would
        # turn "/v1/metrics" into a query or fragment and every export would 404.
        for endpoint in ("http://127.0.0.1:4318/collector?", "http://127.0.0.1:4318/collector#",
                         "http://127.0.0.1:4318?", "http://127.0.0.1:4318#", "https://collector.example/?#"):
            path = self.write("config.json", json.dumps(self.config(otlp=dict(endpoint=endpoint))))
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(em.ConfigError, "without credentials, query or fragment"):
                    em.Config(self.config(otlp=dict(endpoint=endpoint)))
                with unittest.mock.patch("sys.stderr") as stderr:
                    self.assertEqual(em.main(["--config", path, "--check-config"]), 2)
                self.assertIn("configuration rejected",
                              "".join(call.args[0] for call in stderr.write.call_args_list))
        # Percent-encoded delimiters are ordinary path characters and stay verbatim.
        cfg = em.Config(self.config(otlp=dict(endpoint="http://127.0.0.1:4318/a%3Fb%23c")))
        self.assertEqual(cfg.otlp["metrics_url"], "http://127.0.0.1:4318/a%3Fb%23c/v1/metrics")

    def test_collector_path_receives_the_otlp_route(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg = em.Config(self.config(prometheus=None, otlp=dict(endpoint=collector.url + "/collector/")))
        transport = em.Transport(cfg, em.Exporter(cfg), clock=lambda: 0.0, rng=lambda: 0.5)
        self.assertEqual(transport.post("metrics", {"resourceMetrics": []}), ("ok", None))
        self.assertEqual(collector.arrived[-1], "/collector/v1/metrics")

    def test_percent_encoded_otlp_endpoint_reaches_the_collector(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg = em.Config(self.config(prometheus=None, otlp=dict(endpoint=collector.url + "/caf%C3%A9")))
        transport = em.Transport(cfg, em.Exporter(cfg), clock=lambda: 0.0, rng=lambda: 0.5)
        self.assertEqual(transport.post("metrics", {"resourceMetrics": []}), ("ok", None))

    def test_token_generation(self):
        token = em.generate_token()
        self.assertRegex(token["token"], em.MONITOR_TOKEN)
        self.assertEqual(hashlib.sha256(token["token"].encode()).hexdigest(), token["token_sha256"])


class RedactionTests(unittest.TestCase):
    def test_redacts_credentials(self):
        cases = {
            "late password=hunter2 ok": "late password=[redacted] ok",
            'api_key: "a b c" next': 'api_key: "[redacted]" next',
            "Authorization: Bearer abc.def": "Authorization: Bearer [redacted]",
            "curl -H 'x' Bearer sometoken": "curl -H 'x' Bearer [redacted]",
            "key suiprivkey1qqabc end": "key [redacted] end",
            "console sfc_" + "A" * 43: "console [redacted]",
            "id AKIAABCDEFGHIJKLMNOP": "id [redacted]",
            "jwt eyJhbGciOi.eyJzdWIiOi.sig-nat": "jwt [redacted]",
            "raw " + "07" * 32: "raw [redacted]",
            "object 0x" + "07" * 32: "object 0x" + "07" * 32,
            "-----BEGIN PRIVATE KEY-----": "[redacted]",
            "QUJD" * 12: "[redacted]",
            "already password=[redacted] fine": "already password=[redacted] fine",
            "plain line 7": "plain line 7",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(em.redact(line), expected)

    def test_new_lines_overlap(self):
        self.assertEqual(em.new_lines(None, ["a", "b"]), (["a", "b"], False))
        self.assertEqual(em.new_lines(["a", "b"], ["a", "b"]), ([], False))
        self.assertEqual(em.new_lines(["a", "b", "c"], ["b", "c", "d", "e"]), (["d", "e"], False))
        self.assertEqual(em.new_lines(["a", "b"], ["x", "y"]), (["x", "y"], True))
        self.assertEqual(em.new_lines([], ["x"]), (["x"], False))
        self.assertEqual(em.new_lines(["x"], []), ([], False))


class AdapterTests(Base):
    def setUp(self):
        super().setUp()
        self.console = FakeConsole()
        self.addCleanup(self.console.close)

    def make(self, **overrides):
        raw = self.config(**overrides)
        raw["console"]["url"] = self.console.url
        cfg = em.Config(raw)
        return cfg, em.Exporter(cfg), em.Console(cfg.console_url, cfg.console_token)

    def test_metrics_distinguish_current_historical_and_evidence(self):
        cfg, exporter, console = self.make()
        self.assertTrue(exporter.poll(console))
        text = exporter.render_prometheus().decode()
        for line in ('suifleet_up{evidence="simulated",device="sim-1"} 1',
                     'suifleet_device_healthy{evidence="simulated",device="sim-1"} 0',
                     'suifleet_device_health_reasons{evidence="simulated",device="sim-1"} 1',
                     'suifleet_device_pending_receipts{evidence="simulated",device="sim-1"} 1',
                     'suifleet_component_running{evidence="simulated",device="sim-1",component="svc"} 1',
                     'suifleet_component_running{evidence="simulated",device="sim-1",component="odd\\"name"} 0',
                     'suifleet_component_restarts{evidence="simulated",device="sim-1",component="odd\\"name"} 7',
                     'suifleet_component_phase{evidence="simulated",device="sim-1",component="tool",phase="installed"} 1',
                     # 250 journal entries over three pages; i=4 is indeterminate.
                     'suifleet_deployment_outcomes{evidence="simulated",device="sim-1",outcome="applied"} 84',
                     'suifleet_deployment_outcomes{evidence="simulated",device="sim-1",outcome="failed"} 82',
                     'suifleet_deployment_outcomes{evidence="simulated",device="sim-1",outcome="indeterminate"} 1',
                     'suifleet_deployment_outcomes{evidence="simulated",device="sim-1",outcome="pending"} 83',
                     'suifleet_deployment_outcomes_truncated{evidence="simulated",device="sim-1"} 0',
                     'suifleet_exporter_polls_total{evidence="simulated",device="sim-1",result="ok"} 1'):
            self.assertIn(line, text)
        self.assertIn("# HELP suifleet_deployment_outcomes Historical:", text)
        self.assertIn("# HELP suifleet_device_healthy Current:", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("svc exited", text)
        self.assertNotIn("component=\"tool\"", text.split("suifleet_component_running")[1].split("# HELP")[0])
        ops = [body["operation"] for _, body in self.console.requests]
        self.assertEqual(ops.count("deployments"), 3)
        self.assertTrue(all(h.get("Authorization") == "Bearer " + CONSOLE_TOKEN for h, _ in self.console.requests))

    def test_redacted_or_repeated_component_names_are_omitted_and_counted(self):
        # The console redacts secret-looking inventory names, so distinct raw
        # names (here two 64-hex names) arrive as one display name. Neither pull
        # nor push may emit contradictory points under identical labels.
        entry = lambda name, installed, phase: dict(
            component=name, version="1.0", health=phase, deployment="0x9", installed=installed,
            supervised=True, restarts=1, since_ms=1, last_exit=None)
        self.console.components += [entry("[redacted]", True, "running"), entry("[redacted]", False, "exited"),
                                    entry("app-[redacted]", True, "running"),
                                    entry("dup", True, "running"), entry("dup", False, "broken")]
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(otlp=dict(endpoint=collector.url))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        self.assertTrue(exporter.poll(console))
        text = exporter.render_prometheus().decode()
        self.assertIn('suifleet_components_unidentified{evidence="simulated",device="sim-1"} 5', text)
        self.assertNotIn("[redacted]", text)
        self.assertNotIn('component="dup"', text)
        samples = [line.rsplit(" ", 1)[0] for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(samples), len(set(samples)), "duplicate Prometheus series")
        self.assertIn('suifleet_component_installed{evidence="simulated",device="sim-1",component="svc"} 1', text)
        em.export_once(exporter, transport)
        body = [b for path, _, b, _ in collector.received if path == "/v1/metrics"][-1]
        metrics = names_of(body)
        self.assertEqual(metrics["suifleet_components_unidentified"]["gauge"]["dataPoints"][0]["asInt"], "5")
        for metric in metrics.values():
            points = (metric.get("gauge") or metric.get("sum"))["dataPoints"]
            identities = [json.dumps(sorted((a["key"], a["value"]["stringValue"]) for a in p["attributes"]))
                          for p in points]
            self.assertEqual(len(identities), len(set(identities)), metric["name"])
        self.assertNotIn("[redacted]", json.dumps(body))

    def test_unavailable_console_drops_stale_device_metrics(self):
        cfg, exporter, console = self.make()
        exporter.poll(console)
        self.console.status = "unavailable"
        self.assertFalse(exporter.poll(console))
        text = exporter.render_prometheus().decode()
        self.assertIn('suifleet_up{evidence="simulated",device="sim-1"} 0', text)
        self.assertIn('result="unavailable"} 1', text)
        self.assertNotIn("suifleet_device_healthy", text)
        self.assertIn("suifleet_exporter_last_success_timestamp_seconds", text)

    def test_truncated_console_response_withdraws_health_and_recovers(self):
        cfg, exporter, console = self.make()
        self.assertTrue(exporter.poll(console))
        for truncation in TRUNCATIONS:                  # success body and HTTPError body paths
            self.console.truncate = [truncation]
            self.assertFalse(exporter.poll(console))
            text = exporter.render_prometheus().decode()
            self.assertIn('suifleet_up{evidence="simulated",device="sim-1"} 0', text)
            self.assertNotIn("suifleet_device_healthy", text)
        self.assertEqual(exporter.polls["unreachable"], len(TRUNCATIONS))
        self.assertTrue(exporter.poll(console))         # console recovers on the next poll
        self.assertIn('suifleet_device_healthy{evidence="simulated",device="sim-1"} 0',
                      exporter.render_prometheus().decode())

    def test_truncated_log_response_is_counted(self):
        for truncation in (200, (200, "length")):
            with self.subTest(truncation=truncation):
                cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9"),
                                                   logs=dict(components=["svc"], max_lines=10))
                self.console.logs["svc"] = ["one", "two"]
                self.assertTrue(exporter.poll(console))
                self.console.logs["svc"] = ["one", "two", "three"]
                real_call = console.call
                calls = []

                def call(operation, **fields):
                    if operation == "logs" and not calls:
                        calls.append(operation)
                        self.console.truncate = [truncation]
                    return real_call(operation, **fields)
                console.call = call
                self.assertTrue(exporter.poll(console))
                self.assertEqual(exporter.polls["logs-unreachable"], 1)
                self.assertEqual(len(exporter.log_buffer), 2)   # nothing new buffered from the truncated read
                self.assertTrue(exporter.poll(console))
                self.assertEqual([line for _, _, line in exporter.log_buffer], ["one", "two", "three"])

    def test_otlp_resources_of_devices_sharing_a_collector_are_distinct(self):
        # Prometheus-only use may omit the device label.
        self.assertIsNone(em.Config(self.config(device=None)).device)
        collector = FakeCollector()
        self.addCleanup(collector.close)
        resources = []
        for device in ("line-3-gateway", "line-4-gateway"):
            cfg, exporter, console = self.make(device=device, prometheus=None, otlp=dict(endpoint=collector.url))
            transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
            exporter.poll(console)
            em.export_once(exporter, transport)
            body = [b for path, _, b, _ in collector.received if path == "/v1/metrics"][-1]
            resource = {a["key"]: a["value"]["stringValue"] for a in body["resourceMetrics"][0]["resource"]["attributes"]}
            self.assertEqual(resource["suifleet.device"], device)
            resources.append(body["resourceMetrics"][0]["resource"])
        self.assertNotEqual(resources[0], resources[1])

    def test_otlp_resource_identity_is_stable_across_console_outage(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint=collector.url))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        exporter.poll(console)
        em.export_once(exporter, transport)
        self.console.status = "unavailable"
        exporter.poll(console)
        em.export_once(exporter, transport)
        self.console.status = None
        self.console.health = dict(self.console.health, daemon_version="0.3.0")   # daemon upgrade
        exporter.poll(console)
        em.export_once(exporter, transport)
        sent = [body for path, _, body, _ in collector.received if path == "/v1/metrics"]
        self.assertEqual(len(sent), 3)
        resources = [body["resourceMetrics"][0]["resource"] for body in sent]
        self.assertEqual(resources[0], resources[1])
        self.assertEqual(resources[0], resources[2])
        up = [names_of(body)["suifleet_up"]["gauge"]["dataPoints"][0]["asInt"] for body in sent]
        self.assertEqual(up, ["1", "0", "1"])
        self.assertNotIn("suifleet_daemon_info", names_of(sent[1]))
        info = names_of(sent[2])["suifleet_daemon_info"]["gauge"]["dataPoints"][0]["attributes"]
        self.assertIn({"key": "version", "value": {"stringValue": "0.3.0"}}, info)

    def test_deeply_nested_console_reply_is_malformed_and_recovers(self):
        cfg, exporter, console = self.make()
        self.assertTrue(exporter.poll(console))
        depth = 20000                           # about 40 KiB, inside the console size limit
        self.console.raw = (b'{"version":1,"status":"ok","data":{"health":' + b"[" * depth + b"]" * depth + b"}}")
        self.assertFalse(exporter.poll(console))
        self.assertEqual(exporter.polls["malformed"], 1)
        text = exporter.render_prometheus().decode()
        self.assertIn('suifleet_up{evidence="simulated",device="sim-1"} 0', text)
        self.assertNotIn("suifleet_device_healthy", text)
        self.console.raw = None
        self.assertTrue(exporter.poll(console))
        self.assertIn('suifleet_device_healthy{evidence="simulated",device="sim-1"} 0',
                      exporter.render_prometheus().decode())

    def test_wrong_console_token_counts_unauthenticated(self):
        cfg, exporter, _ = self.make()
        self.assertFalse(exporter.poll(em.Console(cfg.console_url, "sfc_" + "B" * 43)))
        self.assertEqual(exporter.polls["unauthenticated"], 1)

    def test_malformed_console_reply(self):
        cfg, exporter, console = self.make()
        self.console.health = dict(health="bogus")
        self.assertFalse(exporter.poll(console))
        self.assertEqual(exporter.polls["malformed"], 1)

    def test_prometheus_endpoint_authentication(self):
        token = em.generate_token()
        cfg, exporter, console = self.make(prometheus=dict(listen="127.0.0.1:0", token_sha256=token["token_sha256"]))
        exporter.poll(console)
        server = em.make_server(exporter)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path, auth=None, method="GET"):
            request = urllib.request.Request(url + path, method=method, headers={"Authorization": auth} if auth else {})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as error:
                return error.code, dict(error.headers), error.read()
        for auth in (None, "Bearer wrong", "Basic " + token["token"], token["token"]):
            with self.subTest(auth=auth):
                self.assertEqual(get("/metrics", auth)[0], 401)
        status, headers, body = get("/metrics", "Bearer " + token["token"])
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain; version=0.0.4"))
        self.assertIn(b"suifleet_device_healthy", body)
        self.assertEqual(get("/other", "Bearer " + token["token"])[0], 404)
        self.assertEqual(get("/metrics", "Bearer " + token["token"], method="POST")[0], 405)

    def test_otlp_export_logs_redaction_and_backoff(self):
        collector = FakeCollector(require="Bearer otlp-secret")
        self.addCleanup(collector.close)
        headers = self.write("otlp.headers", "Authorization: Bearer otlp-secret\n")
        self.console.logs["svc"] = ["boot", "token=abc123 ready"]
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, headers_file=headers, initial_backoff_seconds=2, max_backoff_seconds=8),
            logs=dict(components=["svc", "missing"], max_lines=10, max_buffer=10))
        now = [100.0]
        transport = em.Transport(cfg, exporter, clock=lambda: now[0], rng=lambda: 0.5)
        exporter.poll(console)
        self.assertEqual(exporter.polls["logs-not-found"], 1)
        em.export_once(exporter, transport)
        paths = [p for p, *_ in collector.received]
        self.assertEqual(paths, ["/v1/metrics", "/v1/logs"])
        metrics = collector.received[0][2]
        resource = {a["key"]: a["value"]["stringValue"] for a in metrics["resourceMetrics"][0]["resource"]["attributes"]}
        self.assertEqual(resource["suifleet.evidence"], "simulated")
        self.assertNotIn("service.version", resource)
        info = names_of(metrics)["suifleet_daemon_info"]["gauge"]["dataPoints"][0]
        self.assertEqual({a["key"]: a["value"]["stringValue"] for a in info["attributes"]}["version"], "0.2.9")
        names = {m["name"]: m for m in metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
        self.assertEqual(names["suifleet_device_healthy"]["gauge"]["dataPoints"][0]["asInt"], "0")
        self.assertTrue(names["suifleet_exporter_polls_total"]["sum"]["isMonotonic"])
        logs = collector.received[1][2]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual([r["body"]["stringValue"] for r in logs], ["boot", "token=[redacted] ready"])
        self.assertEqual(exporter.log_records["exported"], 2)

        # Collector outage: 503 with Retry-After, then transport failure; backoff grows, logs retained.
        self.console.logs["svc"] = ["boot", "token=abc123 ready", "third"]
        exporter.poll(console)
        collector.responses = [(503, {"Retry-After": "3"}, b""), (503, {}, b"")]
        em.export_once(exporter, transport)
        self.assertEqual(exporter.backoff["metrics"], 3.0)      # max(2*1.0 jitter, Retry-After 3)
        self.assertFalse(transport.ready("metrics"))
        self.assertEqual(len(exporter.log_buffer), 1)           # log post also failed (503) -> kept
        before = len(collector.received)
        em.export_once(exporter, transport)                       # still backing off: nothing sent
        self.assertEqual(len(collector.received), before)
        now[0] += 3.1
        collector.responses = [(500, {}, b""), (200, {}, b"{}")]
        em.export_once(exporter, transport)
        self.assertEqual(exporter.backoff["metrics"], 4.0)       # second failure doubles
        self.assertEqual(exporter.backoff["logs"], 0.0)          # logs recovered
        self.assertEqual(len(exporter.log_buffer), 0)
        now[0] += 4.1
        em.export_once(exporter, transport)
        self.assertEqual(exporter.backoff["metrics"], 0.0)
        self.assertEqual(exporter.otlp_results[("metrics", "retry")], 2)

        # Rejected payloads are not resent; partial success counts rejected records.
        self.console.logs["svc"] = ["boot", "token=abc123 ready", "third", "four", "five"]
        exporter.poll(console)
        collector.responses = [(200, {}, b"{}"), (200, {}, json.dumps({"partialSuccess": {"rejectedLogRecords": 1}}).encode())]
        em.export_once(exporter, transport)
        self.assertEqual(exporter.log_records["rejected"], 1)
        self.assertEqual(exporter.log_records["exported"], 4)
        for _, sent_headers, body, _ in collector.received:
            self.assertNotIn("abc123", json.dumps(body))
            self.assertNotIn("otlp-secret", json.dumps(body))

    def test_log_component_attribute_never_exports_a_redacted_selector(self):
        secret_name = "ab" * 32                                   # 64 hex characters: scrubbed as a secret
        self.console.logs[secret_name] = ["hidden ready"]
        self.console.logs["sensor-reader"] = ["plain ready"]
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9/"),
                                           logs=dict(components=[secret_name, "sensor-reader"], max_lines=10, max_buffer=10))
        self.assertEqual(em.redact(secret_name), em.REDACTED)
        exporter.poll(console)
        records = exporter.otlp_logs(list(exporter.log_buffer))["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        attributed = {r["body"]["stringValue"]: {a["key"]: a["value"]["stringValue"] for a in r["attributes"]}["suifleet.component"]
                      for r in records}
        self.assertEqual(attributed, {"hidden ready": em.REDACTED, "plain ready": "sensor-reader"})
        self.assertNotIn(secret_name, json.dumps(records))
        self.assertIn(secret_name, exporter.tails)                # the selector itself is still used locally

    def test_log_component_attribute_follows_the_console_secret_word_policy(self):
        # Words the console's secret_value redacts even without the longer
        # segments the regex scrubber needs (suifleet-edge/src/console.rs).
        console_secrets = ["eyJa.b.c", "sfc_x", "suiprivkey", "AKIAABCDEFGHIJKLMNOP", "eyJ.."]
        for name in console_secrets:
            self.assertEqual(em.redact(name), em.REDACTED, name)
            self.assertEqual(em.redact(f"start {name} done"), f"start {em.REDACTED} done", name)
        # Ordinary names, dotted versions and object IDs are left as they are.
        for name in ["sensor-reader", "eyJ-reader", "app.v1.2", "eyJa.b", "AKIAshort", "object 0x" + "ab" * 32 + " ok"]:
            self.assertEqual(em.redact(name), name, name)
        self.console.logs["eyJa.b.c"] = ["jwt ready"]
        self.console.logs["sensor-reader"] = ["plain ready"]
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9/"),
                                           logs=dict(components=["eyJa.b.c", "sensor-reader"], max_lines=10, max_buffer=10))
        exporter.poll(console)
        records = exporter.otlp_logs(list(exporter.log_buffer))["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        attributed = {r["body"]["stringValue"]: {a["key"]: a["value"]["stringValue"] for a in r["attributes"]}["suifleet.component"]
                      for r in records}
        self.assertEqual(attributed, {"jwt ready": em.REDACTED, "plain ready": "sensor-reader"})
        self.assertNotIn("eyJa.b.c", json.dumps(records))

    def test_truncated_collector_response_retries_and_keeps_logs(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, initial_backoff_seconds=2, max_backoff_seconds=8),
            logs=dict(components=["svc"], max_lines=10, max_buffer=10))
        now = [0.0]
        transport = em.Transport(cfg, exporter, clock=lambda: now[0], rng=lambda: 0.5)
        exporter.poll(console)
        for truncation in TRUNCATIONS:                  # success body and HTTPError paths
            collector.truncate = [truncation, truncation]
            em.export_once(exporter, transport)
            self.assertFalse(transport.ready("metrics"))
            self.assertFalse(transport.ready("logs"))
            self.assertEqual(len(exporter.log_buffer), 2)   # retained for retry
            self.assertEqual(exporter.log_records["exported"], 0)
            now[0] += 100
        self.assertEqual(exporter.otlp_results[("metrics", "unreachable")]
                         + exporter.otlp_results[("metrics", "retry")], len(TRUNCATIONS))
        # A short 2xx body is a transport failure, never a delivered batch.
        self.assertEqual(exporter.otlp_results[("logs", "ok")], 0)
        self.assertEqual(transport.failures["logs"], len(TRUNCATIONS))
        em.export_once(exporter, transport)             # collector recovers
        self.assertEqual(len(exporter.log_buffer), 0)
        self.assertEqual(exporter.log_records["exported"], 2)
        self.assertEqual(exporter.backoff["metrics"], 0.0)

    def test_partial_success_count_is_validated(self):
        # Valid OTLP int64 encodings: JSON number or decimal string.
        for body, expected in ((b'{"partialSuccess":{"rejectedLogRecords":2}}', 2),
                               (b'{"partialSuccess":{"rejectedLogRecords":"3"}}', 3),
                               (b'{"partialSuccess":{"rejectedLogRecords":"9223372036854775807"}}', 2 ** 63 - 1),
                               (b'{"partialSuccess":{}}', None), (b'{}', None), (b'', None)):
            with self.subTest(body=body):
                self.assertEqual(em._partial_rejected(body, "logs"), expected)
        malformed = (b'{"partialSuccess":{"rejectedLogRecords":1e999}}',      # inf: int() overflows
                     b'{"partialSuccess":{"rejectedLogRecords":-1}}',
                     b'{"partialSuccess":{"rejectedLogRecords":1.5}}',
                     b'{"partialSuccess":{"rejectedLogRecords":"-1"}}',
                     b'{"partialSuccess":{"rejectedLogRecords":"9223372036854775808"}}',
                     b'{"partialSuccess":{"rejectedLogRecords":' + b"9" * 400 + b'}}',
                     b'{"partialSuccess":{"rejectedLogRecords":"1e3"}}',
                     b'{"partialSuccess":{"rejectedLogRecords":true}}',
                     b'{"partialSuccess":{"rejectedLogRecords":[1]}}',
                     b'{"partialSuccess":[]}', b'not json', b'[' * 60000,
                     b'[]', b'"text"', b'null',                              # not a JSON object
                     b'{"partialSuccess":{"rejectedLogRecords":1},"x":"' + b"a" * em.MAX_OTLP_RESPONSE + b'"}')
        structural = 7    # invalid for both signals: all but the count-specific bodies
        for body in malformed:
            with self.subTest(body=body[:60]):
                self.assertIs(em._partial_rejected(body, "logs"), em._INVALID)

        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, initial_backoff_seconds=1, max_backoff_seconds=8),
            logs=dict(components=["svc"], max_lines=10, max_buffer=10))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        exporter.poll(console)
        lines = list(exporter.log_buffer)
        batch = len(lines)
        self.assertGreater(batch, 0)
        for body in malformed:
            with self.subTest(export=body[:60]):
                collector.responses = [(200, {}, body), (200, {}, body)]
                em.export_once(exporter, transport)   # must not raise
                self.assertEqual(len(exporter.log_buffer), 0)   # accepted: not resent
                self.assertTrue(transport.ready("logs"))        # no backoff
                self.assertEqual(exporter.log_records["rejected"], 0)
                self.assertEqual(exporter.log_records["exported"], 0)   # never confirmed
                exporter.log_buffer.extend(lines)
        self.assertEqual(exporter.otlp_results[("logs", "invalid_response")], len(malformed))
        self.assertEqual(exporter.otlp_results[("logs", "ok")], 0)
        # Metrics replies are judged by rejectedDataPoints, so only the
        # structurally invalid bodies are invalid for that signal.
        self.assertEqual(exporter.otlp_results[("metrics", "invalid_response")], structural)
        self.assertEqual(exporter.otlp_results[("metrics", "ok")], len(malformed) - structural)
        self.assertEqual(exporter.log_records["unconfirmed"], batch * len(malformed))
        self.assertEqual(exporter.log_records["exported"], 0)
        # A later well-formed reply is counted normally.
        collector.responses = [(200, {}, b"{}"), (200, {}, b'{"partialSuccess":{"rejectedLogRecords":"1"}}')]
        em.export_once(exporter, transport)
        self.assertEqual(exporter.log_records["rejected"], 1)
        self.assertEqual(exporter.log_records["exported"], batch - 1)
        self.assertEqual(exporter.log_records["unconfirmed"], batch * len(malformed))
        self.assertEqual(exporter.otlp_results[("logs", "partial")], 1)
        series = {m.name: m for m in exporter.metrics()}["suifleet_exporter_log_records_total"].points
        self.assertIn(({"result": "unconfirmed"}, batch * len(malformed)), series)

    def test_metrics_partial_success_is_reported_not_counted_ok(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, initial_backoff_seconds=1, max_backoff_seconds=8))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        exporter.poll(console)
        rejected_total = lambda: {m.name: m for m in exporter.metrics()}[
            "suifleet_exporter_otlp_metric_points_rejected_total"].points[0][1]
        self.assertEqual(rejected_total(), 0)
        # The collector refuses one data point: reported, never resent, no backoff.
        collector.responses = [(200, {}, b'{"partialSuccess":{"rejectedDataPoints":1,"errorMessage":"x"}}')]
        em.export_once(exporter, transport, signals=("metrics",))
        self.assertEqual(len(collector.received), 1)
        self.assertTrue(transport.ready("metrics"))
        self.assertEqual(exporter.backoff["metrics"], 0.0)
        self.assertEqual(exporter.otlp_results[("metrics", "partial")], 1)
        self.assertEqual(exporter.otlp_results[("metrics", "ok")], 0)
        self.assertEqual(rejected_total(), 1)
        # A later fully successful reply (empty or zero count) is ok and adds nothing.
        collector.responses = [(200, {}, b"{}"), (200, {}, b'{"partialSuccess":{"rejectedDataPoints":"0"}}')]
        em.export_once(exporter, transport, signals=("metrics",))
        em.export_once(exporter, transport, signals=("metrics",))
        self.assertEqual(len(collector.received), 3)
        self.assertEqual(exporter.otlp_results[("metrics", "ok")], 2)
        self.assertEqual(exporter.otlp_results[("metrics", "partial")], 1)
        self.assertEqual(rejected_total(), 1)
        # A string-encoded int64 count accumulates.
        collector.responses = [(200, {}, b'{"partialSuccess":{"rejectedDataPoints":"3"}}')]
        em.export_once(exporter, transport, signals=("metrics",))
        self.assertEqual(rejected_total(), 4)
        self.assertEqual(exporter.otlp_results[("metrics", "partial")], 2)
        series = {m.name: m for m in exporter.metrics()}["suifleet_exporter_otlp_exports_total"].points
        self.assertIn(({"signal": "metrics", "result": "partial"}, 2), series)

    def test_partial_success_count_above_sent_is_invalid(self):
        # A rejected count larger than the points/records sent is impossible:
        # it is an invalid response, never resent and never counted, so the
        # cumulative counter stays a valid OTLP int64 on the next export.
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, initial_backoff_seconds=1, max_backoff_seconds=8),
            logs=dict(components=["svc"], max_lines=10, max_buffer=10))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        exporter.poll(console)
        batch = len(exporter.log_buffer)
        self.assertGreater(batch, 0)
        huge = b'{"partialSuccess":{"rejectedDataPoints":"9223372036854775807",' \
               b'"rejectedLogRecords":"9223372036854775807"}}'
        collector.responses = [(200, {}, huge)] * 3
        em.export_once(exporter, transport)
        em.export_once(exporter, transport, signals=("metrics",))
        self.assertEqual(len(collector.received), 3)
        self.assertEqual(exporter.otlp_results[("metrics", "invalid_response")], 2)
        self.assertEqual(exporter.otlp_results[("logs", "invalid_response")], 1)
        self.assertEqual(exporter.otlp_results[("metrics", "partial")], 0)
        self.assertEqual(exporter.metric_points_rejected, 0)
        self.assertEqual(exporter.log_records["rejected"], 0)
        self.assertEqual(exporter.log_records["unconfirmed"], batch)
        self.assertEqual(len(exporter.log_buffer), 0)               # accepted: not resent
        self.assertTrue(transport.ready("metrics"))
        # One more than sent is invalid; exactly the number sent is a valid partial.
        sent = em._payload_items(exporter.otlp_metrics(), "metrics")
        self.assertGreater(sent, 0)
        for count, result in ((sent + 1, "invalid_response"), (sent, "partial")):
            collector.responses = [(200, {}, b'{"partialSuccess":{"rejectedDataPoints":%d}}' % count)]
            em.export_once(exporter, transport, signals=("metrics",))
            self.assertEqual(exporter.otlp_results[("metrics", result)], 3 if result == "invalid_response" else 1)
        self.assertEqual(exporter.metric_points_rejected, sent)
        # The next healthy export carries a valid int64 counter and health.
        collector.responses = [(200, {}, b"{}")]
        em.export_once(exporter, transport, signals=("metrics",))
        metrics = {m["name"]: m for m in collector.received[-1][2]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
        point = metrics["suifleet_exporter_otlp_metric_points_rejected_total"]["sum"]["dataPoints"][0]
        self.assertEqual(int(point["asInt"]), sent)
        self.assertLessEqual(int(point["asInt"]), 2 ** 63 - 1)
        self.assertIn("suifleet_up", metrics)
        self.assertEqual(em._payload_items(exporter.otlp_logs([(0.0, "svc", "a"), (0.0, "svc", "b")]), "logs"), 2)

    def test_oversized_retry_after_is_capped_and_keeps_logs(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, initial_backoff_seconds=1, max_backoff_seconds=8),
            logs=dict(components=["svc"], max_lines=10, max_buffer=10))
        now = [0.0]
        transport = em.Transport(cfg, exporter, clock=lambda: now[0], rng=lambda: 0.5)
        exporter.poll(console)
        for value in ("9" * 400, "9" * 5000, "1" * 10, "\u00b2", "-5", "Wed, 21 Oct 2015 07:28:00 GMT"):
            with self.subTest(retry_after=value[:20]):
                collector.responses = [(429, {"Retry-After": value}, b""), (429, {"Retry-After": value}, b"")]
                em.export_once(exporter, transport)    # must not raise
                expected = 8.0 if value.isascii() and value.isdigit() else 1.0
                self.assertEqual(exporter.backoff["metrics"], expected)
                self.assertEqual(len(exporter.log_buffer), 2)   # retained for retry
                transport._success("metrics")
                transport._success("logs")
        self.assertEqual(exporter.otlp_results[("metrics", "retry")], 6)
        self.assertEqual(em._retry_after(" 7 ", 8), 7)
        self.assertEqual(em._retry_after("123456789", 8), 123456789)   # capped later by _failure
        self.assertIsNone(em._retry_after(None, 8))
        em.export_once(exporter, transport)             # collector recovers
        self.assertEqual(len(exporter.log_buffer), 0)
        self.assertEqual(exporter.log_records["exported"], 2)

    def test_otlp_unauthorized_backs_off(self):
        collector = FakeCollector(require="Bearer right")
        self.addCleanup(collector.close)
        headers = self.write("otlp.headers", "Authorization: Bearer wrong\n")
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(
            endpoint=collector.url, headers_file=headers, initial_backoff_seconds=1, max_backoff_seconds=4))
        now = [0.0]
        transport = em.Transport(cfg, exporter, clock=lambda: now[0], rng=lambda: 1.0)
        exporter.poll(console)
        delays = []
        for _ in range(5):
            em.export_once(exporter, transport)
            delays.append(exporter.backoff["metrics"])
            now[0] += 100
        self.assertEqual(delays, [1.2, 2.4, 4.0, 4.0, 4.0])     # +20 % jitter, capped
        self.assertEqual(exporter.otlp_results[("metrics", "unauthorized")], 5)

    def test_unreachable_collector_and_bounded_log_buffer(self):
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9", timeout_seconds=1),
                                           logs=dict(components=["svc"], max_lines=200, max_buffer=10))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        self.console.logs["svc"] = [f"line {i}" for i in range(50)]
        exporter.poll(console)
        em.export_once(exporter, transport)
        self.assertEqual(exporter.otlp_results[("metrics", "unreachable")], 1)
        self.assertEqual(len(exporter.log_buffer), 10)
        self.assertEqual(exporter.log_records["dropped"], 40)
        self.assertEqual(exporter.log_buffer[-1][2], "line 49")
        self.console.logs["svc"] = [f"other {i}" for i in range(3)]
        exporter.poll(console)
        self.assertEqual(exporter.log_gaps, 1)

    def test_run_loop_serves_and_stops(self):
        collector = FakeCollector()
        self.addCleanup(collector.close)
        cfg, _, _ = self.make(otlp=dict(endpoint=collector.url))
        cfg.interval = 10
        stop = threading.Event()
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault("exporter", em.run(
            cfg, stop=stop, ready=lambda e, s: holder.update(server=s))))
        thread.start()
        deadline = time.time() + 10
        while not collector.received and time.time() < deadline:
            time.sleep(0.05)
        port = holder["server"].server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            self.assertIn(b"suifleet_up", response.read())
        stop.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(collector.received[0][0], "/v1/metrics")

    def wait(self, predicate, seconds=10):
        deadline = time.time() + seconds
        while not predicate():
            if time.time() > deadline:
                self.fail("condition not reached")
            time.sleep(0.02)

    def test_delayed_collector_does_not_stall_console_polling(self):
        collector = FakeCollector()
        collector.gate = threading.Event()             # every collector reply waits
        self.addCleanup(collector.close)
        cfg, _, _ = self.make(otlp=dict(endpoint=collector.url, timeout_seconds=30),
                              logs=dict(components=["svc"]))
        cfg.interval = 0.2
        stop = threading.Event()
        holder = {}
        thread = threading.Thread(target=lambda: em.run(cfg, stop=stop, ready=lambda e, s: holder.update(e=e, s=s)))
        thread.start()
        self.addCleanup(lambda: (stop.set(), collector.gate.set(), thread.join(35)))
        self.wait(lambda: collector.arrived)            # first export is now blocked in flight
        exporter = holder["e"]
        polls = exporter.polls["ok"]
        self.wait(lambda: exporter.polls["ok"] >= polls + 3, seconds=5)
        self.assertEqual(len(collector.arrived), 1)     # still the same pending request
        self.console.status = "unavailable"             # daemon goes down during the delay
        self.wait(lambda: not exporter.up, seconds=5)
        self.assertEqual(len(collector.arrived), 1)
        port = holder["s"].server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            text = response.read().decode()
        self.assertIn('suifleet_up{evidence="simulated",device="sim-1"} 0', text)
        self.assertNotIn("suifleet_device_healthy", text)   # stale health withdrawn
        self.console.status = None
        collector.gate.set()                            # collector recovers
        self.wait(lambda: exporter.up and exporter.otlp_results[("metrics", "ok")] >= 2)
        stop.set()
        thread.join(35)
        self.assertFalse(thread.is_alive())

    def test_log_batch_in_flight_is_requeued_within_the_buffer_bound(self):
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9"),
                                           logs=dict(components=["svc"], max_lines=200, max_buffer=10))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        self.console.logs["svc"] = [f"line {i}" for i in range(8)]
        exporter.poll(console)
        seen = {}

        def post(signal_name, payload):
            if signal_name == "metrics":
                return ("ok", None)
            # While the batch is in flight the poll loop buffers 5 new lines.
            seen["buffered"] = exporter.render_prometheus()
            self.console.logs["svc"] = [f"line {i}" for i in range(13)]
            exporter.poll(console)
            seen["after"] = exporter.render_prometheus()
            transport.failures["logs"] = 1
            transport.not_before["logs"] = 1.0
            return ("retry", None)
        with unittest.mock.patch.object(transport, "post", side_effect=post):
            em.export_once(exporter, transport)
        self.assertIn(b'suifleet_exporter_log_buffered{evidence="simulated",device="sim-1"} 8', seen["buffered"])
        # The 8 in-flight lines count against max_buffer=10 while pending: only
        # 2 new lines are held, the oldest queued ones give way to newer lines.
        self.assertIn(b'suifleet_exporter_log_buffered{evidence="simulated",device="sim-1"} 10', seen["after"])
        self.assertEqual([line for _, _, line in exporter.log_buffer],
                         [f"line {i}" for i in range(8)] + ["line 11", "line 12"])
        self.assertEqual(exporter.log_records["dropped"], 3)
        self.assertEqual(exporter.log_inflight, 0)
        transport.not_before["logs"] = 0.0
        with unittest.mock.patch.object(transport, "post", return_value=("ok", None)):
            em.export_once(exporter, transport)
        self.assertEqual(len(exporter.log_buffer), 0)
        self.assertEqual(exporter.log_records["exported"], 10)

    def test_whole_buffer_in_flight_keeps_the_bound_at_saturation(self):
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9"),
                                           logs=dict(components=["svc"], max_lines=200, max_buffer=10))
        transport = em.Transport(cfg, exporter, clock=lambda: 0.0, rng=lambda: 0.5)
        self.console.logs["svc"] = [f"line {i}" for i in range(10)]
        exporter.poll(console)
        seen = {}

        def post(signal_name, payload):
            if signal_name == "metrics":
                return ("ok", None)
            self.console.logs["svc"] = [f"line {i}" for i in range(30)]
            exporter.poll(console)                       # 20 new lines while all 10 are in flight
            seen["held"] = len(exporter.log_buffer) + exporter.log_inflight
            seen["text"] = exporter.render_prometheus()
            return ("ok", None)
        with unittest.mock.patch.object(transport, "post", side_effect=post):
            more = em.export_once(exporter, transport)
        self.assertEqual(seen["held"], 10)
        self.assertIn(b'suifleet_exporter_log_buffered{evidence="simulated",device="sim-1"} 10', seen["text"])
        self.assertEqual(exporter.log_records["dropped"], 20)
        self.assertEqual(exporter.log_records["exported"], 10)
        self.assertFalse(more)
        self.assertEqual(len(exporter.log_buffer), 0)

    def test_refilled_log_queue_does_not_starve_metrics(self):
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9"),
                                           logs=dict(components=["svc"], max_lines=200))
        transport = em.Transport(cfg, exporter)
        stop, wakeup = threading.Event(), threading.Event()
        posts, counter = [], [0]
        lock = threading.Lock()

        def refill():
            # The poll loop: every log POST is followed by a poll that adds
            # more lines and signals the worker, as under sustained load.
            counter[0] += 1
            self.console.logs["svc"] = [f"line {counter[0]}-{i}" for i in range(5)]
            exporter.poll(console)
            wakeup.set()

        def post(signal_name, payload):
            with lock:
                if signal_name == "metrics":
                    up = [m for m in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"] if m["name"] == "suifleet_up"]
                    posts.append(("metrics", up[0]["gauge"]["dataPoints"][0]["asInt"]))
                else:
                    posts.append(("logs", None))
                    if sum(1 for p in posts if p[0] == "logs") == 3:
                        self.console.status = "unavailable"   # daemon goes down mid-stream
                        self.console.logs["svc"] = []
                        exporter.poll(console)
                        wakeup.set()
                    elif self.console.status is None:
                        refill()
                if len(posts) >= 12:
                    stop.set()
            return ("ok", None)
        self.console.logs["svc"] = [f"line 0-{i}" for i in range(5)]
        exporter.poll(console)
        wakeup.set()
        with unittest.mock.patch.object(transport, "post", side_effect=post):
            worker = threading.Thread(target=em.export_loop, args=(exporter, transport, stop, wakeup))
            worker.start()
            self.wait(lambda: ("metrics", "0") in posts or stop.is_set(), seconds=10)
            stop.set()
            wakeup.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        # Metrics are re-sent between log batches whenever a poll happened,
        # and the daemon-down transition reaches the collector.
        self.assertIn(("metrics", "0"), posts)
        kinds = [k for k, _ in posts]
        self.assertNotIn(["logs", "logs"], [kinds[i:i + 2] for i in range(len(kinds) - 1)])

    def test_due_metric_retry_is_sent_between_log_batches(self):
        # A metrics retry whose backoff elapses during a log request is sent
        # before the next batch even though no console poll wakes the worker.
        cfg, exporter, console = self.make(prometheus=None, otlp=dict(endpoint="http://127.0.0.1:9", timeout_seconds=30),
                                           logs=dict(components=["svc"], max_buffer=4096))
        now = [0.0]
        transport = em.Transport(cfg, exporter, clock=lambda: now[0], rng=lambda: 0.5)
        stop, wakeup = threading.Event(), threading.Event()
        posts = []
        exporter.poll(console)
        with exporter.lock:
            exporter.log_buffer.extend((0.0, "svc", f"line {i}") for i in range(2048))

        def post(signal_name, payload):
            posts.append((signal_name, now[0]))
            if signal_name == "metrics" and len(posts) == 1:
                transport._failure("metrics", None)       # retry eligible at t=5
                return ("retry", None)
            if signal_name == "logs":
                now[0] += 20                              # slow but successful collector
            if len(posts) >= 6:
                stop.set()
            transport._success(signal_name)
            return ("ok", None)
        wakeup.set()
        with unittest.mock.patch.object(transport, "post", side_effect=post):
            worker = threading.Thread(target=em.export_loop, args=(exporter, transport, stop, wakeup))
            worker.start()
            self.wait(lambda: stop.is_set(), seconds=10)
            stop.set()
            wakeup.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(posts[:3], [("metrics", 0.0), ("logs", 0.0), ("metrics", 20.0)])
        self.assertEqual(posts[3], ("logs", 20.0))
        self.assertEqual(transport.failures["metrics"], 0)



if __name__ == "__main__":
    unittest.main()
