"""SuiFleet industrial collector component, configuration version 1 (SF-070).

Polls Modbus devices and polls or subscribes to OPC UA servers, then appends
typed, timestamped, quality-tagged records to a SuiFleet durable stream (SF-034)
through the authenticated component IPC socket (SF-027). The daemon signs
stream exports with the device key, so exported collector output is
authenticated end to end; an optional pub/sub topic (SF-032) receives a copy.

Every limit is explicit: points per source, poll and publishing rates, request
and connect timeouts, exponential reconnect backoff and the in-memory buffer.
When the buffer is full, Modbus polls are skipped (and reported as a gap) and
OPC UA Publish requests are withheld, so the server's bounded monitored-item
queues absorb the excess and flag overflow. Nothing is dropped silently.

Run:  python3 -m industrial.collector --config collector.json
"""
import argparse
import collections
import itertools
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import sys
import threading
import time

from . import modbus
from . import opcua_codec as ua
from .opcua_client import AuthenticationError, Client as OpcUaClient, ServiceError
from .opcua_secure import MODES, POLICIES, ProtocolError, SecurityError, crypto_available

CONFIG_VERSION = 1
RECORD_SCHEMA = 1
NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
STREAM_NAME = re.compile(r"^[A-Za-z0-9 ,._-]{1,255}$")
TOPIC = re.compile(r"^[A-Za-z0-9/._-]{1,255}$")
OPCUA_TYPES = {"boolean", "sbyte", "byte", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float",
               "double", "string", "datetime"}
MAX_STRING_BYTES = 1024
MAX_MESSAGE_BYTES = 60000           # below the 64 KiB IPC frame, with room for the request envelope
MAX_POINTS = 10000


class ConfigError(ValueError):
    pass


# --- Configuration ---------------------------------------------------------------

def _obj(value, where, required=(), optional=None):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected an object")
    allowed = set(required) | set(optional or {})
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {', '.join(unknown)}")
    missing = [k for k in required if k not in value]
    if missing:
        raise ConfigError(f"{where}: missing field(s) {', '.join(missing)}")
    out = dict(optional or {})
    out.update(value)
    return out


def _int(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where}: expected an integer {low}..{high}")
    return value


def _str(value, where, pattern=None, choices=None):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise ConfigError(f"{where}: invalid value {value!r}")
    if choices is not None and value not in choices:
        raise ConfigError(f"{where}: expected one of {', '.join(sorted(choices))}")
    return value


def _bool(value, where):
    if type(value) is not bool:
        raise ConfigError(f"{where}: expected a boolean")
    return value


def _path(value, where):
    value = _str(value, where)
    if not os.path.isabs(value):
        raise ConfigError(f"{where}: expected an absolute path")
    return value


def read_private_file(path, where):
    """Secrets and keys must be regular files without group/other access."""
    try:
        info = os.stat(path)
    except OSError as error:
        raise ConfigError(f"{where}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{where}: not a regular file")
    if info.st_mode & 0o077:
        raise ConfigError(f"{where}: must not be readable by group or others (chmod 600)")
    return Path(path).read_bytes()


def _common(source, where):
    s = source
    s["connect_timeout_ms"] = _int(s["connect_timeout_ms"], f"{where}.connect_timeout_ms", 100, 60000)
    s["request_timeout_ms"] = _int(s["request_timeout_ms"], f"{where}.request_timeout_ms", 100, 60000)
    s["reconnect_min_ms"] = _int(s["reconnect_min_ms"], f"{where}.reconnect_min_ms", 100, 600000)
    s["reconnect_max_ms"] = _int(s["reconnect_max_ms"], f"{where}.reconnect_max_ms", s["reconnect_min_ms"], 3600000)
    if not isinstance(s["points"], list) or not 1 <= len(s["points"]) <= 1000:
        raise ConfigError(f"{where}.points: expected 1..1000 points")


COMMON = {"connect_timeout_ms": 5000, "request_timeout_ms": 3000, "reconnect_min_ms": 1000, "reconnect_max_ms": 60000}


