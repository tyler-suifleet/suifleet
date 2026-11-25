#!/usr/bin/env python3
"""Opt-in customer-owned operational monitoring for a SuiFleet edge device (SF-303).

The adapter reads the daemon's authenticated, loopback-only local support
console (docs/LOCAL_CONSOLE.md) with a read-only token and exports:

  * a Prometheus text-format ``/metrics`` endpoint that a customer-owned
    Prometheus scrapes (local pull, bearer-protected, loopback by default);
  * OTLP/HTTP JSON metrics and bounded, redacted component log tails pushed to
    a customer-owned OpenTelemetry endpoint.

It is disabled unless the configuration says ``"enabled": true``. It never
contacts a SuiFleet server, never writes to a chain and never uses device keys.
See docs/EDGE_MONITORING.md.
"""
import argparse
import collections
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import random
import re
import secrets
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1"
EVIDENCE = ("simulated", "physical", "unclassified")
MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 10, 3600, 60
MAX_CONSOLE_RESPONSE = 70 * 1024            # the console caps responses at 64 KiB
MAX_COMPONENTS = 256
MAX_INVENTORY_PAGES = 10                    # requests per inventory read, whatever the page size
MAX_DEPLOYMENT_PAGES = 10                   # requests per journal read, whatever the page size
PAGE_LIMITS = (100, 50, 25, 12, 6, 3, 1)    # page sizes tried when the console says too-large
MAX_LOG_COMPONENTS = 16
MAX_LOG_LINES = 200                         # console limit per request
MAX_LINE_BYTES = 512                        # console limit per line
MAX_LOG_BUFFER = 10_000
MAX_LOG_BATCH = 512
MAX_OTLP_RESPONSE = 64 * 1024
MAX_HEADERS = 16
MAX_SECRET_FILE = 8192
DEFAULT_PROMETHEUS = "127.0.0.1:9469"
SCRAPE_DEADLINE = 5                         # seconds per whole /metrics request
TOKEN_PREFIX = "sfm_"
CONSOLE_TOKEN = re.compile(r"sfc_[A-Za-z0-9_-]{20,128}")
MONITOR_TOKEN = re.compile(TOKEN_PREFIX + r"[A-Za-z0-9_-]{43}")
NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
DEVICE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
PHASES = ("starting", "running", "backoff", "stopping", "stopped", "exited", "broken", "installed", "unconfirmed")
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection"}


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- redaction

SECRET_WORDS = ("key", "token", "secret", "passw", "credential", "private", "auth", "cookie",
                "session", "signature", "mnemonic", "seed", "pin", "cert", "csrf", "passphrase")
_PAIR = re.compile(r"""(?P<name>[A-Za-z0-9_.\-]*(?:%s)[A-Za-z0-9_.\-]*)(?P<sep>\s*[=:]\s*)"""
                   r"""(?P<value>"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|\S+)""" % "|".join(SECRET_WORDS), re.I)
_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic)\s+\S+", re.I)
_PATTERNS = (
    re.compile(r"suiprivkey[0-9a-z]+", re.I),
    re.compile(r"\bsf[cm]_[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?<![0-9A-Za-z])(?<!0x)[0-9a-fA-F]{64,}(?![0-9A-Za-z])"),
)
_BASE64_LINE = re.compile(r"^\s*[A-Za-z0-9+/]{40,}={0,2}\s*$")
REDACTED = "[redacted]"
# The support console's word scrubber (suifleet-edge/src/console.rs word_char
# and secret_value), applied as well so this adapter never exports a word the
# console itself would classify as secret, e.g. a short JWT-shaped selector.
_CONSOLE_WORD = re.compile(r"[A-Za-z0-9_\-+/.~]+")
_CONSOLE_KEY_ID = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
_CONSOLE_HEX = re.compile(r"[0-9a-fA-F]{64,}")


def _console_secret_word(word):
    return (word.lower().startswith("suiprivkey") or word.startswith("sfc_")
            or bool(_CONSOLE_KEY_ID.fullmatch(word))
            or (word.startswith("eyJ") and word.count(".") == 2)
            or bool(_CONSOLE_HEX.fullmatch(word)))


def redact(line):
    """Defense-in-depth scrub of one already console-redacted log line.

    Over-redaction is preferred to a leak; components must still not log secrets.
    """
    if "-----BEGIN" in line or "-----END" in line or _BASE64_LINE.match(line):
        return REDACTED
    line = _SCHEME.sub(lambda m: m.group("scheme") + " " + REDACTED, line)

    def pair(m):
        value = m.group("value")
        if value == REDACTED or value.startswith('"' + REDACTED) or value.startswith("'" + REDACTED) \
                or value.lower() in ("bearer", "basic"):
            return m.group(0)       # already redacted, or an authorization scheme kept before its value
        quote = value[0] if value[:1] in "\"'" else ""
        return m.group("name") + m.group("sep") + quote + REDACTED + (quote if quote and len(value) > 1 and value.endswith(quote) else "")
    line = _PAIR.sub(pair, line)
    for pattern in _PATTERNS:
        line = pattern.sub(REDACTED, line)
    return _CONSOLE_WORD.sub(lambda m: REDACTED if _console_secret_word(m.group(0)) else m.group(0), line)


def bounded(text, limit):
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore")


# ---------------------------------------------------------------- configuration

def _loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _check_private_file(path, label, secret):
    # os.lstat raises ValueError for an embedded NUL or an unencodable
    # (lone-surrogate) path; both are malformed configuration, not a crash.
    if not os.path.isabs(path) or "\0" in path:
        raise ConfigError(f"{label} must be an absolute path")
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        raise ConfigError(f"{label} is not readable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{label} must be a regular file, not a symlink")
    if info.st_mode & (0o077 if secret else 0o022):
        raise ConfigError(f"{label} must not be {'accessible' if secret else 'writable'} by group or others")
    if info.st_uid not in (os.geteuid(), 0):
        raise ConfigError(f"{label} must be owned by the adapter user or root")


