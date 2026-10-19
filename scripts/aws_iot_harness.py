"""AWS-shaped IoT Core endpoint for SF-061 tests: an MQTT 3.1.1 broker over
mutual TLS with the AWS IoT Core behaviour the adapter relies on, and the
Device Shadow service. Clean-room, from AWS documentation; no AWS code.

- TLS: the client certificate must chain to the registered CA and be
  registered and active (AWS closes the connection otherwise). A listener
  may require ALPN `x-amzn-mqtt-ca` (port 443).
- Policy, per thing: the client id must be the thing name; publish and
  subscribe topics are matched against globs (`*` matches anything). A
  denied SUBSCRIBE gets 0x80; a denied PUBLISH closes the connection.
- Sessions: clean session 0 keeps subscriptions and queues QoS 1 messages
  while the client is away; unacknowledged messages are resent with DUP. A
  second connection with the same client id takes the session over.
- Device Shadow: `$aws/things/<thing>/shadow[/name/<n>]/{get,update,delete}`
  with accepted/rejected answers echoing clientToken, version conflicts
  (409), 404 for absent shadows, update/delta and update/documents
  notifications, and versions that continue after deletion.
"""
import copy
import hashlib
import json
import socket
import ssl
import struct
import threading
import time


def matches(filter_, topic):
    f, t = filter_.split("/"), topic.split("/")
    if topic.startswith("$") and f[0] in ("+", "#"):
        return False
    for i, level in enumerate(f):
        if level == "#":
            return True
        if i >= len(t) or (level != "+" and level != t[i]):
            return False
    return len(f) == len(t)


def glob(pattern, text):
    parts = pattern.split("*")
    if len(parts) == 1:
        return pattern == text
    if not text.startswith(parts[0]):
        return False
    position = len(parts[0])
    for part in parts[1:-1]:
        found = text.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return text[position:].endswith(parts[-1]) and len(text) - position >= len(parts[-1])


def encode(first, body):
    out, length = bytearray([first]), len(body)
    while True:
        byte = length % 128
        length //= 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes(out) + body


def string(text):
    data = text.encode() if isinstance(text, str) else text
    return struct.pack(">H", len(data)) + data


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
            if not target[key]:
                del target[key]
        elif isinstance(value, dict):
            target[key] = {}
            merge(target[key], value)
            if not target[key]:
                del target[key]
        else:
            target[key] = copy.deepcopy(value)


def delta(desired, reported):
    out = {}
    for key, value in (desired or {}).items():
        other = (reported or {}).get(key)
        if isinstance(value, dict) and isinstance(other, dict):
            inner = delta(value, other)
            if inner:
                out[key] = inner
        elif json.dumps(value, sort_keys=True) != json.dumps(other, sort_keys=True) or type(value) is not type(other):
            out[key] = value
    return out


class Session:
    def __init__(self, client_id):
        self.client_id = client_id
        self.subscriptions = {}
        self.unacked = {}      # packet id -> (topic, payload)
        self.queued = []       # (topic, payload) waiting for a connection
        self.next_id = 1
        self.connection = None


class Connection:
    def __init__(self, broker, sock, listener):
        self.broker, self.sock, self.listener = broker, sock, listener
        self.outbox, self.lock = [], threading.Lock()
        self.closed = False
        self.session = None
        self.thing = None
        self.fingerprint = None
        self.alpn = None

    def send(self, data):
        with self.lock:
            self.outbox.append(data)

    def close(self):
        self.closed = True