def _modbus(raw, where):
    s = _obj(raw, where, ("name", "protocol", "transport", "points"), dict(COMMON, host=None, port=None, unit_id=1, tls=None,
             serial=None, poll_interval_ms=1000, max_registers_per_request=125, max_bits_per_request=2000))
    _common(s, where)
    transport = _str(s["transport"], f"{where}.transport", choices={"tcp", "tls", "rtu"})
    if transport == "rtu":
        if s["host"] is not None or s["port"] is not None or s["tls"] is not None:
            raise ConfigError(f"{where}: rtu takes serial, not host/port/tls")
        serial = _obj(s["serial"], f"{where}.serial", ("device",),
                      {"baud_rate": 9600, "data_bits": 8, "parity": "N", "stop_bits": 1})
        _path(serial["device"], f"{where}.serial.device")
        if serial["baud_rate"] not in modbus.SERIAL_BAUD or type(serial["baud_rate"]) is not int:
            raise ConfigError(f"{where}.serial.baud_rate: expected one of {sorted(modbus.SERIAL_BAUD)}")
        if serial["data_bits"] not in (7, 8) or type(serial["data_bits"]) is not int:
            raise ConfigError(f"{where}.serial.data_bits: expected 7 or 8")
        _str(serial["parity"], f"{where}.serial.parity", choices={"N", "E", "O"})
        if serial["stop_bits"] not in (1, 2) or type(serial["stop_bits"]) is not int:
            raise ConfigError(f"{where}.serial.stop_bits: expected 1 or 2")
        s["serial"] = serial
        s["unit_id"] = _int(s["unit_id"], f"{where}.unit_id", 1, 247)
    else:
        if s["serial"] is not None:
            raise ConfigError(f"{where}: serial is only for rtu")
        _str(s["host"], f"{where}.host")
        if s["port"] is None:
            s["port"] = 502 if transport == "tcp" else 802
        s["port"] = _int(s["port"], f"{where}.port", 1, 65535)
        s["unit_id"] = _int(s["unit_id"], f"{where}.unit_id", 0, 255)
        if transport == "tls":
            tls = _obj(s["tls"], f"{where}.tls", ("ca_file", "certificate_file", "private_key_file"), {"server_name": None})
            for key in ("ca_file", "certificate_file", "private_key_file"):
                _path(tls[key], f"{where}.tls.{key}")
            if tls["server_name"] is not None:
                _str(tls["server_name"], f"{where}.tls.server_name")
            s["tls"] = tls
        elif s["tls"] is not None:
            raise ConfigError(f"{where}: tls settings need transport tls")
    s["poll_interval_ms"] = _int(s["poll_interval_ms"], f"{where}.poll_interval_ms", 50, 86400000)
    s["max_registers_per_request"] = _int(s["max_registers_per_request"], f"{where}.max_registers_per_request", 1, 125)
    s["max_bits_per_request"] = _int(s["max_bits_per_request"], f"{where}.max_bits_per_request", 1, 2000)
    points, names = [], set()
    for i, raw_point in enumerate(s["points"]):
        pw = f"{where}.points[{i}]"
        p = _obj(raw_point, pw, ("name", "table", "address", "type"), {"word_order": "big"})
        _str(p["name"], f"{pw}.name", NAME)
        if p["name"] in names:
            raise ConfigError(f"{pw}.name: duplicate point {p['name']!r}")
        names.add(p["name"])
        _str(p["table"], f"{pw}.table", choices=set(modbus.TABLES))
        bit_table = p["table"] in ("coil", "discrete_input")
        _str(p["type"], f"{pw}.type", choices={"bool"} if bit_table else set(modbus.REGISTER_WIDTH))
        _str(p["word_order"], f"{pw}.word_order", choices={"big", "little"})
        width = 1 if bit_table else modbus.REGISTER_WIDTH[p["type"]]
        p["address"] = _int(p["address"], f"{pw}.address", 0, 0x10000 - width)
        if width > s["max_registers_per_request"]:
            raise ConfigError(f"{pw}: type needs {width} registers, above max_registers_per_request")
        p["width"] = width
        points.append(p)
    s["points"] = points
    return s