def _read_secret(path, label):
    _check_private_file(path, label, secret=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as source:
        data = source.read(MAX_SECRET_FILE + 1)
    if len(data) > MAX_SECRET_FILE:
        raise ConfigError(f"{label} is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{label} is not UTF-8") from None


def _int(section, key, default, low, high, label):
    value = section.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{label} must be an integer between {low} and {high}")
    return value


def _only(section, allowed, label):
    if not isinstance(section, dict):
        raise ConfigError(f"{label} must be an object")
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown {label} field: {unknown[0]}")


class Config:
    """Validated adapter configuration. Secrets are loaded, never logged."""

    def __init__(self, raw, *, check_files=True):
        _only(raw, ("enabled", "evidence", "device", "interval_seconds", "console", "prometheus", "otlp", "logs"), "configuration")
        enabled = raw.get("enabled", False)
        if type(enabled) is not bool:
            raise ConfigError("enabled must be true or false")
        self.enabled = enabled
        if not enabled:
            return
        self.evidence = raw.get("evidence")
        if self.evidence not in EVIDENCE:
            raise ConfigError("evidence must be one of simulated, physical, unclassified")
        self.device = raw.get("device")
        if self.device is not None and (not isinstance(self.device, str) or not DEVICE.fullmatch(self.device)):
            raise ConfigError("device must be 1-128 characters of [A-Za-z0-9._:-]")
        self.interval = _int(raw, "interval_seconds", DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL, "interval_seconds")

        console = raw.get("console")
        _only(console, ("url", "token_file"), "console")
        url = _split_url(console.get("url", "http://127.0.0.1:1441"))
        if url is None or url.scheme != "http" or not url.hostname or not _loopback_host(url.hostname) \
                or url.path not in ("", "/") or url.query or url.fragment or url.username or url.password \
                or _port(url) is None:
            raise ConfigError("console.url must be http://<loopback address>:<port> (the console is loopback-only)")
        self.console_url = f"http://{url.netloc}"
        if not isinstance(console.get("token_file"), str):
            raise ConfigError("console.token_file is required")
        self.console_token_file = console["token_file"]
        if check_files:
            self.console_token = _read_secret(self.console_token_file, "console.token_file").strip()
            if not CONSOLE_TOKEN.fullmatch(self.console_token):
                raise ConfigError("console.token_file must hold one token from suifleet --console-token")

        self.prometheus = None
        if raw.get("prometheus") is not None:
            prom = raw["prometheus"]
            _only(prom, ("listen", "token_sha256", "allow_remote"), "prometheus")
            listen = prom.get("listen", DEFAULT_PROMETHEUS)
            host, port = _split_listen(listen)
            allow_remote = prom.get("allow_remote", False)
            if type(allow_remote) is not bool:
                raise ConfigError("prometheus.allow_remote must be true or false")
            digest = prom.get("token_sha256")
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ConfigError("prometheus.token_sha256 must be 64 lowercase hex characters")
            if not _loopback_host(host):
                if not allow_remote:
                    raise ConfigError("prometheus.listen must be loopback unless allow_remote is true")
                if digest is None:
                    raise ConfigError("a non-loopback prometheus.listen requires token_sha256")
            self.prometheus = dict(host=host.strip("[]"), port=port, token_sha256=digest)

        self.otlp = None
        if raw.get("otlp") is not None:
            otlp = raw["otlp"]
            _only(otlp, ("endpoint", "headers_file", "ca_file", "timeout_seconds", "initial_backoff_seconds",
                         "max_backoff_seconds", "logs"), "otlp")
            endpoint = otlp.get("endpoint")
            if isinstance(endpoint, str) and not re.fullmatch(r"[\x21-\x7e]+", endpoint):
                # urllib/http.client cannot put non-ASCII, whitespace or control
                # characters on the request line; a permanent misconfiguration
                # would otherwise look like an endless collector outage.
                raise ConfigError("otlp.endpoint must be printable ASCII without spaces; percent-encode other characters")
            if isinstance(endpoint, str) and ("?" in endpoint or "#" in endpoint):
                # The OTLP route is appended to the raw string, so even an empty
                # query or fragment delimiter would swallow it ("/c?/v1/metrics").
                raise ConfigError("otlp.endpoint must be an http(s) URL with a valid port and without credentials, query or fragment")
            parsed = _split_url(endpoint)
            if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query \
                    or parsed.fragment or parsed.username or parsed.password or _port(parsed, default=0) is None:
                raise ConfigError("otlp.endpoint must be an http(s) URL with a valid port and without credentials, query or fragment")
            for key in ("headers_file", "ca_file"):
                if otlp.get(key) is not None and not isinstance(otlp[key], str):
                    raise ConfigError(f"otlp.{key} must be a path")
            if parsed.scheme == "http" and not _loopback_host(parsed.hostname):
                raise ConfigError("otlp.endpoint must use https unless it is a loopback collector")
            base = endpoint.rstrip("/")
            self.otlp = dict(
                metrics_url=base + "/v1/metrics", logs_url=base + "/v1/logs",
                timeout=_int(otlp, "timeout_seconds", 5, 1, 30, "otlp.timeout_seconds"),
                initial_backoff=_int(otlp, "initial_backoff_seconds", 5, 1, 300, "otlp.initial_backoff_seconds"),
                max_backoff=_int(otlp, "max_backoff_seconds", 300, 1, 3600, "otlp.max_backoff_seconds"),
                logs=otlp.get("logs", True), headers={}, context=None)
            if type(self.otlp["logs"]) is not bool:
                raise ConfigError("otlp.logs must be true or false")
            if self.otlp["max_backoff"] < self.otlp["initial_backoff"]:
                raise ConfigError("otlp.max_backoff_seconds must be at least initial_backoff_seconds")
            if check_files and otlp.get("headers_file") is not None:
                self.otlp["headers"] = _parse_headers(_read_secret(otlp["headers_file"], "otlp.headers_file"))
            if parsed.scheme == "https":
                ca = otlp.get("ca_file")
                if ca is not None:
                    if check_files:
                        _check_private_file(ca, "otlp.ca_file", secret=False)
                context = ssl.create_default_context(cafile=ca if check_files else None)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.keylog_filename = None
                self.otlp["context"] = context
            elif otlp.get("ca_file") is not None:
                raise ConfigError("otlp.ca_file needs an https endpoint")
            if self.device is None:
                # The OTLP resource is the series identity at a shared collector:
                # without a device id, two adapters with the same evidence class
                # would write contradictory points into one stream.
                raise ConfigError("otlp needs device: a stable identifier unique among devices sharing the collector")

        self.logs = None
        if raw.get("logs") is not None:
            logs = raw["logs"]
            _only(logs, ("components", "max_lines", "max_buffer"), "logs")
            components = logs.get("components")
            if not isinstance(components, list) or not 1 <= len(components) <= MAX_LOG_COMPONENTS \
                    or not all(isinstance(c, str) and NAME.fullmatch(c) for c in components) \
                    or len(set(components)) != len(components):
                raise ConfigError(f"logs.components must list 1-{MAX_LOG_COMPONENTS} distinct component names")
            self.logs = dict(components=components,
                             max_lines=_int(logs, "max_lines", 50, 1, MAX_LOG_LINES, "logs.max_lines"),
                             max_buffer=_int(logs, "max_buffer", 1000, 10, MAX_LOG_BUFFER, "logs.max_buffer"))
            if self.otlp is None or not self.otlp["logs"]:
                raise ConfigError("logs need an otlp endpoint with logs enabled (Prometheus does not ingest logs)")
        if self.prometheus is None and self.otlp is None:
            raise ConfigError("an enabled adapter needs prometheus, otlp or both")


def _split_url(value):
    """urlsplit for a configured string; None for a non-string or unparsable URL."""
    if not isinstance(value, str):
        return None
    try:
        return urllib.parse.urlsplit(value)
    except ValueError:          # for example an unbalanced IPv6 bracket
        return None


def _port(url, default=None):
    """The URL's explicit port in 1-65535, ``default`` when absent, None when invalid."""
    try:
        port = url.port
    except ValueError:          # nonnumeric or out of range
        return None
    if port is None:
        return default
    return port if 1 <= port <= 65535 else None


def _split_listen(listen):
    if not isinstance(listen, str):
        raise ConfigError("prometheus.listen must be host:port")
    host, sep, port = listen.rpartition(":")
    # Bounded ASCII digits only: str.isdigit() also accepts characters such as
    # superscripts that int() rejects, and int() of an overlong digit string
    # raises ValueError instead of the sanitized ConfigError.
    if not sep or not host or not re.fullmatch(r"[0-9]{1,5}", port) or not 0 <= int(port) <= 65535:
        raise ConfigError("prometheus.listen must be host:port")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host != "localhost":
            raise ConfigError("prometheus.listen host must be an IP address or localhost") from None
    return host, int(port)


def _parse_headers(text):
    headers = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not HEADER_NAME.fullmatch(name) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ConfigError(f"otlp.headers_file line {number} must be 'Name: value'")
        if not value.isascii():
            # http.client can only serialize Latin-1 and RFC 9110 treats non-ASCII
            # field octets as obsolete: refuse them here rather than fail every
            # export later, where it would look like a collector outage.
            raise ConfigError(f"otlp.headers_file line {number} value must be printable ASCII")
        if name.lower() in FORBIDDEN_HEADERS or name.lower() in {h.lower() for h in headers}:
            raise ConfigError(f"otlp.headers_file line {number} repeats or overrides a protected header")
        headers[name] = value
    if len(headers) > MAX_HEADERS:
        raise ConfigError(f"otlp.headers_file holds more than {MAX_HEADERS} headers")
    return headers


def load_config(path, *, check_files=True):
    if check_files:
        _check_private_file(path, "configuration", secret=False)
    with open(path, "rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ConfigError("configuration is too large")
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):
        # RecursionError: deeply nested arrays/objects within the size limit.
        raise ConfigError("configuration is not valid JSON") from None
    return Config(raw, check_files=check_files)


# ---------------------------------------------------------------- console client

class ConsoleError(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


# Transport failures, including a truncated or malformed HTTP response
# (http.client.IncompleteRead/BadStatusLine are HTTPException, not OSError).
_IO_ERRORS = (OSError, ValueError, http.client.HTTPException)


def _read_body(response, limit):
    """Reads at most limit + 1 bytes and rejects a body cut short by EOF.

    http.client raises IncompleteRead for a truncated chunked body, but a
    Content-Length body that ends early is returned short without an error.
    The undelivered byte count is left in the response's ``length``.
    """
    data = response.read(limit + 1)
    remaining = getattr(response, "length", None)
    if len(data) <= limit and type(remaining) is int and remaining > 0:
        raise http.client.IncompleteRead(data, remaining)
    return data


def _retry_after(value, cap):
    """Parses a delay-seconds Retry-After header, bounded by cap seconds."""
    value = value.strip() if value else ""
    if not (value.isascii() and value.isdigit()):
        return None
    # Bound the text before int(): a huge value means "wait as long as allowed".
    return int(value) if len(value) <= 9 else cap


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _Deadline:
    """Wall-clock bound on one whole HTTP exchange.

    A socket timeout bounds only each blocking operation, so a peer that keeps
    making slow progress (a byte every few seconds) could hold an exchange
    indefinitely. At the deadline every attached socket is shut down, which
    ends any blocked or later read/write promptly; ``expired`` then tells the
    caller that whatever arrived is incomplete even if it looks well framed.
    Connection setup (see ``_bounded_connection``) additionally caps every
    socket timeout at the remaining budget.
    """

    def __init__(self, seconds):
        self.seconds = seconds
        self.expired = False
        self._lock = threading.Lock()
        self._sockets = []
        self._end = None
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def __enter__(self):
        self._end = time.monotonic() + self.seconds
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        with self._lock:
            self._sockets.clear()
        return False

    def remaining(self):
        """Seconds left; raises TimeoutError once the budget is spent."""
        left = self._end - time.monotonic() if self._end is not None else self.seconds
        if self.expired or left <= 0:
            raise TimeoutError("request deadline exceeded")
        return left

    def attach(self, sock):
        with self._lock:
            if not self.expired:
                self._sockets.append(sock)
                return sock
        _shutdown(sock)
        return sock

    def _expire(self):
        with self._lock:
            self.expired = True
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            _shutdown(sock)


def _shutdown(sock):
    try:
        # The plain socket method, also for an SSLSocket: it shuts down the
        # shared descriptor without touching TLS state another thread is using
        # (for example inside a handshake), which then fails promptly.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass                    # already closed or never connected


# Name resolution has no timeout of its own, so it runs on a helper thread and
# the request stops waiting when its deadline expires. An abandoned lookup
# finishes in the background within the system resolver's own timeouts; at
# most this many may be outstanding, after which new requests fail at once as
# unreachable instead of accumulating threads.
MAX_PENDING_RESOLUTIONS = 4
_resolutions = threading.BoundedSemaphore(MAX_PENDING_RESOLUTIONS)


def _resolve(host, port, deadline):
    try:      # Literal addresses (the loopback console) need no lookup.
        return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)
    except socket.gaierror:
        pass
    if not _resolutions.acquire(blocking=False):
        raise TimeoutError("too many outstanding name resolutions")
    result, done = {}, threading.Event()

    def lookup():
        try:
            result["value"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except OSError as error:
            result["error"] = error
        finally:
            _resolutions.release()
            done.set()
    threading.Thread(target=lookup, daemon=True).start()
    if not done.wait(deadline.remaining()):
        raise TimeoutError("name resolution exceeded the request deadline")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _step_timeout(timeout, deadline):
    left = deadline.remaining()
    return left if not isinstance(timeout, (int, float)) else min(timeout, left)


def _connect(address, timeout, source_address, deadline):
    """socket.create_connection with every address attempt inside the deadline."""
    host, port = address
    last = None
    for family, kind, proto, _, peer in _resolve(host, port, deadline):
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(_step_timeout(timeout, deadline))
            deadline.attach(sock)
            if source_address:
                sock.bind(source_address)
            sock.connect(peer)
            deadline.remaining()         # a connect that completed after expiry
            return sock
        except OSError as error:
            sock.close()
            last = error
            if isinstance(error, TimeoutError) and deadline.expired:
                break
    deadline.remaining()
    raise last if last is not None else OSError("no address to connect to")


def _bounded_connection(http_class, deadline):
    """Connection factory that keeps resolution, every TCP connect attempt,
    the TLS handshake, the request and the response within one wall-clock
    bound: each socket joins `deadline` before it blocks, and each setup step's
    socket timeout is capped at the remaining budget."""
    def factory(*args, **kwargs):
        connection = http_class(*args, **kwargs)
        connection._create_connection = lambda address, timeout, source=None: _connect(
            address, timeout, source, deadline)
        if isinstance(connection, http.client.HTTPSConnection):
            def bounded_tls_connect():
                http.client.HTTPConnection.connect(connection)
                hostname = connection._tunnel_host or connection.host
                # Wrapping detaches the raw socket, so attach the TLS socket
                # before its handshake can block.
                sock = connection._context.wrap_socket(connection.sock, server_hostname=hostname,
                                                       do_handshake_on_connect=False)
                connection.sock = sock
                deadline.attach(sock)
                sock.settimeout(_step_timeout(connection.timeout, deadline))
                sock.do_handshake()
                deadline.remaining()
                sock.settimeout(_step_timeout(connection.timeout, deadline))
            connection.connect = bounded_tls_connect
        return connection
    return factory


class _DeadlineMixin:
    def do_open(self, http_class, req, **kwargs):
        deadline = getattr(req, "deadline", None)
        if deadline is not None:
            http_class = _bounded_connection(http_class, deadline)
        return super().do_open(http_class, req, **kwargs)


class _DeadlineHTTPHandler(_DeadlineMixin, urllib.request.HTTPHandler):
    pass


class _DeadlineHTTPSHandler(_DeadlineMixin, urllib.request.HTTPSHandler):
    pass


def _opener(context=None):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler(),
                                       _DeadlineHTTPSHandler(context=context))


class Console:
    def __init__(self, url, token, timeout=5):
        self.url, self.token, self.timeout = url, token, timeout
        self.opener = _opener()

    def call(self, operation, **fields):
        body = json.dumps(dict(version=1, operation=operation, **fields)).encode()
        request = urllib.request.Request(self.url + "/api/v1/request", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        # self.timeout bounds the whole exchange, not only each socket read.
        with _Deadline(self.timeout) as deadline:
            request.deadline = deadline
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    data = _read_body(response, MAX_CONSOLE_RESPONSE)
            except urllib.error.HTTPError as error:
                try:
                    data = _read_body(error, MAX_CONSOLE_RESPONSE)
                except _IO_ERRORS:
                    raise ConsoleError("unreachable") from None
                finally:
                    error.close()
            except _IO_ERRORS:
                raise ConsoleError("unreachable") from None
        if deadline.expired:
            raise ConsoleError("unreachable")       # whatever arrived may be cut short
        if len(data) > MAX_CONSOLE_RESPONSE:
            raise ConsoleError("too-large")
        try:
            parsed = json.loads(data)
        except (ValueError, RecursionError):
            # RecursionError: deeply nested arrays/objects within the size limit.
            raise ConsoleError("malformed") from None
        if not isinstance(parsed, dict) or parsed.get("version") != 1:
            raise ConsoleError("malformed")
        if parsed.get("status") != "ok":
            status = parsed.get("status")
            raise ConsoleError(status if isinstance(status, str) and NAME.fullmatch(status) else "malformed")
        if not isinstance(parsed.get("data"), dict):
            raise ConsoleError("malformed")
        return parsed["data"]


def _count(value):
    return type(value) is int and 0 <= value <= 2 ** 63


def _altered_name(name):
    """True when the console's redaction or truncation markers altered the name."""
    return REDACTED in name or "[truncated]" in name


def _continuation(cursor, previous, items):
    """Validate a page's ``next`` cursor against the request that produced it.

    The console returns a continuation only after a nonempty page, set to the
    last raw key on that page, and keys strictly increase. A continuation after
    an empty page, or one that does not advance past the previous cursor, would
    repeat the same request indefinitely (or count entries twice), so it is a
    malformed reply rather than more data.
    """
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not items or (previous is not None and cursor <= previous):
        raise ConsoleError("malformed")
    return cursor


def _paged(console, operation, key, max_pages, consume):
    """One bounded, paged console read; ``consume(items)`` returns True to stop.

    The console refuses ("too-large") a page whose serialized reply exceeds
    its 64 KiB ceiling. Entries carry bounded but JSON-escapable text (error
    and exit strings, names), so a full 100-entry page can exceed it while a
    smaller one fits. On "too-large" the page is requested again at the next
    smaller size in PAGE_LIMITS, down to one entry, and that size is kept for
    the rest of this read. A read makes at most ``max_pages`` page requests
    plus ``len(PAGE_LIMITS) - 1`` shrinking retries. Returns True when it
    ended at the page bound with more data remaining (counts are partial).
    """
    after, pages, size = None, 0, 0
    while True:
        fields = dict(limit=PAGE_LIMITS[size])
        if after is not None:
            fields["after"] = after
        try:
            page = console.call(operation, **fields)
        except ConsoleError as error:
            if str(error) != "too-large" or size + 1 >= len(PAGE_LIMITS):
                raise
            size += 1
            continue
        pages += 1
        items = page.get(key)
        if not isinstance(items, list):
            raise ConsoleError("malformed")
        stop = consume(items)
        after = _continuation(page.get("next"), after, items)
        if after is None or stop:
            return False
        if pages >= max_pages:
            return True


def collect(console):
    """One bounded console read: current health, inventory and journal outcomes."""
    health = console.call("health")
    if health.get("health") not in ("healthy", "degraded") or not isinstance(health.get("reasons"), list) \
            or not all(_count(health.get(k)) for k in ("uptime_ms", "pending_receipts", "started_ms")) \
            or not isinstance(health.get("sync"), dict) or not _count(health["sync"].get("consecutive_failures")):
        raise ConsoleError("malformed")
    last_success = health["sync"].get("last_success_ms")
    if last_success is not None and not _count(last_success):
        raise ConsoleError("malformed")
    version = health.get("daemon_version")
    snapshot = dict(healthy=health["health"] == "healthy", reasons=len(health["reasons"]),
                    uptime=health["uptime_ms"] / 1000, pending=health["pending_receipts"],
                    sync_failures=health["sync"]["consecutive_failures"],
                    sync_success=None if last_success is None else last_success / 1000,
                    version=bounded(version, 64) if isinstance(version, str) else "unknown",
                    components=[], components_truncated=False, components_unidentified=0,
                    outcomes=dict(applied=0, failed=0, indeterminate=0, pending=0),
                    outcomes_unreported=0, outcomes_truncated=False)

    def components(items):
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("component"), str):
                raise ConsoleError("malformed")
            if len(snapshot["components"]) >= MAX_COMPONENTS:
                snapshot["components_truncated"] = True
                return True
            phase = item.get("health")
            snapshot["components"].append(dict(
                name=bounded(item["component"], 128),
                installed=item.get("installed") is True,
                supervised=item.get("supervised") is True,
                running=item.get("supervised") is True and phase == "running",
                phase=phase if phase in PHASES else "other",
                restarts=item.get("restarts") if _count(item.get("restarts")) else None))
        return False

    if _paged(console, "inventory", "components", MAX_INVENTORY_PAGES, components):
        snapshot["components_truncated"] = True
    # The console redacts secret-looking inventory names, so distinct raw names
    # (for example two 64-hex names) can share one display name. A redacted or
    # repeated display name is not a series identity: emitting it would write
    # contradictory points under identical labels. Omit those entries and
    # account for them explicitly instead.
    seen = collections.Counter(c["name"] for c in snapshot["components"])
    kept = [c for c in snapshot["components"] if seen[c["name"]] == 1 and not _altered_name(c["name"])]
    snapshot["components_unidentified"] = len(snapshot["components"]) - len(kept)
    snapshot["components"] = kept

    def deployments(items):
        for item in items:
            if not isinstance(item, dict):
                raise ConsoleError("malformed")
            status = item.get("status")
            if item.get("indeterminate") is True:
                outcome = "indeterminate"
            elif status == 1:
                outcome = "applied"
            elif status == 2:
                outcome = "failed"
            else:
                outcome = "pending"
            snapshot["outcomes"][outcome] += 1
            if outcome != "pending" and item.get("reported") is not True:
                snapshot["outcomes_unreported"] += 1
        return False
    if _paged(console, "deployments", "deployments", MAX_DEPLOYMENT_PAGES, deployments):
        snapshot["outcomes_truncated"] = True
    return snapshot


def new_lines(previous, current):
    """Lines in ``current`` after its longest overlap with the end of ``previous``.

    Returns (lines, gap): gap is True when no overlap was found and earlier lines
    may have been missed between polls.
    """
    if previous is None:
        return list(current), False
    for k in range(min(len(previous), len(current)), 0, -1):
        if previous[-k:] == current[:k]:
            return list(current[k:]), False
    return list(current), bool(previous) and bool(current)


# ---------------------------------------------------------------- metric model

class Metric:
    __slots__ = ("name", "kind", "help", "unit", "points")

    def __init__(self, name, kind, help_text, unit=""):
        self.name, self.kind, self.help, self.unit, self.points = name, kind, help_text, unit, []

    def add(self, value, **labels):
        self.points.append((labels, value))
        return self


class Exporter:
    """Holds adapter state shared by the poll loop and the /metrics endpoint."""

    def __init__(self, config, clock=time.time):
        self.config, self.clock = config, clock
        self.lock = threading.Lock()
        self.snapshot = None
        self.up = False
        self.last_success = None
        self.polls = collections.Counter()
        self.otlp_results = collections.Counter()
        self.log_records = collections.Counter()
        self.metric_points_rejected = 0
        self.log_gaps = 0
        self.log_dropped = 0
        self.backoff = dict(metrics=0.0, logs=0.0)
        self.tails = {}
        self.log_buffer = collections.deque()
        self.log_inflight = 0                    # lines of a log batch being posted
        self.log_max = config.logs["max_buffer"] if config.logs else 0
        self.started = clock()

    def base_labels(self):
        labels = dict(evidence=self.config.evidence)
        if self.config.device:
            labels["device"] = self.config.device
        return labels

    def metrics(self):
        with self.lock:
            return self._metrics()

    def _metrics(self):
        out = []
        m = lambda *a: out.append(Metric(*a)) or out[-1]
        m("suifleet_up", "gauge", "Current: 1 when the last local console read succeeded, else 0. Device metrics are omitted while 0.").add(int(self.up))
        m("suifleet_exporter_info", "gauge", "Adapter build and operator-declared evidence class (simulated, physical or unclassified); a declaration, not attestation.").add(1, adapter_version=VERSION)
        polls = m("suifleet_exporter_polls_total", "counter", "Local console reads by result.")
        for result, count in sorted(self.polls.items()):
            polls.add(count, result=result)
        if self.last_success is not None:
            m("suifleet_exporter_last_success_timestamp_seconds", "gauge", "Time of the last successful local console read.", "s").add(self.last_success)
        if self.config.otlp:
            otlp = m("suifleet_exporter_otlp_exports_total", "counter", "OTLP export attempts by signal and result.")
            for (sig, result), count in sorted(self.otlp_results.items()):
                otlp.add(count, signal=sig, result=result)
            m("suifleet_exporter_otlp_metric_points_rejected_total", "counter", "Metric data points the collector refused in an OTLP partial-success reply (not resent).").add(self.metric_points_rejected)
            backoff = m("suifleet_exporter_otlp_backoff_seconds", "gauge", "Current OTLP retry delay after failures (0 when healthy).", "seconds")
            for sig in sorted(self.backoff):
                backoff.add(self.backoff[sig], signal=sig)
        if self.config.logs:
            records = m("suifleet_exporter_log_records_total", "counter", "Redacted component log lines by result (exported, dropped from the bounded buffer, rejected by the collector, unconfirmed after an uninspectable 2xx reply).")
            for result in ("exported", "dropped", "rejected", "unconfirmed"):
                records.add(self.log_records[result], result=result)
            m("suifleet_exporter_log_gaps_total", "counter", "Log polls with no overlap with the previous tail: lines may have been missed.").add(self.log_gaps)
            m("suifleet_exporter_log_buffered", "gauge", "Redacted log lines waiting for export, including a batch in flight.").add(len(self.log_buffer) + self.log_inflight)
        s = self.snapshot if self.up else None
        if s is None:
            return out
        m("suifleet_daemon_info", "gauge", "Daemon version reported by the local console.").add(1, version=s["version"])
        m("suifleet_device_healthy", "gauge", "Current: 1 when the daemon judges the device healthy, 0 when degraded.").add(int(s["healthy"]))
        m("suifleet_device_health_reasons", "gauge", "Current: number of degraded reasons (texts are not exported).").add(s["reasons"])
        m("suifleet_device_uptime_seconds", "gauge", "Current: daemon uptime.", "s").add(s["uptime"])
        m("suifleet_device_pending_receipts", "gauge", "Current: deployment outcomes not yet reported on chain.").add(s["pending"])
        m("suifleet_device_sync_consecutive_failures", "gauge", "Current: consecutive failed chain synchronizations.").add(s["sync_failures"])
        if s["sync_success"] is not None:
            m("suifleet_device_sync_last_success_timestamp_seconds", "gauge", "Current: time of the last successful chain synchronization.", "s").add(s["sync_success"])
        installed = m("suifleet_component_installed", "gauge", "Current: 1 when the component install is confirmed.")
        running = m("suifleet_component_running", "gauge", "Current: 1 when a supervised component is running, else 0.")
        phase = m("suifleet_component_phase", "gauge", "Current: 1 for the component's current lifecycle phase.")
        restarts = m("suifleet_component_restarts", "gauge", "Current daemon run: supervised component restarts (resets when the daemon restarts).")
        for c in s["components"]:
            installed.add(int(c["installed"]), component=c["name"])
            phase.add(1, component=c["name"], phase=c["phase"])
            if c["supervised"]:
                running.add(int(c["running"]), component=c["name"])
                if c["restarts"] is not None:
                    restarts.add(c["restarts"], component=c["name"])
        m("suifleet_components_unidentified", "gauge", "Current: components omitted because their console display name is redacted or not unique, so it cannot identify a series.").add(s["components_unidentified"])
        m("suifleet_components_truncated", "gauge", f"1 when more than {MAX_COMPONENTS} components exist or the bounded inventory read ends early; the rest are omitted.").add(int(s["components_truncated"]))
        outcomes = m("suifleet_deployment_outcomes", "gauge", "Historical: deployment outcomes in the device journal, including past failures.")
        for outcome in ("applied", "failed", "indeterminate", "pending"):
            outcomes.add(s["outcomes"][outcome], outcome=outcome)
        m("suifleet_deployment_outcomes_unreported", "gauge", "Historical: terminal journal outcomes whose receipt is not yet reported on chain.").add(s["outcomes_unreported"])
        m("suifleet_deployment_outcomes_truncated", "gauge", f"1 when the bounded journal read ({MAX_DEPLOYMENT_PAGES} pages of up to 100 entries) ends with entries left uncounted, so counts are partial.").add(int(s["outcomes_truncated"]))
        return out

    # --- poll
    def poll(self, console):
        try:
            snapshot = collect(console)
        except ConsoleError as error:
            with self.lock:
                self.up = False
                self.polls[error.status] += 1
            return False
        with self.lock:
            self.snapshot, self.up = snapshot, True
            self.last_success = self.clock()
            self.polls["ok"] += 1
        if self.config.logs:
            self.poll_logs(console)
        return True

    def poll_logs(self, console):
        cfg = self.config.logs
        for component in cfg["components"]:
            try:
                data = console.call("logs", component=component, limit=cfg["max_lines"])
            except ConsoleError as error:
                with self.lock:
                    self.polls["logs-" + error.status] += 1
                continue
            lines = data.get("lines")
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                with self.lock:
                    self.polls["logs-malformed"] += 1
                continue
            lines = [bounded(x, MAX_LINE_BYTES) for x in lines[-MAX_LOG_LINES:]]
            fresh, gap = new_lines(self.tails.get(component), lines)
            self.tails[component] = lines
            now = self.clock()
            # The raw selector stays local: a name the scrubber would alter
            # (for example 64-character hex, or any word the console's own
            # secret_value policy matches) is exported only as the marker,
            # matching how the console and the inventory metrics treat it.
            label = component if redact(component) == component else REDACTED
            with self.lock:
                self.log_gaps += int(gap)
                for line in fresh:
                    # A batch in flight is still held, so it counts against
                    # max_buffer. Drop the oldest queued line; when the whole
                    # bound is in flight, drop the new line instead.
                    if len(self.log_buffer) + self.log_inflight >= self.log_max:
                        self.log_records["dropped"] += 1
                        if not self.log_buffer:
                            continue
                        self.log_buffer.popleft()
                    self.log_buffer.append((now, label, redact(line)))

    # --- Prometheus exposition
    def render_prometheus(self):
        base = self.base_labels()
        lines = []
        for metric in self.metrics():
            lines.append(f"# HELP {metric.name} {_help(metric.help)}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            for labels, value in metric.points:
                merged = {**base, **labels}
                rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in merged.items())
                lines.append(f"{metric.name}{{{rendered}}} {_number(value)}")
        return ("\n".join(lines) + "\n").encode()

    # --- OTLP JSON
    def resource(self):
        attributes = [_attr("service.name", "suifleet-edge"), _attr("suifleet.evidence", self.config.evidence),
                      _attr("suifleet.monitoring.adapter_version", VERSION)]
        if self.config.device:
            attributes.append(_attr("suifleet.device", self.config.device))
        # The resource identity is fixed for the adapter's lifetime: it does not
        # change with console availability or daemon upgrades. The daemon
        # version is observation data (suifleet_daemon_info{version}).
        return dict(attributes=attributes)

    def otlp_metrics(self):
        now = str(int(self.clock() * 1e9))
        start = str(int(self.started * 1e9))
        out = []
        for metric in self.metrics():
            points = [dict(attributes=[_attr(k, str(v)) for k, v in labels.items()], timeUnixNano=now,
                           **({"asInt": str(value)} if isinstance(value, int) and not isinstance(value, bool) else {"asDouble": float(value)}))
                      for labels, value in metric.points]
            if not points:
                continue
            entry = dict(name=metric.name, description=metric.help, unit=metric.unit or "1")
            if metric.kind == "counter":
                for point in points:
                    point["startTimeUnixNano"] = start
                entry["sum"] = dict(dataPoints=points, aggregationTemporality=2, isMonotonic=True)
            else:
                entry["gauge"] = dict(dataPoints=points)
            out.append(entry)
        return dict(resourceMetrics=[dict(resource=self.resource(), scopeMetrics=[dict(
            scope=dict(name="suifleet.edge_monitoring", version=VERSION), metrics=out)])])

    def otlp_logs(self, batch):
        records = [dict(timeUnixNano=str(int(at * 1e9)), observedTimeUnixNano=str(int(at * 1e9)),
                        severityNumber=0, body=dict(stringValue=line),
                        attributes=[_attr("suifleet.component", component)]) for at, component, line in batch]
        return dict(resourceLogs=[dict(resource=self.resource(), scopeLogs=[dict(
            scope=dict(name="suifleet.edge_monitoring", version=VERSION), logRecords=records)])])


def _attr(key, value):
    return dict(key=key, value=dict(stringValue=value))


def _help(text):
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape(text):
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value):
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


