#!/usr/bin/env python3
"""Request-checking local CloudWatch Logs emulator for the SF-060 adapter.

Serves, on one loopback port:

  POST /                              the CloudWatch Logs JSON 1.1 API subset the
                                      adapter uses: CreateLogGroup, CreateLogStream
                                      and PutLogEvents
  GET  /2016-11-01/credentialprovider/  a stand-in for the SF-057 container
                                      credential endpoint (bearer token checked)
  /_control/...                       test control: reset, queued results, state

Every API request is checked independently of the adapter's code: the SigV4
signature (recomputed here with hmac/hashlib), the session token, the
signed-header set, the request time, the target and content type, the exact
body shape, CloudWatch's name rules and the PutLogEvents limits (at most
10,000 events and 1,048,576 bytes counting 26 bytes per event, events of at
most 262,144 bytes, in time order, within one UTC day). A failed check is
recorded as a violation and answered InvalidParameterException; tests require
none.

PutLogEvents results are injected from a queue: ok, throttled, server-error,
response-lost (events stored, connection closed without an answer), rejected
(InvalidParameterException) and too-old (the first event refused through
rejectedLogEventsInfo). A put to a missing stream is answered
ResourceNotFoundException without taking a queued result.

Run `--serve` to print {"port", "credentials_uri", "token"} as one JSON line
and serve until stdin closes.
"""
import argparse
import datetime
import hashlib
import hmac
import json
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCESS_KEY = "AKIDSF060EMULATOR"
SECRET_KEY = "sf060/emulator+secret/NotARealAwsKey000"
SESSION_TOKEN = "sf060-emulator-session-token"
CREDENTIAL_TOKEN = "sf060-container-authorization-token"
CREDENTIAL_PATH = "/2016-11-01/credentialprovider/"
TARGETS = {"Logs_20140328.CreateLogGroup", "Logs_20140328.CreateLogStream", "Logs_20140328.PutLogEvents"}
REQUIRED_SIGNED = {"content-type", "host", "x-amz-date", "x-amz-security-token", "x-amz-target"}
GROUP_NAME = re.compile(r"^[\.\-_/#A-Za-z0-9]{1,512}$")
GROUP_SHAPE = re.compile(r"^/aws/greengrass/(UserComponent|GreengrassSystemComponent)/([a-z0-9-]+)/([A-Za-z0-9._-]{1,128})$")
STREAM_SHAPE = re.compile(r"^/(\d{4})/(\d{2})/(\d{2})/thing/([A-Za-z0-9_+-]{1,128})$")
EVENT_OVERHEAD, MAX_EVENT, MAX_EVENTS, MAX_BATCH = 26, 262144, 10000, 1048576
RESULTS = {"ok", "throttled", "server-error", "response-lost", "rejected", "too-old"}