def _opcua(raw, where):
    s = _obj(raw, where, ("name", "protocol", "endpoint_url", "points"), dict(
        COMMON, security_policy="None", security_mode="None", certificate_file=None, private_key_file=None,
        server_certificate_file=None, identity={"type": "anonymous"}, allow_plaintext_password=False,
        publishing_interval_ms=1000, keepalive_count=10, lifetime_count=60, session_timeout_ms=60000,
        max_notifications_per_publish=100))
    _common(s, where)
    url = _str(s["endpoint_url"], f"{where}.endpoint_url")
    if not url.startswith("opc.tcp://"):
        raise ConfigError(f"{where}.endpoint_url: expected opc.tcp://host[:port]")
    policy = _str(s["security_policy"], f"{where}.security_policy", choices=set(POLICIES))
    mode = _str(s["security_mode"], f"{where}.security_mode", choices=set(MODES))
    if (policy == "None") != (mode == "None"):
        raise ConfigError(f"{where}: security_policy None requires security_mode None, and Basic256Sha256 requires "
                          "Sign or SignAndEncrypt")
    if policy != "None":
        for key in ("certificate_file", "private_key_file", "server_certificate_file"):
            if s[key] is None:
                raise ConfigError(f"{where}.{key}: required for security_policy {policy}")
    elif s["certificate_file"] is not None or s["private_key_file"] is not None:
        raise ConfigError(f"{where}: client certificates need a security policy other than None")
    for key in ("certificate_file", "private_key_file", "server_certificate_file"):
        if s[key] is not None:
            _path(s[key], f"{where}.{key}")
    identity = s["identity"]
    if not isinstance(identity, dict) or identity.get("type") not in ("anonymous", "username"):
        raise ConfigError(f"{where}.identity.type: expected anonymous or username")
    if identity["type"] == "username":
        identity = _obj(identity, f"{where}.identity", ("type", "username", "password_file"))
        _str(identity["username"], f"{where}.identity.username")
        _path(identity["password_file"], f"{where}.identity.password_file")
    else:
        identity = _obj(identity, f"{where}.identity", ("type",))
    s["identity"] = identity
    _bool(s["allow_plaintext_password"], f"{where}.allow_plaintext_password")
    s["publishing_interval_ms"] = _int(s["publishing_interval_ms"], f"{where}.publishing_interval_ms", 50, 3600000)
    s["keepalive_count"] = _int(s["keepalive_count"], f"{where}.keepalive_count", 1, 1000)
    s["lifetime_count"] = _int(s["lifetime_count"], f"{where}.lifetime_count", 3 * s["keepalive_count"], 30000)
    s["session_timeout_ms"] = _int(s["session_timeout_ms"], f"{where}.session_timeout_ms", 10000, 3600000)
    s["max_notifications_per_publish"] = _int(s["max_notifications_per_publish"],
                                              f"{where}.max_notifications_per_publish", 1, 10000)
    points, names = [], set()
    for i, raw_point in enumerate(s["points"]):
        pw = f"{where}.points[{i}]"
        p = _obj(raw_point, pw, ("name", "node_id"), {"mode": "subscribe", "sampling_interval_ms": None,
                                                     "queue_size": 10, "poll_interval_ms": None, "type": None})
        _str(p["name"], f"{pw}.name", NAME)
        if p["name"] in names:
            raise ConfigError(f"{pw}.name: duplicate point {p['name']!r}")
        names.add(p["name"])
        try:
            p["node"] = ua.NodeId.parse(_str(p["node_id"], f"{pw}.node_id"))
        except ValueError as error:
            raise ConfigError(f"{pw}.node_id: {error}") from None
        _str(p["mode"], f"{pw}.mode", choices={"subscribe", "poll"})
        if p["type"] is not None:
            _str(p["type"], f"{pw}.type", choices=OPCUA_TYPES)
        if p["mode"] == "subscribe":
            if p["poll_interval_ms"] is not None:
                raise ConfigError(f"{pw}.poll_interval_ms: only for mode poll")
            p["sampling_interval_ms"] = _int(s["publishing_interval_ms"] if p["sampling_interval_ms"] is None
                                             else p["sampling_interval_ms"], f"{pw}.sampling_interval_ms", 0, 3600000)
            p["queue_size"] = _int(p["queue_size"], f"{pw}.queue_size", 1, 100)
        else:
            if p["sampling_interval_ms"] is not None:
                raise ConfigError(f"{pw}.sampling_interval_ms: only for mode subscribe")
            p["poll_interval_ms"] = _int(1000 if p["poll_interval_ms"] is None else p["poll_interval_ms"],
                                         f"{pw}.poll_interval_ms", 50, 86400000)
        points.append(p)
    s["points"] = points
    return s


def validate(raw):
    """Validate a parsed configuration. The version is checked before anything else."""
    if not isinstance(raw, dict):
        raise ConfigError("configuration: expected an object")
    if type(raw.get("version")) is not int or raw["version"] != CONFIG_VERSION:
        raise ConfigError(f"unsupported-version: configuration version must be the integer {CONFIG_VERSION}")
    c = _obj(raw, "configuration", ("version", "collector_id", "output", "sources"))
    _str(c["collector_id"], "collector_id", NAME)
    out = _obj(c["output"], "output", ("socket", "component", "stream"), {
        "topic": None, "max_buffered_records": 10000, "max_records_per_message": 100, "retry_min_ms": 200,
        "retry_max_ms": 30000, "ipc_timeout_ms": 10000})
    _path(out["socket"], "output.socket")
    _str(out["component"], "output.component", NAME)
    _str(out["stream"], "output.stream", STREAM_NAME)
    if out["topic"] is not None:
        _str(out["topic"], "output.topic", TOPIC)
    out["max_buffered_records"] = _int(out["max_buffered_records"], "output.max_buffered_records", 1, 100000)
    out["max_records_per_message"] = _int(out["max_records_per_message"], "output.max_records_per_message", 1, 1000)
    out["retry_min_ms"] = _int(out["retry_min_ms"], "output.retry_min_ms", 10, 60000)
    out["retry_max_ms"] = _int(out["retry_max_ms"], "output.retry_max_ms", out["retry_min_ms"], 300000)
    out["ipc_timeout_ms"] = _int(out["ipc_timeout_ms"], "output.ipc_timeout_ms", 100, 120000)
    c["output"] = out
    if not isinstance(c["sources"], list) or not 1 <= len(c["sources"]) <= 64:
        raise ConfigError("sources: expected 1..64 sources")
    sources, names, total = [], set(), 0
    for i, raw_source in enumerate(c["sources"]):
        where = f"sources[{i}]"
        if not isinstance(raw_source, dict):
            raise ConfigError(f"{where}: expected an object")
        _str(raw_source.get("name"), f"{where}.name", NAME)
        protocol = _str(raw_source.get("protocol"), f"{where}.protocol", choices={"modbus", "opcua"})
        source = _modbus(raw_source, where) if protocol == "modbus" else _opcua(raw_source, where)
        if source["name"] in names:
            raise ConfigError(f"{where}.name: duplicate source {source['name']!r}")
        names.add(source["name"])
        total += len(source["points"])
        if protocol == "opcua" and source["max_notifications_per_publish"] > out["max_buffered_records"]:
            raise ConfigError(f"{where}.max_notifications_per_publish: above output.max_buffered_records")
        polled = source["points"] if protocol == "modbus" else [p for p in source["points"] if p["mode"] == "poll"]
        if len(polled) > out["max_buffered_records"]:
            raise ConfigError(f"{where}.points: one poll would not fit output.max_buffered_records")
        sources.append(source)
    if total > MAX_POINTS:
        raise ConfigError(f"sources: {total} points exceed the limit of {MAX_POINTS}")
    c["sources"] = sources
    return c


