#!/usr/bin/env python3
"""SF-070 industrial collector tests: real collector against local simulators.

Codec/configuration vectors, then the actual collector threads polling and
subscribing over real TCP, TLS and pseudo-terminal connections to the
deterministic Modbus and OPC UA simulators. Covers interoperability, typing,
timestamps and quality, wrong credentials and certificates, malformed frames,
disconnects, resubscription, bounded backpressure and reconnect backoff.
No network beyond loopback, no hardware, no keys outside a temporary directory.

OPC UA Basic256Sha256 cases need the `cryptography` package (make
test-industrial unpacks the hash-pinned wheels); they fail rather than skip.
"""
import json
import math
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from industrial import collector as col, modbus, opcua_codec as ua, opcua_secure as sec  # noqa: E402
from industrial import opcua_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "interfaces/compat/industrial/config-cases.json").read_text())


def wait_until(condition, timeout=10.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(message)


class MemorySink:
    """Collector output for in-process tests; `blocked` simulates a full stream."""

    def __init__(self):
        self.messages, self.lock, self.blocked, self.attempts = [], threading.Lock(), False, 0

    def append(self, message):
        with self.lock:
            self.attempts += 1
            if self.blocked:
                raise col.SinkError("full")
            self.messages.append(json.loads(json.dumps(message)))
            return len(self.messages) - 1

    def publish(self, record):
        return True

    def records(self, kind=None, **match):
        with self.lock:
            out = [r for m in self.messages for r in m["records"]]
        return [r for r in out if (kind is None or r["kind"] == kind) and all(r.get(k) == v for k, v in match.items())]


class Certificates:
    """A throwaway CA, server/client certificates and an unrelated CA, made with openssl."""

    def __init__(self, directory):
        self.d = Path(directory)

        def run(*args):
            subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=self.d)
        for ca in ("ca", "other-ca"):
            run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{ca}.key", "-out", f"{ca}.pem", "-days", "2",
                "-subj", f"/CN=sf070-{ca}")
        (self.d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1,URI:urn:suifleet:test\n"
                                        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment,"
                                        "dataEncipherment,nonRepudiation,keyCertSign\n"
                                        "extendedKeyUsage=serverAuth,clientAuth\n")
        for name, ca in (("server", "ca"), ("client", "ca"), ("rogue", "other-ca")):
            run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
                "-subj", f"/CN=sf070-{name}")
            run("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.pem", "-CAkey", f"{ca}.key", "-CAcreateserial",
                "-out", f"{name}.pem", "-days", "2", "-extfile", "ext.cnf")
            run("x509", "-in", f"{name}.pem", "-outform", "DER", "-out", f"{name}.der")
        for key in self.d.glob("*.key"):
            key.chmod(0o600)

    def path(self, name):
        return str(self.d / name)

    def der(self, name):
        return (self.d / f"{name}.der").read_bytes()


def base_config(sources, **output):
    out = {"socket": "/run/suifleet/ipc.sock", "component": "industrial", "stream": "industrial",
           "max_buffered_records": 1000, "retry_min_ms": 20, "retry_max_ms": 100}
    out.update(output)
    return {"version": 1, "collector_id": "test-collector", "output": out, "sources": sources}


FAST = {"connect_timeout_ms": 1000, "request_timeout_ms": 400, "reconnect_min_ms": 100, "reconnect_max_ms": 400}


class Harness(unittest.TestCase):
    def start(self, raw, sink=None):
        config = col.load_secrets(col.validate(raw))
        self.sink = sink or MemorySink()
        self.logs = []
        self.collector = col.Collector(config, sink=self.sink, log=self.logs.append)
        self.collector.start()
        self.addCleanup(self.collector.stop, 1.0)
        return self.collector

    def source(self, name):
        return next(s for s in self.collector.sources if s.s["name"] == name)


# --- Codec and protocol vectors -------------------------------------------------

