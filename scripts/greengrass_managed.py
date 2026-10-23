"""SF-038 AWS-managed component catalog checks and adapter reference models.

Imported by scripts/greengrass_compat.py. Validates
interfaces/compat/managed-components.json and defines two *specification
reference models*: AWS recipe dependency classification against the catalog,
and the proposed credential-provider (token exchange equivalent) contract.
Like the SF-028 models, these make fixtures executable and self-consistent;
they are not an adapter and prove no runtime conformance.
"""
from __future__ import annotations

import datetime
import math
import re
from pathlib import Path

CATALOG = Path("interfaces/compat/managed-components.json")
AWS_TYPES = {"generic", "plugin", "lambda", "nucleus"}
AWS_OS = {"linux", "windows"}
SPECIFICATIONS = {"complete", "incomplete"}
PHASES = {"initial-prototype", "roadmap"}
MANAGED_PREFIXES = ("aws.greengrass.", "aws.iot.", "variant.")
ADAPTER_TEXT = ("summary", "auth", "licensing")

NUM = r"(0|[1-9][0-9]*)"
IDENT = r"(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
CORE = rf"{NUM}\.{NUM}\.{NUM}(-{IDENT}(\.{IDENT})*)?"
COMPARATOR = re.compile(rf"^(>=|<=|>|<|=|\^|~)?({CORE})$")
# Same grammar as the native recipe schema and SF-037 translation.
RANGE = re.compile(rf"^(\*|(>=|<=|>|<|=|\^|~)?{CORE}( (>=|<=|>|<|=|\^|~)?{CORE})*)$")


# ---------------------------------------------------------------------------
# Version ranges (native comparator grammar) used for adapter compatibility.
# ---------------------------------------------------------------------------
def _key(version: str):
    """SemVer 2.0.0 precedence key (spec item 11).

    A prerelease sorts before its release. Prerelease identifiers compare left
    to right: numeric ones as integers and below alphanumeric ones, which
    compare as ASCII; a shorter identifier list that is a prefix sorts first.
    """
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not pre:
        return (major, minor, patch, 1, ())
    ids = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (major, minor, patch, 0, ids)


def satisfies(version: str, requirement: str) -> bool:
    """True when version satisfies a native-grammar range (space means AND)."""
    if not RANGE.match(requirement):
        raise ValueError(f"unsupported range {requirement!r}")
    if requirement == "*":
        return True
    value = _key(version)
    for comparator in requirement.split(" "):
        op, bound = COMPARATOR.match(comparator).group(1, 2)
        low = _key(bound)
        major, minor, patch = low[:3]
        if op == "^":
            high = (major + 1, 0, 0, 0, ()) if major else (0, minor + 1, 0, 0, ()) if minor else (0, 0, patch + 1, 0, ())
            ok = low <= value < high
        elif op == "~":
            ok = low <= value < (major, minor + 1, 0, 0, ())
        else:
            ok = {">=": value >= low, "<=": value <= low, ">": value > low, "<": value < low}.get(op, value == low)
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# AWS recipe ComponentDependencies -> catalog disposition. The SF-037
# translator (dapp/src/lib/greengrass-recipe.ts) implements the same
# classification and executes managed-dependency-cases.json against it.
# ---------------------------------------------------------------------------
def classify_dependency(catalog: dict, name, spec) -> dict:
    if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) - {"VersionRequirement", "DependencyType"}:
        return {"outcome": "reject", "reason": "invalid-dependency"}
    if spec.get("DependencyType", "HARD") not in ("HARD", "SOFT"):
        return {"outcome": "reject", "reason": "invalid-dependency"}
    requirement = spec.get("VersionRequirement", "*")
    if not isinstance(requirement, str) or not RANGE.match(requirement):
        return {"outcome": "reject", "reason": "unsupported-version-range"}
    row = next((c for c in catalog["components"] if c["name"] == name), None)
    if row is None:
        if name.startswith(MANAGED_PREFIXES):
            return {"outcome": "reject", "reason": "unknown-managed-component"}
        return {"outcome": "not-managed"}
    if "discontinued" in row:
        return {"outcome": "reject", "reason": "discontinued-aws-service"}
    disposition = row["disposition"]
    if disposition == "roadmap":
        return {"outcome": "reject", "reason": "roadmap-gap", "tickets": row["tickets"]}
    if disposition == "not-applicable":
        return {"outcome": "reject", "reason": "not-applicable"}
    if disposition == "native":
        return {"outcome": "replace", "tickets": row["tickets"]}
    if disposition == "native-component":
        return {"outcome": "package-natively", "tickets": row["tickets"]}
    adapter = next(a for a in catalog["adapters"] if a["id"] == row["adapter"])
    if adapter["specification"] != "complete":
        return {"outcome": "reject", "reason": "adapter-specification-incomplete", "adapter": adapter["id"],
                "implementation_ticket": adapter["implementation_ticket"]}
    version = adapter["replaces"][name]
    if not satisfies(version, requirement):
        return {"outcome": "reject", "reason": "unsupported-aws-version", "adapter": adapter["id"]}
    return {"outcome": "defer", "adapter": adapter["id"], "requires": sorted(adapter["depends_on"]),
            "implementation_ticket": adapter["implementation_ticket"]}