def load_secrets(config):
    """Read keys, certificates and passwords once at startup, enforcing file modes."""
    for i, s in enumerate(config["sources"]):
        where = f"sources[{i}]"
        if s["protocol"] == "modbus" and s["transport"] == "tls":
            read_private_file(s["tls"]["private_key_file"], f"{where}.tls.private_key_file")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode, context.check_hostname = ssl.CERT_REQUIRED, True
            try:
                context.load_verify_locations(cafile=s["tls"]["ca_file"])
                context.load_cert_chain(s["tls"]["certificate_file"], s["tls"]["private_key_file"])
            except (OSError, ssl.SSLError) as error:
                raise ConfigError(f"{where}.tls: {error}") from None
            s["ssl_context"] = context
        if s["protocol"] != "opcua":
            continue
        if s["security_policy"] != "None" and not crypto_available():
            raise ConfigError(f"{where}.security_policy: {s['security_policy']} needs the Python 'cryptography' "
                              "package; refusing to run with weaker security")
        from .opcua_secure import load_certificate_der, load_private_key
        try:
            if s["private_key_file"]:
                read_private_file(s["private_key_file"], f"{where}.private_key_file")
                s["private_key"] = load_private_key(s["private_key_file"])
                s["certificate"] = load_certificate_der(s["certificate_file"])
            else:
                s["private_key"] = s["certificate"] = None
            s["server_certificate"] = load_certificate_der(s["server_certificate_file"]) \
                if s["server_certificate_file"] else None
        except (OSError, SecurityError, ValueError) as error:
            raise ConfigError(f"{where}: {error}") from None
        if s["identity"]["type"] == "username":
            password = read_private_file(s["identity"]["password_file"], f"{where}.identity.password_file")
            s["password"] = password.rstrip(b"\r\n")
    return config


# --- Output ----------------------------------------------------------------------

class Buffer:
    """Bounded record buffer shared by all sources and drained by one writer."""

    def __init__(self, capacity):
        self.capacity, self.items = capacity, collections.deque()
        self.lock = threading.Condition()
        self.dropped = 0

    def space(self):
        with self.lock:
            return self.capacity - len(self.items)

    def wait_for_space(self, n, timeout):
        deadline = time.monotonic() + timeout
        with self.lock:
            while self.capacity - len(self.items) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.lock.wait(remaining)
            return True

    def put(self, record):
        with self.lock:
            if len(self.items) >= self.capacity:
                self.dropped += 1                  # reported by the next gap record of that source
                return False
            self.items.append(record)
            self.lock.notify_all()
            return True

    def peek(self, limit, timeout):
        with self.lock:
            if not self.items:
                self.lock.wait(timeout)
            return list(itertools.islice(self.items, limit))

    def pop(self, n):
        with self.lock:
            for _ in range(n):
                self.items.popleft()
            self.lock.notify_all()

    def __len__(self):
        with self.lock:
            return len(self.items)


class IpcSink:
    """Appends batches to the durable stream through the daemon's IPC socket."""

    def __init__(self, output):
        from suifleet_ipc import Client, IpcError      # bundled in the zip application
        self.client = Client(output["socket"], component=output["component"], timeout=output["ipc_timeout_ms"] / 1000)
        self.error, self.stream, self.topic = IpcError, output["stream"], output["topic"]

    def append(self, message):
        try:
            return self.client.stream_append(self.stream, message)
        except self.error as error:
            raise SinkError(str(error)) from None
        except (OSError, ValueError) as error:
            raise SinkError(f"ipc: {error}") from None

    def publish(self, record):
        try:
            self.client.publish(self.topic, record)
            return True
        except (self.error, OSError, ValueError):
            return False


class SinkError(Exception):
    pass