class CodecTests(unittest.TestCase):
    def encode_node(self, text):
        w = ua.Writer()
        ua.put_nodeid(w, ua.NodeId.parse(text))
        return w.bytes()

    def test_nodeid_encodings(self):
        # OPC 10000-6 5.2.2.9: TwoByte, FourByte, Numeric and String forms.
        self.assertEqual(self.encode_node("i=72").hex(), "0048")
        self.assertEqual(self.encode_node("ns=5;i=1025").hex(), "01050104")
        self.assertEqual(self.encode_node("ns=2;i=70000").hex(), "02020070110100")
        self.assertEqual(self.encode_node("ns=1;s=Hot").hex(), "030100" "03000000" "486f74")
        for text in ("i=72", "ns=5;i=1025", "ns=2;i=70000", "ns=1;s=Hot", "ns=3;g=72962b91-fa75-4ae6-8d28-b404dc7daf63",
                     "ns=4;b=0102ff"):
            self.assertEqual(str(ua.get_nodeid(ua.Reader(self.encode_node(text)))), text)
        for bad in ("x=1", "ns=;i=1", "ns=70000;i=1", "i=4294967296", "s="):
            with self.assertRaises(ValueError):
                ua.NodeId.parse(bad)

    def test_datetime_and_datavalue(self):
        self.assertEqual(ua.datetime_to_ns(ua.EPOCH_OFFSET_100NS), 0)
        self.assertEqual(ua.datetime_to_ns(ua.ns_to_datetime(1_700_000_000_123_456_700)), 1_700_000_000_123_456_700)
        self.assertIsNone(ua.datetime_to_ns(0))
        self.assertIsNone(ua.datetime_to_ns(0x7FFFFFFFFFFFFFFF))
        dv = ua.DataValue(ua.Variant(ua.DOUBLE, 21.5), 0x40900000, 1234, 5678)
        w = ua.Writer()
        ua.put_datavalue(w, dv)
        self.assertEqual(w.bytes()[0], 0x0F)
        back = ua.get_datavalue(ua.Reader(w.bytes()))
        self.assertEqual((back.value, back.status, back.source_timestamp, back.server_timestamp),
                         (ua.Variant(ua.DOUBLE, 21.5), 0x40900000, 1234, 5678))
        self.assertEqual(ua.quality(0), "GOOD")
        self.assertEqual(ua.quality(0x40900000), "UNCERTAIN")
        self.assertEqual(ua.quality(0x80340000), "BAD")
        self.assertEqual(ua.status_name(0x80340000), "BadNodeIdUnknown")
        self.assertEqual(ua.status_name(ua.OVERFLOW_BITS), "Good+Overflow")

    def test_message_round_trip(self):
        request = ua.make("ReadRequest", request_header=ua.make("RequestHeader", request_handle=7),
                          timestamps_to_return=2, nodes_to_read=[ua.make("ReadValueId", node_id=ua.NodeId(2, "T"),
                                                                         attribute_id=13)])
        name, back = ua.decode_message(ua.encode_message("ReadRequest", request))
        self.assertEqual(name, "ReadRequest")
        self.assertEqual(back["nodes_to_read"][0]["node_id"], ua.NodeId(2, "T"))
        self.assertEqual(ua.encode_message("ReadRequest", request)[:4].hex(), "01007702")   # FourByte i=631

    def test_bounded_decoding(self):
        cases = {
            "truncated": bytes([ua.DOUBLE]) + b"\x00\x00",
            "huge array": bytes([ua.INT32 | 0x80]) + struct.pack("<i", 10_000_000),
            "bad type": bytes([31]),
            "invalid utf-8": bytes([ua.STRING]) + struct.pack("<i", 2) + b"\xff\xfe",
            "dimensions without array": bytes([ua.INT32 | 0x40]) + b"\0\0\0\0",
            "nesting": bytes([ua.VARIANT]) * 20 + bytes([ua.INT32]) + b"\0\0\0\0",
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ua.DecodeError):
                ua.get_variant(ua.Reader(data))
        with self.assertRaises(ua.DecodeError):
            ua.decode_message(ua.encode_message("CloseSessionResponse", ua.make("CloseSessionResponse")) + b"x")
        with self.assertRaises(ua.DecodeError):
            ua.decode_message(b"\x01\x00\x99\x99")

    def test_p_sha256(self):
        # Published TLS 1.2 PRF (P_SHA256) test vector; OPC 10000-6 6.7.5 uses P_SHA256 unchanged.
        secret = bytes.fromhex("9bbe436ba940f017b17652849a71db35")
        seed = b"test label" + bytes.fromhex("a0ba9f936cda311827a6f796ffd5198c")
        expected = ("e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a6b301791e90d35c9c9a46b4e14baf9af"
                    "0fa022f7077def17abfd3797c0564bab4fbc91666e9def9b97fce34f796789baa48082d122ee42c5a72e5a5110fff701"
                    "87347b66")
        self.assertEqual(sec.p_sha256(secret, seed, 100).hex(), expected)

    def test_modbus_vectors(self):
        # MODBUS over Serial Line V1.02 6.2.2 example and the common 01 03 00 00 00 0A frame.
        self.assertEqual(modbus.crc16(bytes.fromhex("0207")), 0x1241)
        self.assertEqual(struct.pack("<H", modbus.crc16(bytes.fromhex("01030000000a"))).hex(), "c5cd")
        self.assertEqual(modbus.read_request(3, 0x006B, 3).hex(), "03006b0003")     # V1.1b3 6.3 example
        self.assertEqual(modbus.parse_read_response(3, 3, bytes.fromhex("0306022b00000064")), [0x022B, 0, 0x64])
        self.assertEqual(modbus.parse_read_response(1, 19, bytes.fromhex("0103cd6b05")),           # V1.1b3 6.1 example
                         [bool(int(b)) for b in "1011001111010110101"])
        self.assertEqual(modbus.decode_registers("float32", [0x41AC, 0x0000]), 21.5)
        self.assertEqual(modbus.decode_registers("float32", [0x0000, 0x41AC], "little"), 21.5)
        self.assertEqual(modbus.decode_registers("int32", [0xFFFF, 0xFFFE]), -2)
        self.assertEqual(modbus.decode_registers("uint64", modbus.encode_registers("uint64", 2**63 + 5)), 2**63 + 5)
        with self.assertRaises(modbus.ExceptionResponse) as raised:
            modbus.parse_read_response(3, 1, b"\x83\x02")
        self.assertEqual(raised.exception.code, 2)
        for bad in (b"\x03\x04\x00\x01", b"\x04\x02\x00\x01", b"\x83", b"\x01\x01\xff", b""):
            with self.subTest(bad=bad.hex()), self.assertRaises(modbus.MalformedResponse):
                modbus.parse_read_response(3 if bad[:1] != b"\x01" else 1, 1 if bad[:1] != b"\x01" else 3, bad)
        for args in ((3, 0, 126), (1, 0, 2001), (3, 65535, 2), (5, 0, 1)):
            with self.assertRaises(ValueError):
                modbus.read_request(*args)