def check_dependency_cases(cases, catalog: dict) -> int:
    for case in cases:
        actual = classify_dependency(catalog, case["dependency"], case["spec"])
        if actual != case["expected"]:
            raise AssertionError(f"managed dependency case {case['id']}: {actual!r} != {case['expected']!r}")
    return len(cases)


# ---------------------------------------------------------------------------
# Proposed credential-provider contract: the AWS container-credentials
# protocol consumed by unmodified AWS SDKs through
# AWS_CONTAINER_CREDENTIALS_FULL_URI / AWS_CONTAINER_AUTHORIZATION_TOKEN.
# Retry delays equal the TokenExchangeService 2.0.3 credentialRetryInSec
# defaults. Refresh margin, deny-by-default grants and per-component tokens
# are SuiFleet proposals.
# ---------------------------------------------------------------------------
CREDENTIAL_PATH = "/2016-11-01/credentialprovider/"
CREDENTIAL_URI = "http://127.0.0.1:{port}" + CREDENTIAL_PATH
REFRESH_MARGIN = 300
RETRY_SECONDS = {"client-error": 120, "server-error": 60, "unknown-error": 300}


class CredentialProvider:
    def __init__(self):
        self.grants: set = set()
        self.active: dict = {}  # component -> current token
        self.issued: set = set()
        self.upstream: list = []
        self.cache = None  # (credential_set, expiration)
        self.retry_at = None
        self.calls = 0
        self.now = 0

    def _revoke(self, component):
        self.active.pop(component, None)

    def _ok(self):
        return {"status": "ok", "credential_set": self.cache[0], "expiration": self.cache[1], "upstream_calls": self.calls}

    def _unavailable(self):
        return {"status": "unavailable", "retry_after": self.retry_at - self.now, "upstream_calls": self.calls}

    def apply(self, op: dict) -> dict:
        if "now" in op:
            if op["now"] < self.now:
                raise AssertionError("fixture time must not move backwards")
            self.now = op["now"]
        kind = op["op"]
        if kind == "grant":
            self.grants.add(op["component"])
            return {}
        if kind == "revoke":
            self.grants.discard(op["component"])
            self._revoke(op["component"])
            return {}
        if kind == "start":
            component, token = op["component"], op["token"]
            if token in self.issued:
                raise AssertionError("fixture tokens must be unique")
            self._revoke(component)
            if component not in self.grants:
                return {"env": {}}
            self.issued.add(token)
            self.active[component] = token
            return {"env": {"AWS_CONTAINER_CREDENTIALS_FULL_URI": CREDENTIAL_URI, "AWS_CONTAINER_AUTHORIZATION_TOKEN": token}}
        if kind == "stop":
            self._revoke(op["component"])
            return {}
        if kind == "upstream":
            self.upstream.append(op)
            return {}
        if kind != "request":
            raise AssertionError(f"unknown op {kind}")
        if op["method"] != "GET":
            return {"status": "method-not-allowed", "upstream_calls": self.calls}
        if op["path"] != CREDENTIAL_PATH:
            return {"status": "not-found", "upstream_calls": self.calls}
        token = op.get("authorization")
        if not isinstance(token, str) or token not in self.active.values():
            return {"status": "unauthorized", "upstream_calls": self.calls}
        valid = self.cache is not None and self.cache[1] > self.now
        if valid and self.cache[1] - self.now > REFRESH_MARGIN:
            return self._ok()
        if self.retry_at is not None and self.now < self.retry_at:
            return self._ok() if valid else self._unavailable()
        if not self.upstream:
            raise AssertionError("fixture must queue an upstream result before a refresh")
        result = self.upstream.pop(0)
        self.calls += 1
        if result["result"] == "ok":
            self.cache = (result["credential_set"], result["expiration"])
            self.retry_at = None
            return self._ok()
        self.retry_at = self.now + RETRY_SECONDS[result["result"]]
        return self._ok() if valid else self._unavailable()