class Writer(threading.Thread):
    """Moves records from the buffer to the sink in bounded messages, retrying with backoff.

    A record leaves the buffer only after the daemon acknowledged its append.
    An IPC timeout has an unknown outcome, so a failed message is retried
    unchanged and may be stored twice; consumers deduplicate by its `batch`.
    """

    def __init__(self, collector):
        super().__init__(daemon=True, name="writer")
        self.c = collector
        out = collector.config["output"]
        self.limit, self.retry_min, self.retry_max = out["max_records_per_message"], out["retry_min_ms"], out["retry_max_ms"]
        self.batches, self.stopping = 0, StopEvent()

    def run(self):
        backoff, message = self.retry_min, None
        while not self.stopping.deadline_passed():
            if message is None:                    # a failed append is retried with the identical message
                records = self.c.buffer.peek(self.limit, 0.2)
                if not records:
                    if self.stopping.is_set():
                        return
                    continue
                message = self._message(records)
            try:
                sequence = self.c.sink.append(message)
            except SinkError as error:
                self.c.stats["append_failures"] += 1
                self.c.stats["last_append_error"] = str(error)
                self.c.log(f"stream append failed ({error}); retrying in {backoff} ms")
                if self.stopping.is_set():         # shutting down: keep trying until the flush deadline
                    time.sleep(max(0.0, min(backoff / 1000, self.stopping.deadline - time.monotonic())))
                else:
                    self.stopping.wait(backoff / 1000)
                backoff = min(backoff * 2, self.retry_max)
                continue
            backoff = self.retry_min
            self.c.buffer.pop(len(message["records"]))
            self.c.stats["appended_records"] += len(message["records"])
            self.c.stats["appended_messages"] += 1
            self.c.stats["last_sequence"] = sequence
            if self.c.config["output"]["topic"]:
                for record in message["records"]:
                    if not self.c.sink.publish(record):
                        self.c.stats["publish_failures"] += 1
            message = None

    def _message(self, records):
        """Largest prefix that stays below MAX_MESSAGE_BYTES (a record is at most a few KiB)."""
        batch = f"{self.c.boot}:{self.batches}"
        message = {"schema": RECORD_SCHEMA, "collector": self.c.config["collector_id"], "batch": batch, "records": []}
        size = len(json.dumps(message, separators=(",", ":")))
        for record in records:
            extra = len(json.dumps(record, separators=(",", ":"))) + 1
            if message["records"] and size + extra > MAX_MESSAGE_BYTES:
                break
            message["records"].append(record)
            size += extra
        self.batches += 1
        return message


class StopEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.deadline = None

    def deadline_passed(self):
        return self.deadline is not None and time.monotonic() >= self.deadline


# --- Value conversion ----------------------------------------------------------------

def opcua_value(point, datavalue):
    """DataValue -> (type, value, quality, status) for one record."""
    status = datavalue.status or 0
    quality, status_text = ua.quality(status), ua.status_name(status)
    variant = datavalue.value
    if variant is None:
        return point.get("type"), None, quality if quality != "GOOD" else "BAD", status_text if status else "no-value"
    vtype = ua.VARIANT_NAMES.get(variant.type)
    if isinstance(variant.value, list) or vtype is None or vtype in ("bytestring", "statuscode"):
        return point.get("type"), None, "BAD", "unsupported-type"
    if point.get("type") and point["type"] != vtype:
        return point["type"], None, "BAD", f"BadTypeMismatch(received {vtype})"
    value = variant.value
    if vtype in ("float", "double") and not math.isfinite(value):
        return vtype, None, "BAD", "non-finite"
    if vtype == "datetime":
        value = ua.datetime_to_ns(value)
    if vtype == "string" and value is not None and len(value.encode()) > MAX_STRING_BYTES:
        return vtype, None, "BAD", "too-large"
    return vtype, value, quality, status_text


# --- Sources ---------------------------------------------------------------------------

class Source(threading.Thread):
    def __init__(self, collector, config):
        super().__init__(daemon=True, name=f"source-{config['name']}")
        self.c, self.s = collector, config
        self.last = {}                       # point -> (quality, status) last emitted
        self.state, self.reason = "starting", ""
        self.backoff = config["reconnect_min_ms"]
        self.stats = collections.Counter()
        self.skipped, self.gap_from = 0, None

    def record(self, point, vtype, value, quality, status, received_ns, source_ns=None, server_ns=None):
        key = point["name"]
        if quality != "GOOD" and self.last.get(key) == (quality, status) and value is None:
            return                            # repeated failure state: reported once
        self.last[key] = (quality, status)
        self.emit({"kind": "sample", "source": self.s["name"], "protocol": self.s["protocol"], "point": key,
                   "type": vtype, "value": value, "quality": quality, "status": status,
                   "timestamp_ns": source_ns or received_ns, "source_timestamp_ns": source_ns,
                   "server_timestamp_ns": server_ns, "received_ns": received_ns})

    def emit(self, record):
        record["id"] = self.c.next_id()
        if not self.c.buffer.put(record):
            self.stats["dropped"] += 1
            self.gap_from = self.gap_from or record.get("received_ns") or time.time_ns()
        else:
            self.stats["records"] += 1

    def status(self, state, reason=""):
        if (state, reason) == (self.state, self.reason):
            return
        self.state, self.reason = state, reason
        self.c.log(f"{self.s['name']}: {state}{(' (' + reason + ')') if reason else ''}")
        self.emit({"kind": "status", "source": self.s["name"], "protocol": self.s["protocol"], "state": state,
                   "reason": reason, "received_ns": time.time_ns()})

    def fail_points(self, points, status):
        now = time.time_ns()
        for point in points:
            self.record(point, point.get("type"), None, "BAD", status, now)

    def report_gap(self, reason):
        if not self.skipped and not self.stats["dropped"]:
            return
        now = time.time_ns()
        dropped, self.stats["dropped"] = self.stats["dropped"], 0
        record = {"kind": "gap", "source": self.s["name"], "protocol": self.s["protocol"], "reason": reason,
                  "skipped_cycles": self.skipped, "dropped_records": dropped,
                  "from_ns": self.gap_from or now, "to_ns": now, "received_ns": now}
        self.stats["skipped_cycles_total"] += self.skipped
        self.skipped, self.gap_from = 0, None
        self.emit(record)

    def wait_backoff(self):
        delay = self.backoff
        self.backoff = min(self.backoff * 2, self.s["reconnect_max_ms"])
        self.stats["reconnect_waits"] += 1
        return self.c.stopping.wait(delay / 1000)

    def run(self):
        while not self.c.stopping.is_set():
            try:
                self.session()
            except Exception as error:                     # noqa: BLE001 - reported, then reconnected
                state, reason = self.classify(error)
                self.status(state, reason)          # the detailed reason goes in the status record
                self.fail_points(self.s["points"], state)
            finally:
                self.close()
            if self.c.stopping.is_set() or self.wait_backoff():
                return

    def classify(self, error):
        return "disconnected", f"{type(error).__name__}: {error}"

    def close(self):
        pass