class ConfigTests(unittest.TestCase):
    def test_shared_cases(self):
        """interfaces/compat/industrial/config-cases.json: every vector, valid and invalid."""
        base = CASES["base"]
        for case in CASES["cases"]:
            with self.subTest(case["name"]):
                raw = json.loads(json.dumps(base))
                for path, value in case.get("set", {}).items():
                    target = raw
                    keys = path.split(".")
                    for key in keys[:-1]:
                        target = target[int(key)] if isinstance(target, list) else target[key]
                    last = int(keys[-1]) if isinstance(target, list) else keys[-1]
                    if value == "$delete":
                        del target[last]
                    else:
                        target[last] = value
                if case["valid"]:
                    col.validate(raw)
                else:
                    with self.assertRaises(col.ConfigError) as raised:
                        col.validate(raw)
                    self.assertIn(case["error"], str(raised.exception))
        self.assertGreaterEqual(len(CASES["cases"]), 30)

    def test_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp, "password")
            secret.write_text("pw\n")
            secret.chmod(0o644)
            raw = base_config([{"name": "opc", "protocol": "opcua", "endpoint_url": "opc.tcp://127.0.0.1:1",
                                "identity": {"type": "username", "username": "op", "password_file": str(secret)},
                                "allow_plaintext_password": True, "points": [{"name": "t", "node_id": "ns=2;s=T"}]}])
            with self.assertRaisesRegex(col.ConfigError, "group or others"):
                col.load_secrets(col.validate(raw))
            secret.chmod(0o600)
            config = col.load_secrets(col.validate(raw))
            self.assertEqual(config["sources"][0]["password"], b"pw")
            Path(tmp, "dir").mkdir(mode=0o700)
            raw["sources"][0]["identity"]["password_file"] = str(Path(tmp, "dir"))
            with self.assertRaisesRegex(col.ConfigError, "regular file"):
                col.load_secrets(col.validate(raw))


# --- Modbus ---------------------------------------------------------------------------

def modbus_points():
    return [
        {"name": "temperature", "table": "holding_register", "address": 0, "type": "float32"},
        {"name": "energy", "table": "holding_register", "address": 2, "type": "int32", "word_order": "little"},
        {"name": "speed", "table": "holding_register", "address": 4, "type": "uint16"},
        {"name": "total", "table": "input_register", "address": 10, "type": "uint64"},
        {"name": "pressure", "table": "input_register", "address": 20, "type": "float64"},
        {"name": "running", "table": "coil", "address": 3, "type": "bool"},
        {"name": "door", "table": "discrete_input", "address": 7, "type": "bool"},
    ]


def modbus_model():
    m = modbus.DataModel(unit=17)
    m.set_value("holding_register", 0, "float32", 21.5)
    m.set_value("holding_register", 2, "int32", -123456, "little")
    m.set_value("holding_register", 4, "uint16", 1500)
    m.set_value("input_register", 10, "uint64", 2**40 + 7)
    m.set_value("input_register", 20, "float64", -0.125)
    m.set_bit("coil", 3, True)
    m.set_bit("discrete_input", 7, False)
    return m


EXPECTED_MODBUS = {"temperature": ("float32", 21.5), "energy": ("int32", -123456), "speed": ("uint16", 1500),
                   "total": ("uint64", 2**40 + 7), "pressure": ("float64", -0.125), "running": ("bool", True),
                   "door": ("bool", False)}