def _sign(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signature(secret, date, region, string_to_sign):
    key = _sign(("AWS4" + secret).encode(), date)
    for part in (region, "logs", "aws4_request"):
        key = _sign(key, part)
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


class Emulator:
    def __init__(self):
        self.lock = threading.Lock()
        self.server = None
        self.reset()

    def reset(self, strict=False):
        with self.lock:
            self.strict = strict
            self.results = []
            self.calls = []
            self.violations = []
            self.groups = set()
            self.streams = {}
            self.credential_fetches = 0
            self.down = False

    # --- lifecycle -------------------------------------------------------
    def start(self, port=0):
        emulator = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                emulator.get(self)

            def do_POST(self):
                emulator.post(self)

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # A client killed mid-request (an adapter SIGKILL test) is expected.
                if not isinstance(sys.exc_info()[1], ConnectionError):
                    super().handle_error(request, client_address)

        self.server = Server(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def port(self):
        return self.server.server_port

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    @property
    def credentials_uri(self):
        return f"http://127.0.0.1:{self.port}{CREDENTIAL_PATH}"

    def queue(self, results):
        assert all(r in RESULTS for r in results), results
        with self.lock:
            self.results.extend(results)

    def state(self):
        with self.lock:
            return {"calls": list(self.calls), "violations": list(self.violations),
                    "streams": {f"{g}|{s}": list(e) for (g, s), e in self.streams.items()},
                    "credential_fetches": self.credential_fetches, "pending_results": list(self.results)}

    def stored(self, group, stream=None):
        """Events stored for a group (all its streams, by stream name order)."""
        with self.lock:
            return [e for (g, s), events in sorted(self.streams.items()) if g == group and (stream is None or s == stream) for e in events]

    # --- HTTP ------------------------------------------------------------
    @staticmethod
    def respond(handler, status, body, headers=()):
        data = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/x-amz-json-1.1")
        handler.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(data)

    def get(self, handler):
        if handler.path != CREDENTIAL_PATH:
            return self.respond(handler, 404, {"message": "not found"})
        if handler.headers.get("Authorization") != CREDENTIAL_TOKEN:
            return self.respond(handler, 401, {"message": "unauthorized"})
        with self.lock:
            self.credential_fetches += 1
        expiration = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.respond(handler, 200, {"AccessKeyId": ACCESS_KEY, "SecretAccessKey": SECRET_KEY, "Token": SESSION_TOKEN, "Expiration": expiration})

    def control(self, handler, body):
        action = handler.path.removeprefix("/_control/")
        if action == "reset":
            self.reset(strict=bool(body.get("strict")))
        elif action == "results":
            self.queue(body["results"])
        elif action == "down":
            with self.lock:
                self.down = bool(body.get("down"))
        elif action != "state":
            return self.respond(handler, 404, {})
        self.respond(handler, 200, self.state())

    def violation(self, handler, message):
        with self.lock:
            self.violations.append(message)
        self.respond(handler, 400, {"__type": "InvalidParameterException", "message": message})

    def post(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        if handler.path.startswith("/_control/"):
            return self.control(handler, json.loads(raw or b"{}"))
        if handler.path != "/":
            return self.violation(handler, f"path {handler.path!r}")
        problem = self.check_signature(handler, raw)
        if problem:
            return self.violation(handler, problem)
        target = handler.headers.get("X-Amz-Target")
        if target not in TARGETS:
            return self.violation(handler, f"target {target!r}")
        try:
            body = json.loads(raw)
        except ValueError:
            return self.violation(handler, "body is not JSON")
        group = body.get("logGroupName") if isinstance(body, dict) else None
        if not isinstance(group, str) or not GROUP_NAME.match(group) or not GROUP_SHAPE.match(group):
            return self.violation(handler, f"log group {group!r}")
        scope_region = handler.headers["Authorization"].split("Credential=")[1].split("/")[2]
        if GROUP_SHAPE.match(group).group(2) != scope_region:
            return self.violation(handler, "signing region differs from the group's region")
        with self.lock:
            down = self.down
        if down:
            return self.respond(handler, 503, {"__type": "ServiceUnavailableException"})
        if target.endswith("CreateLogGroup"):
            if set(body) != {"logGroupName"}:
                return self.violation(handler, f"CreateLogGroup fields {sorted(body)}")
            with self.lock:
                exists = group in self.groups
                self.groups.add(group)
            if exists:
                return self.respond(handler, 400, {"__type": "ResourceAlreadyExistsException"})
            return self.respond(handler, 200, {})
        stream = body.get("logStreamName")
        if not isinstance(stream, str) or not STREAM_SHAPE.match(stream):
            return self.violation(handler, f"log stream {stream!r}")
        if target.endswith("CreateLogStream"):
            if set(body) != {"logGroupName", "logStreamName"}:
                return self.violation(handler, f"CreateLogStream fields {sorted(body)}")
            with self.lock:
                if group not in self.groups:
                    return self.respond(handler, 400, {"__type": "ResourceNotFoundException"})
                exists = (group, stream) in self.streams
                self.streams.setdefault((group, stream), [])
            if exists:
                return self.respond(handler, 400, {"__type": "ResourceAlreadyExistsException"})
            return self.respond(handler, 200, {})
        problem = self.check_put(body, stream)
        if problem:
            return self.violation(handler, problem)
        with self.lock:
            if (group, stream) not in self.streams:
                return self.respond(handler, 400, {"__type": "ResourceNotFoundException"})
            if self.results:
                result = self.results.pop(0)
            elif self.strict:
                self.violations.append("PutLogEvents without a queued result")
                result = "server-error"
            else:
                result = "ok"
            events = [[e["timestamp"], e["message"]] for e in body["logEvents"]]
            self.calls.append({"group": group, "stream": stream, "events": events, "result": result})
            if result in ("ok", "response-lost"):
                self.streams[(group, stream)].extend(events)
            elif result == "too-old":
                self.streams[(group, stream)].extend(events[1:])
        if result == "ok":
            return self.respond(handler, 200, {"nextSequenceToken": "0"})
        if result == "too-old":
            return self.respond(handler, 200, {"rejectedLogEventsInfo": {"tooOldLogEventEndIndex": 0}})
        if result == "throttled":
            return self.respond(handler, 400, {"__type": "ThrottlingException", "message": "Rate exceeded"})
        if result == "server-error":
            return self.respond(handler, 500, {"__type": "ServiceUnavailableException"})
        if result == "rejected":
            return self.respond(handler, 400, {"__type": "InvalidParameterException", "message": "injected"})
        # response-lost: accepted, but the answer never arrives.
        handler.close_connection = True
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def check_signature(self, handler, raw):
        headers = {k.lower(): v.strip() for k, v in handler.headers.items()}
        auth = headers.get("authorization", "")
        match = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([a-z0-9-]+)/logs/aws4_request, "
                             r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})", auth)
        if not match:
            return "malformed Authorization"
        access_key, date, region, signed, given = match.groups()
        if access_key != ACCESS_KEY:
            return "unknown access key"
        if headers.get("x-amz-security-token") != SESSION_TOKEN:
            return "missing or wrong session token"
        if headers.get("content-type") != "application/x-amz-json-1.1":
            return f"content type {headers.get('content-type')!r}"
        amz_date = headers.get("x-amz-date", "")
        try:
            at = datetime.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return "malformed x-amz-date"
        if abs(at.timestamp() - time.time()) > 900 or amz_date[:8] != date:
            return "request time outside 15 minutes or scope date mismatch"
        names = signed.split(";")
        if names != sorted(names) or not REQUIRED_SIGNED <= set(names):
            return f"signed headers {signed}"
        if any(name not in headers for name in names):
            return "a signed header is missing"
        canonical = "\n".join(["POST", "/", "", *[f"{n}:{headers[n]}" for n in names], "", signed, hashlib.sha256(raw).hexdigest()])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, f"{date}/{region}/logs/aws4_request", hashlib.sha256(canonical.encode()).hexdigest()])
        if not hmac.compare_digest(signature(SECRET_KEY, date, region, to_sign), given):
            return "signature mismatch"
        return None

    @staticmethod
    def check_put(body, stream):
        if set(body) != {"logGroupName", "logStreamName", "logEvents"}:
            return f"PutLogEvents fields {sorted(body)}"
        events = body["logEvents"]
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
            return "PutLogEvents needs 1..10000 events"
        total, previous, days = 0, None, set()
        for event in events:
            if not isinstance(event, dict) or set(event) != {"timestamp", "message"}:
                return "event fields"
            ts, message = event["timestamp"], event["message"]
            if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0 or not isinstance(message, str) or not message:
                return "event timestamp or message"
            size = len(message.encode()) + EVENT_OVERHEAD
            if size > MAX_EVENT:
                return f"event of {size} bytes"
            total += size
            if previous is not None and ts < previous:
                return "events out of time order"
            previous = ts
            days.add(datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).strftime("%Y/%m/%d"))
        if total > MAX_BATCH:
            return f"batch of {total} bytes"
        if len(days) != 1 or events[-1]["timestamp"] - events[0]["timestamp"] > 86400000:
            return "batch spans more than one UTC day"
        y, m, d, _ = STREAM_SHAPE.match(stream).groups()
        if f"{y}/{m}/{d}" not in days:
            return "stream day differs from the events' day"
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", required=True)
    parser.parse_args()
    emulator = Emulator()
    emulator.start()
    print(json.dumps({"port": emulator.port, "credentials_uri": emulator.credentials_uri, "token": CREDENTIAL_TOKEN}), flush=True)
    sys.stdin.read()  # until the test closes stdin (or exits)
    emulator.stop()


if __name__ == "__main__":
    main()