def _plan_requests(source):
    """Group points into read requests: adjacent ranges within the per-request limits."""
    groups = []
    for table in modbus.TABLES:
        points = sorted((p for p in source["points"] if p["table"] == table), key=lambda p: p["address"])
        limit = source["max_bits_per_request"] if table in ("coil", "discrete_input") else source["max_registers_per_request"]
        current = None
        for p in points:
            end = p["address"] + p["width"]
            if current and p["address"] <= current["end"] and max(end, current["end"]) - current["start"] <= limit:
                current["end"] = max(end, current["end"])
                current["points"].append(p)
            else:
                current = {"function": modbus.TABLES[table], "start": p["address"], "end": end, "points": [p]}
                groups.append(current)
    return groups


class ModbusSource(Source):
    def __init__(self, collector, config):
        super().__init__(collector, config)
        self.client, self.plan = None, _plan_requests(config)

    def make_client(self):
        s = self.s
        if s["transport"] == "rtu":
            serial = s["serial"]
            return modbus.RtuClient(serial["device"], s["unit_id"], s["request_timeout_ms"] / 1000, serial["baud_rate"],
                                    serial["data_bits"], serial["parity"], serial["stop_bits"])
        return modbus.TcpClient(s["host"], s["port"], s["unit_id"], s["connect_timeout_ms"] / 1000,
                                s["request_timeout_ms"] / 1000, s.get("ssl_context"),
                                (s["tls"] or {}).get("server_name") if s["tls"] else None)

    def classify(self, error):
        text = str(error)
        if isinstance(error, modbus.ModbusError) and ("TLS handshake" in text or "TLS error" in text):
            return "rejected", text
        return "disconnected", f"{type(error).__name__}: {text}"

    def session(self):
        self.client = self.make_client()
        self.client.connect()
        self.status("connected")
        self.backoff = self.s["reconnect_min_ms"]
        interval = self.s["poll_interval_ms"] / 1000
        next_poll = time.monotonic()
        while not self.c.stopping.is_set():
            now = time.monotonic()
            if now < next_poll:
                if self.c.stopping.wait(next_poll - now):
                    return
            late = int((time.monotonic() - next_poll) // interval)
            if late:                               # overrun: count the cycles we cannot catch up on
                self.skipped += late
                self.gap_from = self.gap_from or time.time_ns()
                next_poll += late * interval
            next_poll += interval
            if not self.c.buffer.wait_for_space(len(self.s["points"]), max(0.0, next_poll - time.monotonic())):
                self.skipped += 1                  # backpressure: skip this poll rather than queue unbounded data
                self.gap_from = self.gap_from or time.time_ns()
                self.stats["backpressure_skips"] += 1
                continue
            self.report_gap("backpressure" if self.stats["backpressure_skips"] else "overrun")
            self.stats["backpressure_skips"] = 0
            self.poll()

    def poll(self):
        for group in self.plan:
            try:
                values = self.client.read(group["function"], group["start"], group["end"] - group["start"])
            except modbus.ExceptionResponse as error:
                self.stats["exceptions"] += 1
                self.fail_points(group["points"], f"exception:{error.code} {modbus.EXCEPTIONS.get(error.code, 'unknown')}")
                continue
            received = time.time_ns()
            self.stats["polls"] += 1
            for p in group["points"]:
                offset = p["address"] - group["start"]
                if p["type"] == "bool":
                    self.record(p, "bool", values[offset], "GOOD", "good", received)
                    continue
                value = modbus.decode_registers(p["type"], values[offset:offset + p["width"]], p["word_order"])
                if isinstance(value, float) and not math.isfinite(value):
                    self.record(p, p["type"], None, "BAD", "non-finite", received)
                else:
                    self.record(p, p["type"], value, "GOOD", "good", received)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None


class Resubscribe(Exception):
    pass


class OpcUaSource(Source):
    def __init__(self, collector, config):
        super().__init__(collector, config)
        self.client = None
        self.subscribed = [p for p in config["points"] if p["mode"] == "subscribe"]
        self.polled = [p for p in config["points"] if p["mode"] == "poll"]

    def classify(self, error):
        status = getattr(error, "status", None)
        if isinstance(error, (AuthenticationError, SecurityError)) or status in (
                ua.STATUS["BadCertificateUntrusted"], ua.STATUS["BadCertificateInvalid"],
                ua.STATUS["BadSecurityPolicyRejected"], ua.STATUS["BadSecurityChecksFailed"],
                ua.STATUS["BadUserAccessDenied"], ua.STATUS["BadIdentityTokenRejected"]):
            return "rejected", str(error)
        return "disconnected", f"{type(error).__name__}: {error}"

    def session(self):
        s = self.s
        self.client = OpcUaClient(
            s["endpoint_url"], POLICIES[s["security_policy"]], MODES[s["security_mode"]], s.get("certificate"),
            s.get("private_key"), s.get("server_certificate"),
            {"type": "username", "username": s["identity"]["username"], "password": s["password"]}
            if s["identity"]["type"] == "username" else {"type": "anonymous"},
            s["allow_plaintext_password"], s["connect_timeout_ms"] / 1000, s["request_timeout_ms"] / 1000,
            s["session_timeout_ms"])
        self.client.connect()
        self.status("connected")
        self.backoff = s["reconnect_min_ms"]
        stop_poll = threading.Event()
        poller = threading.Thread(target=self.poll_loop, args=(stop_poll,), daemon=True) if self.polled else None
        if poller:
            poller.start()
        try:
            while not self.c.stopping.is_set():
                if not self.subscribed:
                    if self.c.stopping.wait(0.5) or self.client.broken:
                        break
                    continue
                try:
                    self.subscription_loop()
                except Resubscribe as reason:
                    self.stats["resubscriptions"] += 1
                    self.status("resubscribing", str(reason))
            if self.client.broken and not self.c.stopping.is_set():
                raise self.client.broken
        finally:
            stop_poll.set()
            if poller:
                poller.join(timeout=5)

    def subscription_loop(self):
        s = self.s
        created = self.client.create_subscription(s["publishing_interval_ms"], s["lifetime_count"], s["keepalive_count"],
                                                  s["max_notifications_per_publish"])
        sub_id = created["subscription_id"]
        handles = {i + 1: p for i, p in enumerate(self.subscribed)}
        results = self.client.create_monitored_items(sub_id, [(h, p["node"], p["sampling_interval_ms"], p["queue_size"])
                                                              for h, p in handles.items()])
        now = time.time_ns()
        for (handle, point), result in zip(handles.items(), results):
            if result["status_code"] >> 31:
                self.record(point, point.get("type"), None, "BAD", ua.status_name(result["status_code"]), now)
        self.status("subscribed", f"subscription {sub_id}")
        interval = created["revised_publishing_interval"] / 1000
        publish_timeout = interval * created["revised_max_keep_alive_count"] * 2 + s["request_timeout_ms"] / 1000
        acks, paused = [], False
        while not self.c.stopping.is_set():
            if not self.c.buffer.wait_for_space(s["max_notifications_per_publish"], interval):
                self.skipped += 1                  # one publishing cycle without a Publish request
                if not paused:
                    paused, self.gap_from = True, time.time_ns()
                    self.stats["publish_pauses"] += 1
                if self.client.broken:
                    raise self.client.broken
                continue                           # withhold Publish: the server queues and flags overflow
            if paused:
                paused = False
                self.report_gap("backpressure")
            try:
                response = self.client.publish(acks, publish_timeout)
            except ServiceError as error:
                if error.status in (ua.STATUS["BadNoSubscription"], ua.STATUS["BadSubscriptionIdInvalid"]):
                    raise Resubscribe(ua.status_name(error.status)) from None
                if error.status == ua.STATUS["BadTimeout"]:
                    acks = []
                    continue
                raise
            if response["subscription_id"] != sub_id:
                raise Resubscribe("unknown subscription in Publish response")
            message = response["notification_message"]
            acks = [(sub_id, message["sequence_number"])] if message["notification_data"] else []
            received = time.time_ns()
            for notification in message["notification_data"]:
                body = notification.body
                kind = ua.STRUCT_BY_ID.get(notification.type_id.identifier)
                if kind == "StatusChangeNotification":
                    raise Resubscribe(f"subscription status {ua.status_name(body['status'])}")
                if kind != "DataChangeNotification":
                    continue
                for item in body["monitored_items"]:
                    point = handles.get(item["client_handle"])
                    if point is None:
                        continue
                    self.emit_value(point, item["value"], received)
            self.stats["publish_responses"] += 1

    def emit_value(self, point, datavalue, received):
        vtype, value, quality, status = opcua_value(point, datavalue)
        if status.endswith("+Overflow"):
            self.stats["overflows"] += 1
        self.record(point, vtype, value, quality, status, received,
                    ua.datetime_to_ns(datavalue.source_timestamp), ua.datetime_to_ns(datavalue.server_timestamp))

    def poll_loop(self, stop):
        schedule = {p["name"]: time.monotonic() for p in self.polled}
        while not stop.is_set() and not self.c.stopping.is_set():
            now = time.monotonic()
            due = [p for p in self.polled if schedule[p["name"]] <= now]
            if not due:
                stop.wait(max(0.0, min(schedule.values()) - now))
                continue
            for p in due:
                schedule[p["name"]] += p["poll_interval_ms"] / 1000
                if schedule[p["name"]] < now:
                    self.skipped += 1
                    schedule[p["name"]] = now + p["poll_interval_ms"] / 1000
            if not self.c.buffer.wait_for_space(len(due), 0.0):
                self.skipped += 1
                self.gap_from = self.gap_from or time.time_ns()
                continue
            try:
                results = self.client.read([p["node"] for p in due])
            except ServiceError as error:          # the server answered: report it and poll again next time
                self.poll_failed(due, ua.status_name(error.status))
                continue
            except ProtocolError as error:
                if error.status == ua.STATUS["BadTimeout"] and self.client.broken is None:
                    self.poll_failed(due, "BadTimeout")
                    continue
                self.client.abort(error)           # the session notices and reconnects
                return
            except Exception as error:             # noqa: BLE001 - never stop polling silently
                self.client.abort(ProtocolError(f"Read: {type(error).__name__}: {error}", ua.STATUS["BadDecodingError"]))
                return
            received = time.time_ns()
            self.report_gap("backpressure")
            for p, datavalue in zip(due, results):
                self.emit_value(p, datavalue, received)

    def poll_failed(self, points, status):
        self.stats["poll_failures"] += 1
        self.fail_points(points, status)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None


# --- Collector ---------------------------------------------------------------------------

class Collector:
    def __init__(self, config, sink=None, log=None):
        self.config = config
        self.buffer = Buffer(config["output"]["max_buffered_records"])
        self.sink = sink or IpcSink(config["output"])
        self.stopping = StopEvent()
        self.boot = os.urandom(6).hex()
        self.counter, self.counter_lock = 0, threading.Lock()
        self.stats = collections.Counter()
        self.log = log or (lambda text: print(f"suifleet-industrial: {text}", file=sys.stderr, flush=True))
        self.writer = Writer(self)
        self.sources = [(ModbusSource if s["protocol"] == "modbus" else OpcUaSource)(self, s) for s in config["sources"]]

    def next_id(self):
        with self.counter_lock:
            self.counter += 1
            return f"{self.config['collector_id']}:{self.boot}:{self.counter}"

    def start(self):
        self.writer.start()
        for source in self.sources:
            source.start()

    def stop(self, flush_seconds=5.0):
        self.stopping.deadline = time.monotonic() + flush_seconds
        self.stopping.set()
        for source in self.sources:
            source.join(timeout=flush_seconds)
        self.writer.stopping.deadline = self.stopping.deadline
        self.writer.stopping.set()
        self.writer.join(timeout=max(0.1, self.stopping.deadline - time.monotonic()))
        return len(self.buffer)

    def snapshot(self):
        return {"collector": self.config["collector_id"], "boot": self.boot, "buffered": len(self.buffer),
                "capacity": self.buffer.capacity, "writer": dict(self.stats),
                "sources": {s.s["name"]: {"state": s.state, "reason": s.reason, "pending_skipped_cycles": s.skipped,
                                         **dict(s.stats)} for s in self.sources}}


def write_status(path, snapshot):
    temporary = f"{path}.tmp"
    with open(temporary, "w") as f:
        json.dump(snapshot, f, sort_keys=True)
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="SuiFleet industrial collector (Modbus, OPC UA)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true", help="validate configuration and secrets, then exit")
    parser.add_argument("--status-file", help="periodically write counters and source states as JSON")
    args = parser.parse_args(argv)
    try:
        config = load_secrets(validate(json.loads(Path(args.config).read_text())))
    except (OSError, json.JSONDecodeError) as error:
        print(f"suifleet-industrial: cannot read configuration: {error}", file=sys.stderr)
        return 2
    except ConfigError as error:
        print(f"suifleet-industrial: invalid configuration: {error}", file=sys.stderr)
        return 2
    if args.check_config:
        print("configuration valid")
        return 0
    collector = Collector(config)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    collector.start()
    collector.log(f"started: {len(collector.sources)} source(s) -> stream {config['output']['stream']!r}")
    while not done.wait(1.0):
        if args.status_file:
            write_status(args.status_file, collector.snapshot())
    left = collector.stop()
    if args.status_file:
        write_status(args.status_file, collector.snapshot())
    collector.log(f"stopped; {left} buffered record(s) not appended")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