class ModbusTests(Harness):
    def setUp(self):
        self.model = modbus_model()
        self.sim = modbus.Simulator(self.model)
        self.addCleanup(self.sim.close)

    def config(self, **extra):
        source = {"name": "plc", "protocol": "modbus", "transport": "tcp", "host": "127.0.0.1", "port": self.sim.port,
                  "unit_id": 17, "poll_interval_ms": 50, "points": modbus_points(), **FAST}
        source.update(extra)
        return base_config([source])

    def latest(self, point):
        samples = self.sink.records("sample", point=point)
        return samples[-1] if samples else None

    def test_interoperability_types_timestamps_quality(self):
        self.start(self.config())
        wait_until(lambda: all(self.latest(p) for p in EXPECTED_MODBUS), message="no samples")
        before = time.time_ns()
        for point, (kind, value) in EXPECTED_MODBUS.items():
            sample = self.latest(point)
            self.assertEqual((sample["type"], sample["value"], sample["quality"], sample["status"]), (kind, value, "GOOD", "good"))
            self.assertIsNone(sample["source_timestamp_ns"])
            self.assertEqual(sample["timestamp_ns"], sample["received_ns"])
            self.assertLessEqual(sample["received_ns"], before)
        self.model.set_value("holding_register", 0, "float32", 22.25)
        wait_until(lambda: self.latest("temperature")["value"] == 22.25)
        # Adjacent points share requests: holding 0-4 (one read), input 10-13 and 20-23, coil, discrete.
        self.assertEqual(len(self.source("plc").plan), 5)
        status = self.sink.records("status", source="plc")
        self.assertEqual(status[0]["state"], "connected")
        ids = [r["id"] for r in self.sink.records()]
        self.assertEqual(len(ids), len(set(ids)), "record ids are unique")
        message = self.sink.messages[0]
        self.assertEqual((message["schema"], message["collector"]), (1, "test-collector"))

    def test_exception_response_is_bad_quality_once_then_recovers(self):
        self.model.exceptions[(3, 4)] = 2
        self.start(self.config())
        wait_until(lambda: self.latest("speed") and self.latest("total"))
        time.sleep(0.4)
        bad = self.sink.records("sample", point="speed", quality="BAD")
        self.assertEqual(len(bad), 1, "a persisting exception is reported once")
        self.assertEqual(bad[0]["status"], "exception:2 illegal-data-address")
        self.assertIsNone(bad[0]["value"])
        self.assertEqual(self.latest("total")["quality"], "GOOD", "other requests are unaffected")
        self.assertEqual(self.sink.records("status", state="disconnected"), [], "an exception keeps the connection")
        del self.model.exceptions[(3, 4)]
        wait_until(lambda: self.latest("speed")["quality"] == "GOOD")

    def test_malformed_responses_reconnect(self):
        self.start(self.config())
        wait_until(lambda: self.latest("temperature"))
        for fault in ("wrong-transaction", "wrong-unit", "bad-length", "bad-byte-count", "garbage", "close", "stall"):
            with self.subTest(fault):
                connects = len(self.sink.records("status", state="connected"))
                bad = len(self.sink.records("sample", point="temperature", status="disconnected"))
                self.sim.fault = fault
                wait_until(lambda: len(self.sink.records("status", state="connected")) > connects, 5,
                           f"no reconnect after {fault}")
                reason = self.sink.records("status", state="disconnected")[-1]["reason"]
                self.assertTrue(reason.startswith(("MalformedResponse", "ResponseTimeout", "ModbusError")), reason)
                self.assertEqual(len(self.sink.records("sample", point="temperature", status="disconnected")), bad + 1)
                self.model.set_value("holding_register", 4, "uint16", 1000 + len(fault))
                wait_until(lambda: self.latest("speed")["value"] == 1000 + len(fault))

    def test_non_finite_float_is_bad(self):
        self.model.set_registers("holding_register", 0, [0x7FC0, 0x0000])          # float32 NaN
        self.start(self.config())
        wait_until(lambda: self.latest("temperature"))
        sample = self.latest("temperature")
        self.assertEqual((sample["value"], sample["quality"], sample["status"]), (None, "BAD", "non-finite"))

    def test_server_outage_backoff_and_recovery(self):
        port = self.sim.port
        self.start(self.config(reconnect_min_ms=100, reconnect_max_ms=400))
        wait_until(lambda: self.latest("temperature"))
        self.sim.close()
        wait_until(lambda: self.sink.records("status", state="disconnected"))
        time.sleep(1.6)
        waits = self.source("plc").stats["reconnect_waits"]
        # 100, 200, 400, 400 ms...: at most ~6 attempts in 1.6 s, never a tight loop.
        self.assertTrue(2 <= waits <= 7, waits)
        bad = self.sink.records("sample", point="temperature", quality="BAD")
        self.assertEqual(len(bad), 1, "an outage is reported once per point")
        self.sim = modbus.Simulator(self.model, port=port)
        wait_until(lambda: self.latest("temperature")["quality"] == "GOOD", 5)

    def test_backpressure_skips_polls_and_reports_gap(self):
        sink = MemorySink()
        sink.blocked = True
        self.start(self.config(points=modbus_points()[:2]) | {"output": base_config([])["output"] | {
            "max_buffered_records": 6, "retry_min_ms": 20, "retry_max_ms": 50}}, sink)
        wait_until(lambda: self.source("plc").skipped >= 5, 5, "polls not skipped")
        self.assertLessEqual(len(self.collector.buffer), 6, "buffer is bounded")
        appended_before = len(sink.records())
        sink.blocked = False
        gap = wait_until(lambda: sink.records("gap", source="plc"), 5, "no gap record")[0]
        self.assertEqual(gap["reason"], "backpressure")
        self.assertGreaterEqual(gap["skipped_cycles"], 5)
        self.assertLess(gap["from_ns"], gap["to_ns"])
        self.assertEqual(appended_before, 0)
        records = sink.records()
        self.assertEqual([r["id"] for r in records], sorted((r["id"] for r in records), key=lambda i: int(i.split(":")[-1])),
                         "records leave the buffer in order")
        self.assertEqual(self.collector.buffer.dropped, 0, "backpressure skips polls instead of dropping records")
        batches = [m["batch"] for m in sink.messages]
        self.assertEqual(len(batches), len(set(batches)), "each stored message has its own batch id")
        self.assertGreater(sink.attempts, len(sink.messages), "failed appends were retried")

    def test_lost_acknowledgement_resends_identical_batch(self):
        class AckLost(MemorySink):
            """Stores the first message, then reports a failure, as after an IPC timeout."""
            def append(self, message):
                stored = super().append(message)
                if len(self.messages) == 1:
                    raise col.SinkError("ipc: timed out")
                return stored
        sink = AckLost()
        self.start(self.config(), sink)
        wait_until(lambda: len(sink.messages) >= 3)
        self.assertEqual(sink.messages[0], sink.messages[1], "the retry is byte-for-byte the same message")
        unique = {m["batch"]: m for m in sink.messages}
        ids = [r["id"] for m in unique.values() for r in m["records"]]
        self.assertEqual(len(ids), len(set(ids)), "deduplicating by batch removes the duplicate")

    def test_tls_mutual_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            certs = Certificates(tmp)
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.minimum_version = ssl.TLSVersion.TLSv1_2
            server_tls.load_cert_chain(certs.path("server.pem"), certs.path("server.key"))
            server_tls.verify_mode = ssl.CERT_REQUIRED
            server_tls.load_verify_locations(certs.path("ca.pem"))
            tls_sim = modbus.Simulator(self.model, tls=server_tls)
            self.addCleanup(tls_sim.close)

            def config(name, client, ca="ca.pem", server_name="localhost"):
                return {"name": name, "protocol": "modbus", "transport": "tls", "host": "127.0.0.1", "port": tls_sim.port,
                        "unit_id": 17, "poll_interval_ms": 50, "points": modbus_points()[:1], **FAST,
                        "tls": {"ca_file": certs.path(ca), "certificate_file": certs.path(f"{client}.pem"),
                                "private_key_file": certs.path(f"{client}.key"), "server_name": server_name}}
            plain = {"name": "plaintext", "protocol": "modbus", "transport": "tcp", "host": "127.0.0.1",
                     "port": tls_sim.port, "unit_id": 17, "poll_interval_ms": 50, "points": modbus_points()[:1], **FAST}
            self.start(base_config([config("good", "client"), config("untrusted-client", "rogue"),
                                    config("untrusted-server", "client", ca="other-ca.pem"),
                                    config("wrong-name", "client", server_name="plc.example"), plain]))
            wait_until(lambda: self.sink.records("sample", source="good", quality="GOOD"))
            wait_until(lambda: len({r["source"] for r in self.sink.records("status") if r["state"] in ("rejected", "disconnected")}) == 4,
                       10, "rejections not reported")
            for name in ("untrusted-client", "untrusted-server", "wrong-name", "plaintext"):
                self.assertEqual(self.sink.records("sample", source=name, quality="GOOD"), [], name)
            self.assertEqual(self.sink.records("status", source="untrusted-server")[0]["state"], "rejected")
            self.assertIn("CERTIFICATE_VERIFY_FAILED", self.sink.records("status", source="untrusted-server")[0]["reason"])
            self.assertEqual(self.sink.records("status", source="wrong-name")[0]["state"], "rejected")
            wait_until(lambda: self.sink.records("status", source="untrusted-client", state="rejected"), 10,
                       "a refused client certificate is a rejection")

    def test_rtu_over_pseudo_terminal(self):
        rtu = modbus.RtuSimulator(modbus_model())
        self.addCleanup(rtu.close)
        self.start(base_config([{"name": "rtu", "protocol": "modbus", "transport": "rtu", "unit_id": 17,
                                 "serial": {"device": rtu.device, "baud_rate": 115200}, "poll_interval_ms": 100,
                                 "points": modbus_points(), **FAST}]))
        wait_until(lambda: all(self.sink.records("sample", point=p) for p in EXPECTED_MODBUS), 10)
        for point, (kind, value) in EXPECTED_MODBUS.items():
            self.assertEqual(self.sink.records("sample", point=point)[-1]["value"], value)
        connects = len(self.sink.records("status", state="connected"))
        rtu.fault = "bad-crc"
        wait_until(lambda: len(self.sink.records("status", state="connected")) > connects, 5)
        self.assertIn("CRC mismatch", self.sink.records("status", state="disconnected")[-1]["reason"])