# ---------------------------------------------------------------- OTLP transport

class Transport:
    """POSTs OTLP JSON with bounded exponential backoff per signal."""

    def __init__(self, config, exporter, clock=time.monotonic, rng=random.random):
        self.cfg, self.exporter, self.clock, self.rng = config.otlp, exporter, clock, rng
        self.opener = _opener(self.cfg["context"])
        self.failures = dict(metrics=0, logs=0)
        self.not_before = dict(metrics=0.0, logs=0.0)

    def ready(self, signal_name):
        return self.clock() >= self.not_before[signal_name]

    def post(self, signal_name, payload):
        """Returns 'ok', 'unconfirmed' or 'rejected' (do not resend) or 'retry'."""
        url = self.cfg["metrics_url"] if signal_name == "metrics" else self.cfg["logs_url"]
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {**self.cfg["headers"], "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        retry_after, status, data, unreadable = None, None, None, False
        # The configured timeout bounds the whole exchange, not only each read.
        with _Deadline(self.cfg["timeout"]) as deadline:
            request.deadline = deadline
            try:
                with self.opener.open(request, timeout=self.cfg["timeout"]) as response:
                    status = response.status
                    try:
                        data = _read_body(response, MAX_OTLP_RESPONSE)
                    except _IO_ERRORS:
                        # The status line already arrived: a 2xx means the collector
                        # accepted the payload, so a body cut short (truncation or
                        # timeout) leaves the acknowledgement uninspectable, not the
                        # delivery failed. Other statuses keep their own handling.
                        data, unreadable = b"", True
            except urllib.error.HTTPError as error:
                status, data = error.code, b""
                value = error.headers.get("Retry-After") if error.headers else None
                retry_after = _retry_after(value, self.cfg["max_backoff"])
                error.close()
            except _IO_ERRORS:
                if status is None or not 200 <= status < 300:
                    status = None              # failed before a success status was known
                if data is None:
                    data, unreadable = b"", True
        if deadline.expired and status is not None and 200 <= status < 300:
            data, unreadable = b"", True       # the acknowledgement body may be cut short
        if status is not None and 200 <= status < 300:
            rejected = _INVALID if unreadable else _partial_rejected(data, signal_name)
            if rejected not in (None, _INVALID) and rejected > _payload_items(payload, signal_name):
                rejected = _INVALID                # more refused than sent: impossible
            self._success(signal_name)
            if rejected is _INVALID:
                # Accepted (2xx) but the body could not be read or inspected,
                # or its count is impossible: never resend, never count it, and do not
                # report the records as confirmed exported.
                self._record(signal_name, "invalid_response")
                return ("unconfirmed", None)
            # A positive partial-success count is recorded as "partial", not
            # "ok", so a refused point or record is never shown as delivered.
            self._record(signal_name, "partial" if rejected else "ok")
            return ("ok", rejected)
        if status in (400, 413, 422):
            # Permanent: the collector refused this payload; never resend it.
            self._success(signal_name)
            self._record(signal_name, "rejected")
            return ("rejected", None)
        result = "unauthorized" if status in (401, 403) else ("unreachable" if status is None else "retry")
        self._failure(signal_name, retry_after)
        self._record(signal_name, result)
        return ("retry", None)

    def _record(self, signal_name, result):
        with self.exporter.lock:
            self.exporter.otlp_results[(signal_name, result)] += 1

    def _success(self, signal_name):
        self.failures[signal_name] = 0
        self.not_before[signal_name] = 0.0
        with self.exporter.lock:
            self.exporter.backoff[signal_name] = 0.0

    def _failure(self, signal_name, retry_after):
        self.failures[signal_name] += 1
        delay = min(self.cfg["max_backoff"], self.cfg["initial_backoff"] * 2 ** (self.failures[signal_name] - 1))
        delay *= 0.8 + 0.4 * self.rng()            # +-20 % jitter
        if retry_after is not None:
            delay = max(delay, float(retry_after))
        delay = min(delay, float(self.cfg["max_backoff"]))
        self.not_before[signal_name] = self.clock() + delay
        with self.exporter.lock:
            self.exporter.backoff[signal_name] = round(delay, 3)


_MAX_OTLP_COUNT = 2 ** 63 - 1                 # OTLP int64 counts
_INVALID = object()


def _partial_rejected(data, signal_name):
    """Rejected count from an OTLP partial-success reply.

    None when the reply carries no count; _INVALID when the body exceeds the
    read cap, is not a JSON object, or the count is not a nonnegative int64
    (OTLP JSON encodes int64 as a number or decimal string).
    """
    if not data:
        return None
    if len(data) > MAX_OTLP_RESPONSE:
        return _INVALID                        # over the read cap: cannot be inspected
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError):
        return _INVALID
    if not isinstance(parsed, dict):
        return _INVALID                        # an OTLP response is a JSON object
    partial = parsed.get("partialSuccess")
    if partial is None:
        return None
    if not isinstance(partial, dict):
        return _INVALID
    key = "rejectedDataPoints" if signal_name == "metrics" else "rejectedLogRecords"
    value = partial.get(key)
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_OTLP_COUNT:
        return _INVALID
    return value


