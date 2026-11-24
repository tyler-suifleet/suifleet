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
                body = self.rfile.read(int(self.headers["Content-Length"]))
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
        path = self.write("config.json", "{}", 0o640)
        self.assertEqual(em.main(["--config", path]), 0)

    def test_disabled_main_opens_no_socket(self):
        path = self.write("config.json", json.dumps(dict(enabled=False)), 0o640)
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

    def test_secret_files_are_protected(self):
        wide = self.write("wide.token", CONSOLE_TOKEN, 0o640)
        link = self.dir / "link.token"
        link.symlink_to(self.token_file)
        wrong = self.write("wrong.token", "hunter2", 0o600)
        for path in (wide, str(link), wrong, "relative.token"):
            with self.subTest(path=path), self.assertRaises(em.ConfigError):
                em.Config(self.config(console=dict(url="http://127.0.0.1:1441", token_file=path)))
        for text in ("Host: evil", "NoColon", "Authorization: a\nauthorization: b", "X: a\x01"):
            headers = self.write("h", text)
            with self.subTest(text=text), self.assertRaises(em.ConfigError):
                em.Config(self.config(otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers)))

    def test_rejected_configuration_message_has_no_secret(self):
        headers = self.write("h", "Authorization: Bearer topsecretvalue\nHost: x\n")
        path = self.write("config.json", json.dumps(self.config(
            otlp=dict(endpoint="http://127.0.0.1:4318", headers_file=headers))), 0o640)
        with unittest.mock.patch("sys.stderr") as stderr:
            self.assertEqual(em.main(["--config", path]), 2)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("rejected", written)
        self.assertNotIn("topsecretvalue", written)

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
        self.assertEqual(resource["service.version"], "0.2.9")
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



if __name__ == "__main__":
    unittest.main()