# --- OPC UA -----------------------------------------------------------------------------

def opc_points():
    return [
        {"name": "temperature", "node_id": "ns=2;s=Temperature", "type": "double", "sampling_interval_ms": 20},
        {"name": "counter", "node_id": "ns=2;s=Counter", "type": "int32", "sampling_interval_ms": 20},
        {"name": "state", "node_id": "ns=2;s=State", "type": "string"},
        {"name": "flag", "node_id": "ns=2;i=1001", "mode": "poll", "poll_interval_ms": 100, "type": "boolean"},
        {"name": "missing", "node_id": "ns=2;s=Missing"},
    ]


def populate(sim):
    sim.add_variable("ns=2;s=Temperature", ua.DOUBLE, 21.5)
    sim.add_variable("ns=2;s=Counter", ua.INT32, 0)
    sim.add_variable("ns=2;s=State", ua.STRING, "idle")
    sim.add_variable("ns=2;i=1001", ua.BOOLEAN, True)


class OpcUaTests(Harness):
    def simulator(self, **kwargs):
        sim = opcua_server.Simulator(**kwargs)
        populate(sim)
        self.addCleanup(sim.close)
        return sim

    def source(self, name="opc"):
        return super().source(name)

    def opc(self, sim, name="opc", **extra):
        source = {"name": name, "protocol": "opcua", "endpoint_url": sim.url, "publishing_interval_ms": 50,
                  "keepalive_count": 3, "lifetime_count": 9, "points": opc_points(), **FAST}
        source.update(extra)
        return source

    def latest(self, point, source="opc"):
        samples = self.sink.records("sample", point=point, source=source)
        return samples[-1] if samples else None

    def test_interoperability_subscription_poll_quality_timestamps(self):
        sim = self.simulator()
        self.start(base_config([self.opc(sim)]))
        wait_until(lambda: all(self.latest(p) for p in ("temperature", "counter", "state", "flag", "missing")), 10)
        self.assertEqual((self.latest("temperature")["value"], self.latest("temperature")["type"]), (21.5, "double"))
        self.assertEqual(self.latest("flag")["value"], True)
        missing = self.latest("missing")
        self.assertEqual((missing["quality"], missing["status"]), ("BAD", "BadNodeIdUnknown"))
        for value in range(1, 6):
            sim.tick()
            wait_until(lambda: self.latest("counter")["value"] == value)
        counters = [r["value"] for r in self.sink.records("sample", point="counter")]
        self.assertEqual(counters, sorted(counters), "subscription values arrive in order")
        stamp = 1_760_000_000_000_000_000
        sim.set_value("ns=2;s=Temperature", 19.0, status=ua.STATUS["UncertainLastUsableValue"],
                      source_timestamp=ua.ns_to_datetime(stamp))
        wait_until(lambda: self.latest("temperature")["value"] == 19.0)
        sample = self.latest("temperature")
        self.assertEqual((sample["quality"], sample["status"]), ("UNCERTAIN", "UncertainLastUsableValue"))
        self.assertEqual((sample["source_timestamp_ns"], sample["timestamp_ns"]), (stamp, stamp))
        self.assertGreater(sample["server_timestamp_ns"], stamp)
        sim.set_value("ns=2;s=Temperature", None, status=ua.STATUS["BadSensorFailure"])
        wait_until(lambda: self.latest("temperature")["quality"] == "BAD")
        self.assertEqual(self.latest("temperature")["status"], "BadSensorFailure")

    def test_malformed_values(self):
        sim = self.simulator()
        self.start(base_config([self.opc(sim)]))
        wait_until(lambda: self.latest("temperature"))
        sim.set_value("ns=2;s=Temperature", "hot", vtype=ua.STRING)
        wait_until(lambda: self.latest("temperature")["quality"] == "BAD")
        self.assertEqual(self.latest("temperature")["status"], "BadTypeMismatch(received string)")
        sim.set_value("ns=2;s=Temperature", math.inf, vtype=ua.DOUBLE)
        wait_until(lambda: self.latest("temperature")["status"] == "non-finite")
        sim.set_value("ns=2;s=State", "x" * 2000)
        wait_until(lambda: self.latest("state")["status"] == "too-large")
        sim.set_value("ns=2;s=State", [1, 2], vtype=ua.INT32)
        wait_until(lambda: self.latest("state")["status"] == "unsupported-type")
        sim.set_value("ns=2;s=Temperature", 20.0, vtype=ua.DOUBLE)
        wait_until(lambda: self.latest("temperature")["value"] == 20.0)

    def test_malformed_frames_reconnect(self):
        sim = self.simulator()
        self.start(base_config([self.opc(sim)]))
        wait_until(lambda: self.latest("temperature"))
        for corrupt in ("chunk", "body"):
            with self.subTest(corrupt):
                connects = len(self.sink.records("status", state="connected"))
                sim.corrupt_next = corrupt
                sim.tick()
                wait_until(lambda: len(self.sink.records("status", state="connected")) > connects, 8)
                value = sim.nodes[ua.NodeId(2, "Counter")].value + 1
                sim.set_value("ns=2;s=Counter", value)
                wait_until(lambda: self.latest("counter")["value"] == value, 8)

    def test_disconnect_reconnects_and_resubscribes(self):
        sim = self.simulator()
        self.start(base_config([self.opc(sim)]))
        wait_until(lambda: self.latest("counter"))
        sim.drop_connections()
        wait_until(lambda: self.sink.records("status", state="disconnected"))
        wait_until(lambda: len(self.sink.records("status", state="subscribed")) >= 2, 8)
        sim.set_value("ns=2;s=Counter", 41)
        wait_until(lambda: self.latest("counter")["value"] == 41)
        self.assertEqual(self.latest("counter")["quality"], "GOOD")

    def test_server_side_subscription_loss_resubscribes(self):
        sim = self.simulator()
        self.start(base_config([self.opc(sim)]))
        wait_until(lambda: self.latest("counter"))
        sessions = sim.counters["sessions"]
        sim.expire_subscriptions()
        wait_until(lambda: self.source().stats["resubscriptions"] >= 1, 8, "no resubscription")
        sim.set_value("ns=2;s=Counter", 7)
        wait_until(lambda: self.latest("counter")["value"] == 7)
        self.assertEqual(sim.counters["sessions"], sessions, "resubscribed on the same session")
        sim.restart()                                 # sessions forgotten: a new session is needed
        wait_until(lambda: sim.counters["sessions"] > sessions, 8)
        sim.set_value("ns=2;s=Counter", 8)
        wait_until(lambda: self.latest("counter")["value"] == 8, 8)

    def test_backpressure_withholds_publish_and_reports_overflow(self):
        sim = self.simulator()
        sink = MemorySink()
        points = [{"name": "counter", "node_id": "ns=2;s=Counter", "type": "int32", "sampling_interval_ms": 10,
                   "queue_size": 5}]
        self.start(base_config([self.opc(sim, points=points, max_notifications_per_publish=4, lifetime_count=200)],
                               max_buffered_records=8), sink)
        wait_until(lambda: sink.records("sample", point="counter"))
        sink.blocked = True

        def tick_until_paused():
            sim.tick()
            return self.source().stats["publish_pauses"] >= 1
        wait_until(tick_until_paused, 5, "Publish was not withheld")
        for _ in range(40):
            sim.tick()
            time.sleep(0.012)
        self.assertLessEqual(len(self.collector.buffer), 8)
        sink.blocked = False
        wait_until(lambda: sink.records("gap", source="opc"), 5, "no gap record")
        final = sim.nodes[ua.NodeId(2, "Counter")].value
        wait_until(lambda: sink.records("sample", point="counter")[-1]["value"] == final, 5)
        statuses = [r["status"] for r in sink.records("sample", point="counter")]
        self.assertIn("Good+Overflow", statuses, "the server queue overflow is visible to consumers")
        self.assertGreater(self.source().stats["overflows"], 0)
        self.assertEqual(self.collector.buffer.dropped, 0)

    def test_publish_notification_bound(self):
        sim = self.simulator()
        many = [{"name": f"p{i}", "node_id": f"ns=2;s=V{i}", "type": "int32", "sampling_interval_ms": 10} for i in range(30)]
        for i in range(30):
            sim.add_variable(f"ns=2;s=V{i}", ua.INT32, i)
        self.start(base_config([self.opc(sim, points=many, max_notifications_per_publish=7)]))
        wait_until(lambda: all(self.latest(f"p{i}") for i in range(30)), 10)
        self.assertGreaterEqual(self.source().stats["publish_responses"], 5, "30 initial values need >= 5 publishes of 7")

    def test_plaintext_password_refused_unless_allowed(self):
        sim = self.simulator(users={"op": "pw"}, allow_anonymous=False)
        with tempfile.TemporaryDirectory() as tmp:
            password = Path(tmp, "password")
            password.write_text("pw\n")
            password.chmod(0o600)
            identity = {"type": "username", "username": "op", "password_file": str(password)}
            self.start(base_config([self.opc(sim, name="refused", identity=identity),
                                    self.opc(sim, name="allowed", identity=identity, allow_plaintext_password=True)]))
            wait_until(lambda: self.latest("temperature", "allowed"), 10)
            refused = wait_until(lambda: self.sink.records("status", source="refused"))[0]
            self.assertEqual(refused["state"], "rejected")
            self.assertIn("refusing to send a password without encryption", refused["reason"])
            self.assertEqual(self.sink.records("sample", source="refused", quality="GOOD"), [])

    def test_no_downgrade_when_server_lacks_configured_security(self):
        self.assertTrue(sec.crypto_available(), "Basic256Sha256 tests need cryptography (make test-industrial)")
        with tempfile.TemporaryDirectory() as tmp:
            certs = Certificates(tmp)
            sim = self.simulator(endpoints=[("None", "None")])
            self.start(base_config([self.opc(sim, security_policy="Basic256Sha256", security_mode="SignAndEncrypt",
                                             certificate_file=certs.path("client.der"),
                                             private_key_file=certs.path("client.key"),
                                             server_certificate_file=certs.path("server.der"))]))
            status = wait_until(lambda: self.sink.records("status", source="opc"))[0]
            self.assertEqual(status["state"], "rejected")
            self.assertIn("does not offer the configured security policy and mode", status["reason"])

    def test_basic256sha256_credentials_and_certificates(self):
        self.assertTrue(sec.crypto_available(), "Basic256Sha256 tests need cryptography (make test-industrial)")
        with tempfile.TemporaryDirectory() as tmp:
            certs = Certificates(tmp)
            sim = self.simulator(endpoints=[("Basic256Sha256", "SignAndEncrypt"), ("Basic256Sha256", "Sign")],
                                 certificate=certs.der("server"), private_key=sec.load_private_key(certs.path("server.key")),
                                 trusted_certificates=[certs.der("client")], users={"op": "correct horse"},
                                 allow_anonymous=False)
            for name, text in (("good", "correct horse\n"), ("bad", "wrong\n")):
                Path(tmp, f"{name}.password").write_text(text)
                Path(tmp, f"{name}.password").chmod(0o600)

            def secure(name, client="client", pin="server", mode="SignAndEncrypt", password="good"):
                return self.opc(sim, name=name, security_policy="Basic256Sha256", security_mode=mode,
                                certificate_file=certs.path(f"{client}.der"), private_key_file=certs.path(f"{client}.key"),
                                server_certificate_file=certs.path(f"{pin}.der"),
                                identity={"type": "username", "username": "op",
                                          "password_file": str(Path(tmp, f"{password}.password"))})
            self.start(base_config([secure("encrypted"), secure("signed", mode="Sign"),
                                    secure("wrong-password", password="bad"), secure("untrusted-client", client="rogue"),
                                    secure("wrong-pin", pin="rogue")]))
            for name in ("encrypted", "signed"):
                wait_until(lambda: self.latest("temperature", name), 15, f"{name}: no data")
            sim.tick()
            wait_until(lambda: self.latest("counter", "encrypted")["value"] == 1)
            expected = {"wrong-password": "BadUserAccessDenied", "untrusted-client": "BadCertificateUntrusted",
                        "wrong-pin": "does not match the pinned certificate"}
            for name, text in expected.items():
                status = wait_until(lambda: self.sink.records("status", source=name), 10)[0]
                self.assertEqual(status["state"], "rejected", status)
                self.assertIn(text, status["reason"])
                self.assertEqual(self.sink.records("sample", source=name, quality="GOOD"), [])
            self.assertGreaterEqual(sim.counters["activations_rejected"], 1)
            self.assertGreaterEqual(sim.counters["channels_rejected"], 1)
            self.assertNotIn("correct horse", json.dumps(self.sink.records()) + "\n".join(self.logs))


class CliTests(unittest.TestCase):
    def test_check_config_and_unsupported_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "c.json")
            raw = json.loads(json.dumps(CASES["base"]))
            path.write_text(json.dumps(raw))
            run = lambda: subprocess.run([sys.executable, "-m", "industrial.collector", "--config", str(path), "--check-config"],
                                         cwd=ROOT / "scripts", capture_output=True, text=True, timeout=30)
            result = run()
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "configuration valid"), result.stderr)
            for version in (2, True, "1"):
                raw["version"] = version
                path.write_text(json.dumps(raw))
                result = run()
                self.assertEqual(result.returncode, 2)
                self.assertIn("unsupported-version", result.stderr)


if __name__ == "__main__":
    unittest.main()