def _payload_items(payload, signal_name):
    """Number of data points (metrics) or log records (logs) in an OTLP payload."""
    if signal_name == "metrics":
        return sum(len((metric.get("sum") or metric.get("gauge") or {}).get("dataPoints", ()))
                   for resource in payload["resourceMetrics"] for scope in resource["scopeMetrics"]
                   for metric in scope["metrics"])
    return sum(len(scope["logRecords"]) for resource in payload["resourceLogs"]
               for scope in resource["scopeLogs"])


def export_once(exporter, transport, signals=("metrics", "logs")):
    """Send current metrics and at most one pending log batch if not backing off.

    Returns True when more log lines are queued and logs are not backing off,
    so the caller can schedule another batch after servicing any newer poll.

    A log batch is taken out of the buffer while its request is in flight, so
    the poll loop can keep appending without racing this export; the batch
    still counts against the configured buffer bound (see poll_logs), and a
    batch that must be retried is put back at the front.
    """
    if "metrics" in signals and transport.ready("metrics"):
        result, rejected = transport.post("metrics", exporter.otlp_metrics())
        if result == "ok" and rejected:
            with exporter.lock:
                exporter.metric_points_rejected += rejected
    if "logs" not in signals or not exporter.config.logs or not transport.ready("logs"):
        return False
    with exporter.lock:
        batch = [exporter.log_buffer.popleft() for _ in range(min(MAX_LOG_BATCH, len(exporter.log_buffer)))]
        exporter.log_inflight = len(batch)
    if not batch:
        return False
    result, rejected = "retry", None             # an unexpected error requeues the batch
    try:
        result, rejected = transport.post("logs", exporter.otlp_logs(batch))
    finally:
        with exporter.lock:
            exporter.log_inflight = 0
            if result == "retry":
                exporter.log_buffer.extendleft(reversed(batch))
                while len(exporter.log_buffer) > exporter.log_max:
                    exporter.log_buffer.popleft()              # defensive; poll_logs keeps the bound
                    exporter.log_records["dropped"] += 1
    with exporter.lock:
        if result == "ok":
            refused = rejected or 0                  # bounded by the batch in Transport.post
            exporter.log_records["exported"] += len(batch) - refused
            exporter.log_records["rejected"] += refused
        elif result == "unconfirmed":
            exporter.log_records["unconfirmed"] += len(batch)
        elif result == "rejected":
            exporter.log_records["rejected"] += len(batch)
        more = bool(exporter.log_buffer)
    return more and result != "retry" and transport.ready("logs")