# ---------------------------------------------------------------------------
# Proposed cloudwatch-metrics-export contract, pinned to
# aws.greengrass.Cloudwatch 4.0.0: the cloudwatch/metric/put request format,
# validation, PublishInterval (default 10 s, at most 900 s, 0 = no batching),
# MaxMetricsToRetain (default 5000, at least 2000), 20 metrics per
# PutMetricData call, a 20-metric namespace trigger, and same-namespace
# oldest-first replacement when the buffer is full. SuiFleet proposals: SF-027
# publisher grants, counted drops, and a buffer that survives restarts (the
# AWS component loses it).
# ---------------------------------------------------------------------------
METRIC_UNITS = frozenset(
    ["Seconds", "Microseconds", "Milliseconds", "Bytes", "Kilobytes", "Megabytes", "Gigabytes", "Terabytes",
     "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits", "Percent", "Count", "Bytes/Second",
     "Kilobytes/Second", "Megabytes/Second", "Gigabytes/Second", "Terabytes/Second", "Bits/Second",
     "Kilobits/Second", "Megabits/Second", "Gigabits/Second", "Terabits/Second", "Count/Second", "None"])
METRIC_BATCH = 20
RETRYABLE = {"throttled", "server-error", "connection-error"}


def _number(value) -> bool:
    """A finite JSON number; booleans, NaN and infinities are not numbers here.

    JSON integers are unbounded in Python and are never converted to float, so
    an integer too large for a float is still classified without raising;
    callers compare it against their exact bounds."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _metric_valid(message) -> bool:
    request = message.get("request") if isinstance(message, dict) else None
    if not isinstance(request, dict) or set(request) != {"namespace", "metricData"}:
        return False
    namespace, data = request["namespace"], request["metricData"]
    if not isinstance(namespace, str) or not 0 < len(namespace) <= 255 or namespace[0] == ":" or namespace.startswith("AWS/"):
        return False
    if not isinstance(data, dict) or set(data) - {"metricName", "value", "dimensions", "timestamp", "unit"}:
        return False
    name, value = data.get("metricName"), data.get("value")
    if not isinstance(name, str) or not 0 < len(name) <= 255 or not _number(value):
        return False
    if value != 0 and not 8.515920e-109 <= abs(value) <= 1.174271e+108:
        return False
    dimensions = data.get("dimensions", [])
    if not isinstance(dimensions, list) or len(dimensions) > 10:
        return False
    for dimension in dimensions:
        if not isinstance(dimension, dict) or set(dimension) != {"name", "value"}:
            return False
        if not all(isinstance(dimension[k], str) and dimension[k] for k in ("name", "value")) or dimension["name"] == "coreName":
            return False
    if "timestamp" in data and (not _number(data["timestamp"]) or data["timestamp"] < 0):
        return False
    unit = data.get("unit", "None")
    return isinstance(unit, str) and unit in METRIC_UNITS


class MetricsExport:
    def __init__(self):
        self.interval, self.retain = 10, 5000
        self.grants: set = set()
        self.buffer: list = []  # (seq, namespace, metricName), arrival order
        self.upstream: list = []
        self.seq = self.dropped = self.rejected = 0
        self.last_publish = 0
        self.now = 0

    def _state(self, **extra):
        return {**extra, "buffered": len(self.buffer), "dropped": self.dropped, "rejected": self.rejected}

    def apply(self, op: dict) -> dict:
        if "now" in op:
            if op["now"] < self.now:
                raise AssertionError("fixture time must not move backwards")
            self.now = op["now"]
        kind = op["op"]
        if kind == "configure":
            interval, retain = op.get("publish_interval", self.interval), op.get("max_metrics_to_retain", self.retain)
            if not isinstance(interval, int) or isinstance(interval, bool) or not 0 <= interval <= 900:
                return {"status": "invalid-config"}
            if not isinstance(retain, int) or isinstance(retain, bool) or retain < 2000:
                return {"status": "invalid-config"}
            self.interval, self.retain = interval, retain
            return {"status": "ok"}
        if kind == "grant":
            self.grants.add(op["publisher"])
            return {}
        if kind == "revoke":
            self.grants.discard(op["publisher"])
            return {}
        if kind == "upstream":
            self.upstream.extend(op["results"])
            return {}
        if kind == "restart":
            self.last_publish = self.now
            return self._state()
        if kind == "put":
            if op["publisher"] not in self.grants:
                return {"status": "fail", "error": "Unauthorized"}
            if not _metric_valid(op["message"]):
                return {"status": "fail", "error": "InvalidInputException"}
            namespace = op["message"]["request"]["namespace"]
            name = op["message"]["request"]["metricData"]["metricName"]
            for _ in range(op.get("count", 1)):
                self.seq += 1
                self.buffer.append((self.seq, namespace, name))
                if len(self.buffer) > self.retain:
                    # The new entry is always in its namespace, so this terminates;
                    # a namespace with nothing older loses the new metric itself.
                    self.buffer.pop(next(i for i, e in enumerate(self.buffer) if e[1] == namespace))
                    self.dropped += 1
            return self._state(status="success")
        if kind != "tick":
            raise AssertionError(f"unknown op {kind}")
        counts: dict = {}
        for _, namespace, _ in self.buffer:
            counts[namespace] = counts.get(namespace, 0) + 1
        due = self.now - self.last_publish >= self.interval or any(n >= METRIC_BATCH for n in counts.values())
        if not self.buffer or not due:
            return self._state(calls=[])
        self.last_publish = self.now
        calls, done = [], set()
        for namespace in counts:  # first-arrival order
            entries = [e for e in self.buffer if e[1] == namespace]
            for start in range(0, len(entries), METRIC_BATCH):
                batch = entries[start:start + METRIC_BATCH]
                if not self.upstream:
                    raise AssertionError("fixture must queue an upstream result for each PutMetricData call")
                result = self.upstream.pop(0)
                calls.append({"namespace": namespace, "count": len(batch), "first": batch[0][2],
                              "last": batch[-1][2], "result": result})
                if result == "invalid":
                    self.rejected += len(batch)
                elif result not in RETRYABLE | {"ok"}:
                    raise AssertionError(f"unknown upstream result {result}")
                if result not in RETRYABLE:  # retryable batches go out again in the next publish
                    done.update(e[0] for e in batch)
        self.buffer = [e for e in self.buffer if e[0] not in done]
        return self._state(calls=calls)


# ---------------------------------------------------------------------------
# Proposed cloudwatch-logs-export contract, pinned to aws.greengrass.LogManager
# 2.3.14: periodicUploadIntervalSec (default 300), minimumLogLevel (default
# INFO), log group /aws/greengrass/{componentType}/{region}/{componentName},
# log stream /{yyyy/MM/dd}/thing/{thingName} with ':' replaced by '+', and the
# PutLogEvents limits (chronological events, at most 10,000 events and
# 1,048,576 bytes counting 26 bytes per event, within one day). As in
# LogManager 2.2.2 and later, a line over 256 KiB (counting the 26 bytes) is
# split into consecutive events at UTF-8 character boundaries. SuiFleet
# proposals: configured literal redaction before spooling, a bounded durable
# spool with counted oldest-first drops (a line larger than the whole spool
# drops only itself), and a per-source checkpoint that only advances on a
# successful response. A lost response is resent unchanged (at least once),
# never skipped.
# ---------------------------------------------------------------------------
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
LOG_COMPONENT_TYPES = {"UserComponent", "GreengrassSystemComponent"}
LOG_BATCH_EVENTS = 10000
LOG_BATCH_BYTES = 1048576
LOG_EVENT_OVERHEAD = 26
LOG_EVENT_BYTES = 262144
LOG_RETRYABLE = RETRYABLE | {"response-lost"}
LOG_TIME_LIMIT = 253402300800  # 10000-01-01T00:00:00Z; event times must name a UTC day


class LogsExport:
    def __init__(self):
        self.interval, self.minimum, self.limit = 300, "INFO", 10485760
        self.redact: list = []
        self.region, self.thing = "us-east-1", "thing"
        self.sources: dict = {}
        self.spool: list = []  # (seq, source, time, message), arrival order
        self.upstream: list = []
        self.seq = self.dropped = 0
        self.last_upload = 0
        self.now = 0

    @staticmethod
    def _size(message: str) -> int:
        return len(message.encode()) + LOG_EVENT_OVERHEAD

    @staticmethod
    def _split(message: str) -> list:
        data, limit, parts = message.encode(), LOG_EVENT_BYTES - LOG_EVENT_OVERHEAD, []
        while len(data) > limit:
            cut = limit
            while data[cut] & 0xC0 == 0x80:  # never cut inside a UTF-8 character
                cut -= 1
            parts.append(data[:cut].decode())
            data = data[cut:]
        return parts + [data.decode()]

    @staticmethod
    def _time(value) -> bool:
        # Checked before spooling, so the day of every spooled event can be formatted.
        return _number(value) and 0 <= value < LOG_TIME_LIMIT

    def _state(self, **extra):
        return {**extra, "spooled": len(self.spool), "dropped": self.dropped}

    def _configure(self, op) -> bool:
        interval = op.get("interval", self.interval)
        if not _number(interval) or interval <= 0 or op.get("minimum_level", self.minimum) not in LOG_LEVELS:
            return False
        limit = op.get("spool_limit", self.limit)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return False
        redact = op.get("redact", self.redact)
        if not isinstance(redact, list) or not all(isinstance(r, str) and r for r in redact):
            return False
        sources = op.get("sources", self.sources)
        if not isinstance(sources, dict) or not all(isinstance(t, str) and t in LOG_COMPONENT_TYPES for t in sources.values()):
            return False
        thing = op.get("thing", self.thing)
        if not isinstance(thing, str) or not thing:
            return False
        self.interval, self.minimum, self.limit = interval, op.get("minimum_level", self.minimum), limit
        self.redact, self.sources, self.thing = redact, sources, thing
        self.region = op.get("region", self.region)
        return True

    def _batches(self, events):
        batch, size, day = [], 0, None
        for event in events:
            event_day = datetime.datetime.fromtimestamp(event[2], datetime.timezone.utc).strftime("%Y/%m/%d")
            event_size = self._size(event[3])
            if event_size > LOG_EVENT_BYTES:
                raise AssertionError("spooled events must already be split to the event limit")
            if batch and (event_day != day or len(batch) == LOG_BATCH_EVENTS or size + event_size > LOG_BATCH_BYTES):
                yield day, batch
                batch, size = [], 0
            batch.append(event)
            size += event_size
            day = event_day
        if batch:
            yield day, batch

    def apply(self, op: dict) -> dict:
        if "now" in op:
            if op["now"] < self.now:
                raise AssertionError("fixture time must not move backwards")
            self.now = op["now"]
        kind = op["op"]
        if kind == "configure":
            return {"status": "ok" if self._configure(op) else "invalid-config"}
        if kind == "upstream":
            self.upstream.extend(op["results"])
            return {}
        if kind == "restart":
            return self._state()
        if kind == "log":
            if not isinstance(op["source"], str) or op["source"] not in self.sources:
                return {"status": "unknown-source"}
            repeat = op.get("repeat", 1)  # fixture shorthand: the line is message * repeat
            if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat <= 0:
                raise AssertionError("fixture repeat must be a positive integer")
            if op.get("level") not in LOG_LEVELS or not isinstance(op.get("message"), str) or not self._time(op.get("time")):
                return {"status": "invalid"}
            if LOG_LEVELS.index(op["level"]) < LOG_LEVELS.index(self.minimum):
                return {"status": "filtered"}
            message = op["message"] * repeat
            for secret in self.redact:
                message = message.replace(secret, "[REDACTED]")
            parts = self._split(message)
            # A line larger than the whole spool drops itself without evicting older events.
            if sum(self._size(part) for part in parts) > self.limit:
                self.dropped += len(parts)
                return self._state(status="dropped", events=len(parts))
            for part in parts:
                self.seq += 1
                self.spool.append((self.seq, op["source"], op["time"], part))
            while sum(self._size(e[3]) for e in self.spool) > self.limit:
                self.spool.pop(0)
                self.dropped += 1
            return self._state(status="spooled", events=len(parts))
        if kind != "tick":
            raise AssertionError(f"unknown op {kind}")
        if not self.spool or self.now - self.last_upload < self.interval:
            return self._state(uploads=[])
        self.last_upload = self.now
        uploads, sent = [], set()
        for source in sorted({e[1] for e in self.spool}):
            # Chronological order is required; equal times keep arrival order.
            events = sorted((e for e in self.spool if e[1] == source), key=lambda e: e[2])
            group = f"/aws/greengrass/{self.sources[source]}/{self.region}/{source}"
            for day, batch in self._batches(events):
                if not self.upstream:
                    raise AssertionError("fixture must queue an upstream result for each PutLogEvents call")
                result = self.upstream.pop(0)
                uploads.append({"group": group, "stream": f"/{day}/thing/{self.thing.replace(':', '+')}",
                                "events": [[e[2], e[3]] for e in batch], "result": result})
                if result in LOG_RETRYABLE:
                    break  # the checkpoint never passes an unacknowledged batch
                if result != "ok":
                    raise AssertionError(f"unknown upstream result {result}")
                sent.update(e[0] for e in batch)
        self.spool = [e for e in self.spool if e[0] not in sent]
        return self._state(uploads=uploads)


# ---------------------------------------------------------------------------
# Catalog structure and cross-reference checks.
# ---------------------------------------------------------------------------
def check_catalog(root: Path, catalog: dict, compat: dict, backlog_keys: set) -> list:
    errors = []
    if catalog.get("format_version") != 1:
        errors.append("managed catalog: format_version must be 1")
    if catalog.get("parity_claim") is not False:
        errors.append("managed catalog: parity_claim must be false")
    families = catalog.get("families", {})
    dispositions = catalog.get("dispositions", {})
    adapters = {a.get("id"): a for a in catalog.get("adapters", [])}
    if len(adapters) != len(catalog.get("adapters", [])):
        errors.append("managed catalog: duplicate adapter")
    rows = {}
    for row in catalog.get("components", []):
        name = row.get("name")
        label = f"component {name}"
        if name in rows:
            errors.append(f"{label}: duplicate")
        rows[name] = row
        if row.get("family") not in families:
            errors.append(f"{label}: unknown family")
        aws = row.get("aws", {})
        if aws.get("type") not in AWS_TYPES or not aws.get("os") or set(aws.get("os", [])) - AWS_OS:
            errors.append(f"{label}: aws type/os")
        disposition = row.get("disposition")
        if disposition not in dispositions:
            errors.append(f"{label}: disposition {disposition!r}")
        for ticket in row.get("tickets", []):
            if ticket not in backlog_keys:
                errors.append(f"{label}: unknown ticket {ticket}")
        if disposition in ("native", "native-component") and not row.get("tickets"):
            errors.append(f"{label}: {disposition} needs owning tickets")
        if not row.get("native_equivalent"):
            errors.append(f"{label}: needs native_equivalent guidance")
        if disposition == "adapter" and "adapter" not in row:
            errors.append(f"{label}: adapter disposition needs an adapter")
        if "adapter" in row and row["adapter"] not in adapters:
            errors.append(f"{label}: unknown adapter {row['adapter']}")
        elif disposition == "adapter" and "adapter" in row and name not in (adapters[row["adapter"]].get("replaces") or {}):
            errors.append(f"{label}: adapter {row['adapter']} does not list it in replaces")
        if disposition == "roadmap" and "SF-064" not in row.get("tickets", []):
            errors.append(f"{label}: roadmap gaps are tracked by SF-064")
        if "discontinued" in row and disposition != "not-applicable":
            errors.append(f"{label}: discontinued AWS services can only be not-applicable")
        if "approval" in row:
            errors.append(f"{label}: approvals are not recorded in this catalog")
    for family in catalog.get("required_families", []):
        if family not in families or not any(r.get("family") == family for r in rows.values()):
            errors.append(f"managed catalog: required family {family} has no component")
    platforms = {p["id"]: p for p in compat.get("platforms", [])}
    phases = {}
    for entry in catalog.get("platform_phases", []):
        pid = entry.get("platform")
        label = f"platform phase {pid}"
        if pid in phases:
            errors.append(f"{label}: duplicate")
        phases[pid] = entry.get("phase")
        if pid not in platforms:
            errors.append(f"{label}: unknown platform")
        if entry.get("phase") not in PHASES or not entry.get("reason"):
            errors.append(f"{label}: needs phase and reason")
        if entry.get("phase") == "initial-prototype" and pid in platforms and platforms[pid].get("native") is None:
            errors.append(f"{label}: initial-prototype platforms need a native runtime")
        if entry.get("phase") == "roadmap" and "SF-064" not in entry.get("tickets", []):
            errors.append(f"{label}: roadmap platforms are tracked by SF-064")
        for ticket in entry.get("tickets", []):
            if ticket not in backlog_keys:
                errors.append(f"{label}: unknown ticket {ticket}")
        if "approval" in entry:
            errors.append(f"{label}: approvals are not recorded in this catalog")
    for pid in platforms:
        if pid not in phases:
            errors.append(f"platform {pid}: has no SF-038 phase")
    suites = compat.get("suites", {})
    for aid, adapter in adapters.items():
        label = f"adapter {aid}"
        complete = adapter.get("specification") == "complete"
        if adapter.get("specification") not in SPECIFICATIONS:
            errors.append(f"{label}: specification")
        for text in ADAPTER_TEXT:
            if not adapter.get(text):
                errors.append(f"{label}: needs {text}")
        if not adapter.get("aws_services"):
            errors.append(f"{label}: needs aws_services")
        replaces = adapter.get("replaces") or {}
        if not replaces:
            errors.append(f"{label}: replaces no AWS component")
        for name, version in replaces.items():
            if complete and not re.fullmatch(CORE, str(version)):
                errors.append(f"{label}: complete specifications must pin an exact AWS version for {name}")
            row = rows.get(name)
            if row is None:
                errors.append(f"{label}: replaces unknown component {name}")
            elif row.get("adapter") != aid:
                errors.append(f"{label}: {name} does not reference this adapter")
            elif "reference_version" in row and version != row["reference_version"]:
                errors.append(f"{label}: {name} version {version} != reference {row['reference_version']}")
        if not adapter.get("platforms"):
            errors.append(f"{label}: needs platforms")
        for platform in adapter.get("platforms", []):
            if platform not in platforms or platforms[platform].get("native") is None:
                errors.append(f"{label}: platform {platform} has no native runtime")
            elif phases.get(platform) != "initial-prototype":
                errors.append(f"{label}: platform {platform} is not in the initial prototype phase")
        for ticket in adapter.get("depends_on", []):
            if ticket not in backlog_keys:
                errors.append(f"{label}: unknown ticket {ticket}")
        acceptance = adapter.get("acceptance", {})
        if not acceptance.get("runtime_tests") or not acceptance.get("environment"):
            errors.append(f"{label}: needs runtime_tests and environment")
        if complete and not acceptance.get("reference_suites"):
            errors.append(f"{label}: complete specifications need an executable reference suite")
        for suite in acceptance.get("reference_suites", []):
            if suite not in suites:
                errors.append(f"{label}: unknown suite {suite}")
        ticket = adapter.get("implementation_ticket")
        if ticket not in backlog_keys:
            errors.append(f"{label}: unknown implementation ticket {ticket}")
        if ticket in adapter.get("depends_on", []):
            errors.append(f"{label}: depends on its own implementation ticket")
        if "ticket_request" in adapter:
            errors.append(f"{label}: ticket_request is obsolete; link implementation_ticket")
        # Reference-model agreement is not adapter execution; only the
        # implementation ticket may record a runtime receipt.
        execution = adapter.get("runtime_execution")
        if execution not in ("pending", "passed"):
            errors.append(f"{label}: runtime_execution {execution!r}")
        elif execution == "passed":
            receipt = adapter.get("runtime_evidence")
            if not receipt or not (root / receipt).exists():
                errors.append(f"{label}: passed runtime execution needs an existing runtime_evidence receipt")
        if not complete and not adapter.get("specification_gap"):
            errors.append(f"{label}: incomplete specifications need a specification_gap")
    return errors