class AwsIot:
    def __init__(self, ca_file, server_cert, server_key):
        self.ca_file, self.server_cert, self.server_key = ca_file, server_cert, server_key
        self.lock = threading.RLock()
        self.certificates = {}    # sha256 hex -> {"thing", "active", "name"}
        self.policies = {}        # thing -> {"publish": [...], "subscribe": [...]}
        self.sessions = {}
        self.connections = []
        self.shadows = {}         # (thing, name) -> {"state", "version", "exists"}
        self.published = []       # (client id, topic, payload, dup) from devices, non-shadow
        self.denied = []          # (client id, kind, topic)
        self.events = []          # connection events
        self.shadow_requests = [] # (thing, name, op, request)
        self.race = {}            # (thing, name) -> cloud update applied before the next device update
        self.rejections = []      # (thing, name, op, code) answered on .../rejected
        self.down = False
        self.listeners = []

    # --- registry ---------------------------------------------------------

    def register(self, name, pem_path, thing, active=True):
        der = ssl.PEM_cert_to_DER_cert(open(pem_path).read())
        fingerprint = hashlib.sha256(der).hexdigest()
        with self.lock:
            self.certificates[fingerprint] = {"thing": thing, "active": active, "name": name}
        return fingerprint

    def set_active(self, name, active):
        with self.lock:
            for cert in self.certificates.values():
                if cert["name"] == name:
                    cert["active"] = active

    def kick(self):
        """Close every connection (as an outage or revocation does)."""
        with self.lock:
            for connection in self.connections:
                connection.close()

    # --- listeners ----------------------------------------------------------

    def listen(self, port, alpn=None, server_cert=None, server_key=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_cert or self.server_cert, server_key or self.server_key)
        context.load_verify_locations(self.ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        if alpn:
            context.set_alpn_protocols([alpn])
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(16)
        self.listeners.append(server)
        threading.Thread(target=self._accept, args=(server, context, alpn, port), daemon=True).start()

    def _accept(self, server, context, alpn, port):
        while True:
            sock, _ = server.accept()
            threading.Thread(target=self._handshake, args=(sock, context, alpn, port), daemon=True).start()

    def _handshake(self, sock, context, alpn, port):
        if self.down:
            sock.close()
            return
        try:
            sock.settimeout(10)
            tls = context.wrap_socket(sock, server_side=True)
        except (ssl.SSLError, OSError) as error:
            with self.lock:
                self.events.append({"port": port, "event": "tls-refused", "reason": str(error)[:200]})
            sock.close()
            return
        selected = tls.selected_alpn_protocol()
        der = tls.getpeercert(binary_form=True)
        fingerprint = hashlib.sha256(der).hexdigest() if der else None
        with self.lock:
            cert = self.certificates.get(fingerprint)
            refusal = None
            if alpn and selected != alpn:
                refusal = "no-alpn"
            elif not cert or not cert["active"]:
                refusal = "certificate-not-active"
            if refusal:
                self.events.append({"port": port, "event": refusal, "certificate": cert and cert["name"]})
        if refusal:
            tls.close()
            return
        connection = Connection(self, tls, port)
        connection.fingerprint, connection.thing, connection.alpn = fingerprint, cert["thing"], selected
        with self.lock:
            self.connections.append(connection)
        try:
            self._serve(connection)
        finally:
            with self.lock:
                if connection in self.connections:
                    self.connections.remove(connection)
                if connection.session and connection.session.connection is connection:
                    connection.session.connection = None
            try:
                tls.close()
            except OSError:
                pass

    # --- MQTT ---------------------------------------------------------------

    def _serve(self, connection):
        sock, buffer = connection.sock, b""
        sock.settimeout(0.05)
        while not connection.closed:
            with connection.lock:
                pending, connection.outbox = connection.outbox, []
            for data in pending:
                sock.sendall(data)
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                buffer += chunk
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            except (OSError, ssl.SSLError):
                return
            while True:
                packet = self._decode(buffer)
                if not packet:
                    break
                first, body, used = packet
                buffer = buffer[used:]
                if not self._packet(connection, first, body):
                    return

    @staticmethod
    def _decode(buffer):
        if len(buffer) < 2:
            return None
        length, multiplier, index = 0, 1, 1
        while True:
            if index >= len(buffer):
                return None
            byte = buffer[index]
            length += (byte & 0x7F) * multiplier
            index += 1
            if not byte & 0x80:
                break
            multiplier *= 128
        if len(buffer) < index + length:
            return None
        return buffer[0], buffer[index:index + length], index + length

    def _packet(self, connection, first, body):
        kind = first >> 4
        if connection.session is None and kind != 1:
            return False
        if kind == 1:
            return self._connect(connection, body)
        if kind == 3:
            return self._publish_in(connection, first, body)
        if kind == 4:
            (packet_id,) = struct.unpack(">H", body[:2])
            with self.lock:
                connection.session.unacked.pop(packet_id, None)
            return True
        if kind == 8:
            return self._subscribe(connection, body)
        if kind == 12:
            connection.send(bytes([0xD0, 0]))
            return True
        return kind != 14

    def _connect(self, connection, body):
        (name_length,) = struct.unpack(">H", body[:2])
        at = 2 + name_length
        level, flags = body[at], body[at + 1]
        at += 4
        (id_length,) = struct.unpack(">H", body[at:at + 2])
        client_id = body[at + 2:at + 2 + id_length].decode()
        clean = bool(flags & 0x02)
        with self.lock:
            # AWS policy: the client id must be the certificate's thing.
            if level != 4 or client_id != connection.thing:
                self.events.append({"event": "connect-refused", "client_id": client_id, "thing": connection.thing})
                connection.send(encode(0x20, bytes([0, 5])))
                connection.sock.settimeout(1)
                with connection.lock:
                    for data in connection.outbox:
                        connection.sock.sendall(data)
                return False
            session = self.sessions.get(client_id)
            present = bool(session) and not clean
            if clean or not session:
                session = Session(client_id)
                self.sessions[client_id] = session
            if session.connection:
                session.connection.close()
            session.connection = connection
            connection.session = session
            name = self.certificates[connection.fingerprint]["name"]
            self.events.append({"event": "connected", "client_id": client_id, "certificate": name, "alpn": connection.alpn,
                                "port": connection.listener, "session_present": present})
            connection.send(encode(0x20, bytes([1 if present else 0, 0])))
            for packet_id, (topic, payload) in sorted(session.unacked.items()):
                connection.send(encode(0x3A, string(topic) + struct.pack(">H", packet_id) + payload))
            queued, session.queued = session.queued, []
            for topic, payload in queued:
                self._deliver(session, topic, payload)
        return True

    def _allowed(self, thing, action, topic):
        policy = self.policies.get(thing, {})
        return any(glob(pattern, topic) for pattern in policy.get(action, []))

    def _subscribe(self, connection, body):
        (packet_id,) = struct.unpack(">H", body[:2])
        at, codes = 2, []
        with self.lock:
            while at < len(body):
                (length,) = struct.unpack(">H", body[at:at + 2])
                filter_ = body[at + 2:at + 2 + length].decode()
                qos = body[at + 2 + length]
                at += 3 + length
                if self._allowed(connection.thing, "subscribe", filter_):
                    connection.session.subscriptions[filter_] = min(qos, 1)
                    codes.append(min(qos, 1))
                else:
                    self.denied.append((connection.session.client_id, "subscribe", filter_))
                    codes.append(0x80)
        connection.send(encode(0x90, struct.pack(">H", packet_id) + bytes(codes)))
        return True

    def _publish_in(self, connection, first, body):
        qos, dup = (first >> 1) & 3, bool(first & 8)
        (length,) = struct.unpack(">H", body[:2])
        topic = body[2:2 + length].decode()
        at = 2 + length
        packet_id = None
        if qos:
            (packet_id,) = struct.unpack(">H", body[at:at + 2])
            at += 2
        payload = body[at:]
        with self.lock:
            if not self._allowed(connection.thing, "publish", topic):
                # AWS IoT Core disconnects a client whose publication its policy denies.
                self.denied.append((connection.session.client_id, "publish", topic))
                return False
            shadow = self._shadow_topic(topic)
            if shadow:
                self._shadow(connection, *shadow, payload)
            else:
                self.published.append((connection.session.client_id, topic, payload.decode(errors="replace"), dup))
                self.route(topic, payload)
        if qos:
            connection.send(encode(0x40, struct.pack(">H", packet_id)))
        return True

    def route(self, topic, payload):
        """Deliver to every session subscribed (QoS 1)."""
        with self.lock:
            for session in self.sessions.values():
                if any(matches(f, topic) for f in session.subscriptions):
                    self._deliver(session, topic, payload)

    def _deliver(self, session, topic, payload):
        if session.connection is None or session.connection.closed:
            session.queued.append((topic, payload))
            return
        packet_id = session.next_id
        session.next_id = 1 if packet_id == 65535 else packet_id + 1
        session.unacked[packet_id] = (topic, payload)
        session.connection.send(encode(0x32, string(topic) + struct.pack(">H", packet_id) + payload))

    # --- Device Shadow --------------------------------------------------------

    @staticmethod
    def _shadow_topic(topic):
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "$aws" or parts[1] != "things" or parts[3] != "shadow":
            return None
        thing, rest = parts[2], parts[4:]
        name = ""
        if rest[:1] == ["name"] and len(rest) >= 2:
            name, rest = rest[1], rest[2:]
        if len(rest) != 1 or rest[0] not in ("get", "update", "delete"):
            return None
        return thing, name, rest[0]

    @staticmethod
    def prefix(thing, name):
        return f"$aws/things/{thing}/shadow" + (f"/name/{name}" if name else "")

    def shadow(self, thing, name):
        return self.shadows.setdefault((thing, name), {"state": {}, "version": 0, "exists": False})

    def _answer(self, thing, name, op, outcome, body):
        if outcome == "rejected":
            self.rejections.append((thing, name, op, body.get("code")))
        self.route(f"{self.prefix(thing, name)}/{op}/{outcome}", json.dumps(body).encode())

    def _shadow(self, connection, thing, name, op, payload):
        try:
            request = json.loads(payload) if payload else {}
        except ValueError:
            request = None
        now = int(time.time())
        if not isinstance(request, dict):
            return self._answer(thing, name, op, "rejected", {"code": 400, "message": "Payload contains invalid json", "timestamp": now})
        token = request.get("clientToken")
        self.shadow_requests.append((thing, name, op, request))
        reply = (lambda body: {**body, "clientToken": token}) if token is not None else (lambda body: body)
        if op == "update" and (thing, name) in self.race:
            # Another writer stores first: this request's version is stale.
            self.update(thing, name, {"state": self.race.pop((thing, name))})
        shadow = self.shadow(thing, name)
        if op == "get":
            if not shadow["exists"]:
                return self._answer(thing, name, op, "rejected", reply({"code": 404, "message": f"No shadow exists with name: '{name or thing}'"}))
            state = copy.deepcopy(shadow["state"])
            d = delta(state.get("desired"), state.get("reported"))
            if d:
                state["delta"] = d
            return self._answer(thing, name, op, "accepted", reply({"state": state, "metadata": {}, "version": shadow["version"], "timestamp": now}))
        if op == "delete":
            if not shadow["exists"]:
                return self._answer(thing, name, op, "rejected", reply({"code": 404, "message": "No shadow exists"}))
            shadow["exists"], shadow["state"] = False, {}
            return self._answer(thing, name, op, "accepted", reply({"version": shadow["version"], "timestamp": now}))
        state = request.get("state")
        if not isinstance(state, dict) or set(state) - {"desired", "reported"}:
            return self._answer(thing, name, op, "rejected", reply({"code": 400, "message": "Invalid state node"}))
        if "version" in request and (not shadow["exists"] or request["version"] != shadow["version"]):
            return self._answer(thing, name, op, "rejected", reply({"code": 409, "message": "Version conflict"}))
        version = self.update(thing, name, request)
        self._answer(thing, name, op, "accepted", reply({"state": state, "metadata": {}, "version": version, "timestamp": now}))

    def update(self, thing, name, request):
        """Apply an update (also used by cloud writers); returns the version."""
        with self.lock:
            shadow = self.shadow(thing, name)
            previous = {"state": copy.deepcopy(shadow["state"]), "version": shadow["version"]} if shadow["exists"] else None
            for section, patch in request["state"].items():
                if patch is None:
                    shadow["state"].pop(section, None)
                    continue
                shadow["state"].setdefault(section, {})
                merge(shadow["state"][section], patch)
                if not shadow["state"][section]:
                    del shadow["state"][section]
            shadow["exists"] = True
            shadow["version"] += 1
            now = int(time.time())
            prefix = self.prefix(thing, name)
            current = {"state": copy.deepcopy(shadow["state"]), "version": shadow["version"]}
            self.route(f"{prefix}/update/documents", json.dumps({"previous": previous, "current": current, "timestamp": now}).encode())
            d = delta(shadow["state"].get("desired"), shadow["state"].get("reported"))
            if d and "desired" in request["state"]:
                self.route(f"{prefix}/update/delta", json.dumps({"state": d, "version": shadow["version"], "timestamp": now}).encode())
            return shadow["version"]

    def delete(self, thing, name):
        with self.lock:
            shadow = self.shadow(thing, name)
            if shadow["exists"]:
                shadow["exists"], shadow["state"] = False, {}
                self.route(f"{self.prefix(thing, name)}/delete/accepted", json.dumps({"version": shadow["version"], "timestamp": int(time.time())}).encode())

    def snapshot(self):
        with self.lock:
            return {
                "connections": [{"client_id": c.session and c.session.client_id, "thing": c.thing, "alpn": c.alpn, "port": c.listener,
                                 "certificate": self.certificates[c.fingerprint]["name"]} for c in self.connections if not c.closed],
                "events": self.events[-60:],
                "published": [list(p) for p in self.published],
                "denied": [list(d) for d in self.denied],
                "shadows": {f"{t}/{n}": s for (t, n), s in self.shadows.items()},
                "shadow_requests": len(self.shadow_requests),
                "rejections": [list(r) for r in self.rejections],
                "shadow_updates": sum(1 for r in self.shadow_requests if r[2] == "update"),
            }