def export_loop(exporter, transport, stop, wakeup):
    """OTLP export worker: runs off the poll loop so that collector latency,
    timeouts and backoff never delay console polling or the withdrawal of
    stale health. `wakeup` is set after each console poll.

    Each turn sends at most one log batch. While more lines are queued the
    worker keeps draining, but a newer console poll is serviced first (metrics
    plus the next batch), so a steadily refilled log queue never starves
    metric updates such as a daemon-down transition. A metrics retry whose
    backoff has elapsed is likewise sent before the next batch, so its
    recovery never waits behind a run of log requests."""
    more = False
    while not stop.is_set():
        if more and not wakeup.is_set():
            retry = transport.failures["metrics"] and transport.ready("metrics")
            more = export_once(exporter, transport, ("metrics", "logs") if retry else ("logs",))
            continue
        due = [t for s, t in transport.not_before.items() if transport.failures[s]]
        timeout = max(0.05, min(due) - transport.clock()) if due else None
        polled = wakeup.wait(timeout)
        if stop.is_set():
            return
        if polled:
            wakeup.clear()
            more = export_once(exporter, transport)
        else:
            # Retry only the signals that are backing off and now due.
            ready = tuple(s for s in ("metrics", "logs") if transport.failures[s] and transport.ready(s))
            more = export_once(exporter, transport, ready) if ready else False


# ---------------------------------------------------------------- Prometheus endpoint

def make_server(exporter):
    cfg = exporter.config.prometheus
    digest = cfg["token_sha256"]

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "suifleet-edge-monitoring"
        sys_version = ""
        timeout = SCRAPE_DEADLINE

        def setup(self):
            # The socket timeout bounds each read/write; this wall-clock deadline
            # bounds the whole request, so a client that trickles bytes cannot
            # hold the single handler and block other scrapes.
            self.deadline = _Deadline(self.timeout).__enter__()
            self.deadline.attach(self.request)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                self.deadline.__exit__(None, None, None)

        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="text/plain; charset=utf-8", extra=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.path != "/metrics":
                return self._send(404, b"not found\n")
            if digest is not None:
                values = self.headers.get_all("Authorization") or []
                presented = values[0] if len(values) == 1 else ""
                scheme, _, credential = presented.partition(" ")
                actual = hashlib.sha256(credential.strip().encode()).hexdigest()
                if scheme.lower() != "bearer" or not hmac.compare_digest(actual, digest):
                    return self._send(401, b"unauthorized\n", extra=[("WWW-Authenticate", 'Bearer realm="suifleet"')])
            self._send(200, exporter.render_prometheus(), "text/plain; version=0.0.4; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            self._send(405, b"method not allowed\n")

        do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_POST

    class Server(http.server.HTTPServer):
        address_family = socket.AF_INET6 if ":" in cfg["host"] else socket.AF_INET
        request_queue_size = 16

        def handle_error(self, request, client_address):
            pass                # e.g. a request cut off at its deadline; never print request data

    return Server((cfg["host"], cfg["port"]), Handler)


# ---------------------------------------------------------------- main

def generate_token():
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return dict(token=token, token_sha256=hashlib.sha256(token.encode()).hexdigest())


def run(config, *, stop=None, ready=None):
    exporter = Exporter(config)
    console = Console(config.console_url, config.console_token)
    transport = Transport(config, exporter) if config.otlp else None
    server = exporter_thread = None
    failure = []
    stop = stop or threading.Event()
    wakeup = threading.Event()
    if config.prometheus:
        server = make_server(exporter)
        threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=0.5), daemon=True).start()
    if transport:
        def worker():
            try:
                export_loop(exporter, transport, stop, wakeup)
            except BaseException as error:     # surface to run() instead of dying silently
                failure.append(error)
                stop.set()
        exporter_thread = threading.Thread(target=worker, name="otlp-export", daemon=True)
        exporter_thread.start()
    if ready:
        ready(exporter, server)
    try:
        next_poll = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_poll:
                exporter.poll(console)
                next_poll = now + config.interval
                wakeup.set()
            stop.wait(max(0.05, next_poll - time.monotonic()))
    finally:
        stop.set()
        wakeup.set()
        if server:
            server.shutdown()
            server.server_close()
        if exporter_thread:
            # Bounded: an in-flight request ends within the OTLP timeout.
            exporter_thread.join(config.otlp["timeout"] + 1)
    if failure:
        raise failure[0]
    return exporter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", help="absolute path of the protected JSON configuration")
    group.add_argument("--token", action="store_true", help="print a new /metrics bearer token and its SHA-256")
    parser.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)
    if args.token:
        print(json.dumps(generate_token()))
        return 0
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"edge monitoring configuration rejected: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("edge monitoring configuration is not readable", file=sys.stderr)
        return 2
    if not config.enabled:
        print("edge monitoring is disabled (set \"enabled\": true to opt in)", file=sys.stderr)
        return 0
    if args.check_config:
        print("edge monitoring configuration ok", file=sys.stderr)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def ready(_exporter, server):
        where = f" Prometheus on {server.server_address[0]}:{server.server_address[1]}" if server else ""
        print(f"edge monitoring started;{where} evidence={config.evidence}", file=sys.stderr, flush=True)
    try:
        run(config, stop=stop, ready=ready)
    except OSError as error:
        # Never print request bodies, headers or tokens; errno names only.
        print(f"edge monitoring stopped: {type(error).__name__} {getattr(error, 'errno', '')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
