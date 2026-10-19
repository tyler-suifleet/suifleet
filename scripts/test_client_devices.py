#!/usr/bin/env python3
"""LAN client devices (SF-035) with the real daemon, a real Eclipse Mosquitto
2.0.20 broker and two different TLS clients, on an isolated Docker network
with no Internet access, against the local chain simulator: no keys, gas,
public network or hardware.

Topology: the runner container (the daemon, the chain simulator and a control
API) sits on an internal Docker network; Mosquitto shares its network namespace
and listens on loopback only, with password authentication. LAN clients are
this host (Python's ssl module with raw MQTT 3.1.1) and separate containers
on the network (Mosquitto's mosquitto_sub with OpenSSL; a Python UDP probe).
Client keys and CSRs come from the openssl CLI (P-256 and Ed25519).

  enrollment     -> operator IPC issues certificates from CSRs; components cannot
  LAN discovery  -> UDP broadcast hint; no reply to short or foreign probes
  discovery      -> HTTPS with mutual TLS; server certificate valid for the
                    detected address and configured name; document signed by
                    the device key (independently verified)
  MQTT           -> client to client and client to/from components (SF-032
                    bridge) through the authorizing gateway
  denial         -> no/untrusted certificate, foreign client ID, MQTT 5,
                    unauthorized subscribe (SUBACK 0x80) and publish (session
                    ends, nothing delivered), no direct broker access
  revocation     -> live session ended, reconnect and HTTPS refused
  rotation       -> new certificate over HTTPS; old one usable for the grace
                    period only; its live session then ends
  restart        -> CA, registry, rotation and revocation persist
  offline expiry -> with the chain unavailable, the gateway keeps serving;
                    a certificate's live session ends at expiry and it cannot
                    reconnect
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BROKER_IMAGE = "eclipse-mosquitto:2.0.20@sha256:21421af7b32bf9ce508e9090c8eb13bb81f410ca778dc205506180a6f862d0eb"
NETWORK, BROKER, RUNNER = "suifleet-cd-net", "suifleet-cd-broker", "suifleet-cd-runner"
MQTT_PORT, HTTPS_PORT, UDP_PORT, CONTROL_PORT = 8883, 8443, 8442, 18898
GRACE_SECONDS, SHORT_SECONDS = 5, 60
PUB_UID, SUB_UID = 63101, 63102
GROUPS = {
    "formatVersion": "2021-03-05",
    "definitions": {
        "sensors": {"selectionRule": "thingName: sensor-*", "policyName": "sensor"},
        "controllers": {"selectionRule": "thingName: ctl-*", "policyName": "controller"},
    },
    "policies": {
        "sensor": {
            "connect": {"operations": ["mqtt:connect"], "resources": ["mqtt:clientId:${iot:Connection.Thing.ThingName}"]},
            "publish": {"operations": ["mqtt:publish"],
                        "resources": ["mqtt:topic:sensors/${iot:Connection.Thing.ThingName}/*", "mqtt:topic:lan/commands/*"]},
            "subscribe": {"operations": ["mqtt:subscribe"], "resources": ["mqtt:topicfilter:device1/sensors/#"]},
        },
        "controller": {
            "all": {"operations": ["mqtt:connect", "mqtt:subscribe"],
                    "resources": ["mqtt:clientId:${iot:Connection.Thing.ThingName}", "mqtt:topicfilter:sensors/#"]},
        },
    },
}


def sh(*command, check=True, input=None):
    result = subprocess.run(command, text=True, capture_output=True, input=input)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)}: {result.stdout}{result.stderr}")
    return result.stdout.strip()


# --- Runner (inside the root container) -----------------------------------------

def serve():
    sys.path.insert(0, "/workspace/scripts")
    sys.path.insert(0, "/workspace/sdk/python")
    import test_ota
    from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc
    assert os.geteuid() == 0
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

    class Chain(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    chain = ThreadingHTTPServer(("127.0.0.1", 0), Chain)
    threading.Thread(target=chain.serve_forever, daemon=True).start()
    test_ota.SHIM = Path("/workspace/suifleet-edge/target/debug/examples/grpc_sim_shim")
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{chain.server_port}")
    root = Path(tempfile.mkdtemp(prefix="suifleet-cd-"))
    root.chmod(0o755)
    key = root / "device.key"
    key.write_text("07" * 32)
    key.chmod(0o600)
    url = f"http://127.0.0.1:{chain.server_port}"
    manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
        rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})
    (root / "deployment.json").write_text(json.dumps(manifest))
    (root / "groups.json").write_text(json.dumps(GROUPS))
    for name in ("gateway", "bridge"):
        secret = root / f"{name}-password"
        secret.write_text(Path(f"/work/{name}-password").read_text())
        secret.chmod(0o600)
    run_dir = root / "run"
    run_dir.mkdir(mode=0o755)
    ipc_socket = run_dir / "ipc.sock"
    (root / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
        f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
        f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
        f'[runtime]\nstate_dir = "{root / "state"}"\npoll_seconds = 1\n'
        f'[ipc]\nsocket = "{ipc_socket}"\nadministrators = [0]\n'
        f'[ipc.components.pub]\nuid = {PUB_UID}\noperations = ["publish"]\n'
        '[[ipc.components.pub.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\nresources = ["sensors/*"]\n'
        f'[ipc.components.sub]\nuid = {SUB_UID}\noperations = ["subscribe", "receive", "ack", "unsubscribe"]\n'
        '[[ipc.components.sub.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["lan/commands/#"]\n'
        '[pubsub]\n'
        f'[pubsub.bridge]\nbroker = "127.0.0.1:1883"\nusername = "bridge"\npassword_file = "{root / "bridge-password"}"\nkeep_alive_seconds = 10\n'
        '[[pubsub.bridge.mappings]]\ntopic = "sensors/#"\nsource = "Pubsub"\ntarget = "LocalMqtt"\ntarget_topic_prefix = "device1/"\n'
        '[[pubsub.bridge.mappings]]\ntopic = "lan/commands/#"\nsource = "LocalMqtt"\ntarget = "Pubsub"\n'
        '[client_devices]\ndevice_groups_file = "groups.json"\nbroker = "127.0.0.1:1883"\n'
        f'broker_username = "gateway"\nbroker_password_file = "{root / "gateway-password"}"\n'
        f'mqtt_listen = "0.0.0.0:{MQTT_PORT}"\nhttps_listen = "0.0.0.0:{HTTPS_PORT}"\nudp_discovery = "0.0.0.0:{UDP_PORT}"\n'
        f'rotation_grace_seconds = {GRACE_SECONDS}\ncertificate_validity_seconds = 3600\nhostnames = ["gateway.test"]\n')
    daemon = {"process": None}
    log = (root / "daemon.log").open("a")

    def start_daemon():
        daemon["process"] = subprocess.Popen(["/workspace/suifleet-edge/target/debug/suifleet"],
            env={**os.environ, "EDGE_CONFIG": str(root / "config.toml")}, stdout=log, stderr=log)
        deadline = time.time() + 30
        while not ipc_socket.exists() and time.time() < deadline:
            assert daemon["process"].poll() is None, "the daemon exited during startup"
            time.sleep(0.1)
        assert ipc_socket.exists(), "IPC socket did not appear"

    def stop_daemon():
        process = daemon["process"]
        process.terminate()
        process.wait(timeout=30)
        if ipc_socket.exists():
            ipc_socket.unlink()

    def ipc(uid, operation, component="", topic="", payload=None):
        code = ("import os,sys,json; os.setgid(int(sys.argv[1])); os.setuid(int(sys.argv[1])); "
                "sys.path.insert(0,'/workspace/sdk/python'); from suifleet_ipc import Client; "
                "c=Client(sys.argv[2], component=sys.argv[3]); "
                "print(json.dumps(c.request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))))")
        result = subprocess.run([sys.executable, "-c", code, str(uid), str(ipc_socket), component, operation, topic, json.dumps(payload)],
                                text=True, capture_output=True)
        return {"ok": result.returncode == 0, "data": json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else None,
                "error": result.stderr.strip()[-300:]}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                if self.path == "/ipc":
                    result = ipc(body["uid"], body["operation"], body.get("component", ""), body.get("topic", ""), body.get("payload"))
                elif self.path == "/restart":
                    stop_daemon()
                    start_daemon()
                    result = {"ok": True}
                elif self.path == "/chain-stop":
                    shim.terminate()
                    shim.wait(timeout=10)
                    chain.shutdown()
                    chain.server_close()
                    result = {"ok": True}
                elif self.path == "/address":
                    result = {"ok": True, "data": sh("/workspace/suifleet-edge/target/debug/suifleet", "--derive-address", str(key))}
                elif self.path == "/log":
                    result = {"ok": True, "data": (root / "daemon.log").read_text()[-6000:]}
                else:
                    result = {"ok": False, "error": "unknown"}
            except Exception as error:
                result = {"ok": False, "error": repr(error)}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    for uid, name in ((PUB_UID, "cdpub"), (SUB_UID, "cdsub")):
        subprocess.run(["useradd", "-u", str(uid), "-M", "-s", "/usr/sbin/nologin", name], check=False, capture_output=True)
    start_daemon()
    print("READY", flush=True)
    ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Control).serve_forever()


# --- Raw MQTT 3.1.1 over TLS (the host's client) ----------------------------------

def mqtt_string(text):
    data = text.encode() if isinstance(text, str) else text
    return struct.pack(">H", len(data)) + data


def mqtt_packet(first, body):
    length, encoded = len(body), b""
    while True:
        byte, length = length % 128, length // 128
        encoded += bytes([byte | (0x80 if length else 0)])
        if not length:
            return bytes([first]) + encoded + body


def mqtt_connect(client_id, level=4):
    return mqtt_packet(0x10, mqtt_string("MQTT") + bytes([level, 0x02]) + struct.pack(">H", 30) + mqtt_string(client_id))


def mqtt_publish(topic, payload, packet_id=None):
    qos = 1 if packet_id else 0
    return mqtt_packet(0x30 | (qos << 1), mqtt_string(topic) + (struct.pack(">H", packet_id) if packet_id else b"") + payload)


def mqtt_subscribe(packet_id, filters):
    return mqtt_packet(0x82, struct.pack(">H", packet_id) + b"".join(mqtt_string(f) + bytes([q]) for f, q in filters))


class Client:
    """One TLS connection: raw packets in and out; None once closed."""

    def __init__(self, ip, identity, port=MQTT_PORT, server_hostname=None, ca=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(str(ca))
        if identity:
            context.load_cert_chain(str(identity[0]), str(identity[1]))
        raw = socket.create_connection((ip, port), timeout=10)
        self.sock = context.wrap_socket(raw, server_hostname=server_hostname or ip)
        self.buffer = b""

    def send(self, data):
        try:
            self.sock.sendall(data)
            return True
        except (OSError, ssl.SSLError):
            return False

    def packet(self, timeout=10):
        """(type, flags, body), or None when the connection ended."""
        self.sock.settimeout(timeout)
        while True:
            if len(self.buffer) >= 2:
                length, multiplier, index = 0, 1, 1
                while index < len(self.buffer):
                    byte = self.buffer[index]
                    length += (byte & 0x7f) * multiplier
                    multiplier *= 128
                    index += 1
                    if not byte & 0x80:
                        break
                else:
                    index = None
                if index and len(self.buffer) >= index + length:
                    first, body = self.buffer[0], self.buffer[index:index + length]
                    self.buffer = self.buffer[index + length:]
                    return first >> 4, first & 0x0f, body
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise
            except (OSError, ssl.SSLError):
                return None
            if not chunk:
                return None
            self.buffer += chunk

    def closed_within(self, seconds):
        """Seconds until the server ended the connection, or None."""
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            try:
                if self.packet(timeout=max(0.1, seconds - (time.monotonic() - start))) is None:
                    return time.monotonic() - start
            except socket.timeout:
                return None
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def mqtt_session(ip, ca, identity, client_id):
    """A connected session (CONNACK 0) or the CONNACK code / None."""
    try:
        client = Client(ip, identity, ca=ca)
    except (OSError, ssl.SSLError):
        return None
    if not client.send(mqtt_connect(client_id)):
        return None
    try:
        reply = client.packet()
    except socket.timeout:
        return None
    if reply is None:
        return None
    assert reply[0] == 2, reply
    if reply[2][1] != 0:
        client.close()
        return reply[2][1]
    return client


# --- Host ------------------------------------------------------------------------

class Harness:
    def __init__(self, work, ip):
        self.work, self.ip = work, ip
        self.certs = work / "certs"
        self.ca = self.certs / "ca.pem"

    def control(self, path, **body):
        import urllib.request
        request = urllib.request.Request(f"http://{self.ip}:{CONTROL_PORT}{path}", data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())

    def ipc(self, uid, operation, component="", topic="", payload=None):
        return self.control("/ipc", uid=uid, operation=operation, component=component, topic=topic, payload=payload)

    def status(self):
        return self.ipc(0, "client_devices", payload={"action": "list"})["data"]

    def keypair(self, name, kind="ec"):
        key, csr = self.certs / f"{name}.key", self.certs / f"{name}.csr"
        algorithm = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"] if kind == "ec" else ["-newkey", "ed25519"]
        sh("openssl", "req", "-new", *algorithm, "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}")
        return key, csr.read_text()

    def enroll(self, thing, kind="ec", validity=None, uid=0):
        attempt, csr = self.keypair(f"{thing}.attempt", kind)
        payload = {"action": "enroll", "thing": thing, "csr": csr}
        if validity:
            payload["validity_seconds"] = validity
        result = self.ipc(uid, "client_devices", payload=payload)
        if not result["ok"]:
            return result
        key, certificate = self.certs / f"{thing}.key", self.certs / f"{thing}.crt"
        attempt.rename(key)
        certificate.write_text(result["data"]["certificate"])
        self.ca.write_text(result["data"]["ca"])
        return {"ok": True, "identity": (certificate, key), **result["data"]}

    def https(self, identity, method, path, body="", server_hostname=None):
        client = Client(self.ip, identity, port=HTTPS_PORT, server_hostname=server_hostname, ca=self.ca)
        client.sock.sendall(f"{method} {path} HTTP/1.1\r\nHost: gateway\r\nContent-Length: {len(body.encode())}\r\n\r\n{body}".encode())
        response = b""
        client.sock.settimeout(10)
        while True:
            try:
                chunk = client.sock.recv(65536)
            except (OSError, ssl.SSLError):
                break
            if not chunk:
                break
            response += chunk
        client.close()
        head, _, payload = response.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]) if head else None, json.loads(payload) if payload else None

    def session(self, thing, identity=None):
        return mqtt_session(self.ip, self.ca, identity or (self.certs / f"{thing}.crt", self.certs / f"{thing}.key"), thing)

    def mosquitto_sub(self, thing, topic, count, timeout):
        """mosquitto_sub (libmosquitto/OpenSSL) in its own container on the network."""
        # -t: a terminal keeps mosquitto_sub's output line buffered.
        process = subprocess.Popen(["docker", "run", "--rm", "-t", "--network", NETWORK, "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{self.certs}:/certs:ro", BROKER_IMAGE, "mosquitto_sub", "-h", self.ip, "-p", str(MQTT_PORT),
            "--cafile", "/certs/ca.pem", "--cert", f"/certs/{thing}.crt", "--key", f"/certs/{thing}.key", "-i", thing,
            "-q", "1", "-t", topic, "-C", str(count), "-W", str(timeout), "-v", "-d"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines, subscribed = [], threading.Event()

        def read():
            for line in process.stdout:
                lines.append(line.rstrip("\r\n"))
                if "received SUBACK" in line:
                    subscribed.set()
        threading.Thread(target=read, daemon=True).start()
        return process, lines, subscribed


def messages(lines):
    """mosquitto_sub -v message lines (topic payload), without its debug output."""
    return [line for line in lines if line.startswith("sensors/")]


def wait(condition, message, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(message)


def verify_discovery(envelope, expected_address, ca_sha256):
    """Independently verify the CA binding a discovery answer carries: a Sui
    personal-message Ed25519 signature (flag 0x00) by the device key over the
    exact statement bytes, naming this CA. The document itself arrived over
    mutual TLS with a server certificate of that CA."""
    binding = envelope["ca_binding"]
    document = base64.b64decode(binding["message"])
    signature = base64.b64decode(binding["signature"])
    assert signature[0] == 0 and len(signature) == 97, "Ed25519 Sui signature"
    raw_signature, public_key = signature[1:65], signature[65:]
    address = "0x" + hashlib.blake2b(b"\x00" + public_key, digest_size=32).hexdigest()
    assert address == expected_address == binding["signer"], (address, expected_address, binding["signer"])
    length, encoded = len(document), b""
    while True:  # BCS vector<u8>: ULEB128 length, then bytes
        byte, length = length & 0x7f, length >> 7
        encoded += bytes([byte | (0x80 if length else 0)])
        if not length:
            break
    digest = hashlib.blake2b(bytes([3, 0, 0]) + encoded + document, digest_size=32).digest()
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        spki = bytes.fromhex("302a300506032b6570032100") + public_key
        (scratch / "pub.pem").write_text("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(spki).decode() + "\n-----END PUBLIC KEY-----\n")
        (scratch / "digest").write_bytes(digest)
        (scratch / "sig").write_bytes(raw_signature)
        out = sh("openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(scratch / "pub.pem"), "-rawin",
                 "-in", str(scratch / "digest"), "-sigfile", str(scratch / "sig"))
        assert "Signature Verified Successfully" in out, out
    statement = json.loads(document)
    assert statement["kind"] == "client-device-ca" and statement["ca_sha256"] == ca_sha256, statement
    assert statement["signer"] == expected_address, statement
    assert statement["device_id"] == envelope["document"]["gateway"]["device_object_id"], statement
    return envelope["document"]


def host(args):
    for binary in ("suifleet", "examples/grpc_sim_shim"):
        assert (ROOT / "suifleet-edge/target/debug" / binary).exists(), f"build {binary} first"
    work = Path(tempfile.mkdtemp(prefix="suifleet-cd-host-"))
    work.chmod(0o755)
    (work / "certs").mkdir(mode=0o700)
    for name in ("gateway", "bridge"):
        (work / f"{name}-password").write_text(secrets.token_hex(16))
        (work / f"{name}-password").chmod(0o644)  # read by root in the runner; the work dir is private to the test
    (work / "mosquitto.conf").write_text("listener 1883 127.0.0.1\nallow_anonymous false\npassword_file /work/passwd\n"
                                         "persistence false\n")
    for name in (BROKER, RUNNER):
        sh("docker", "rm", "-f", name, check=False)
    sh("docker", "network", "rm", NETWORK, check=False)
    checks, h = [], None

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    try:
        sh("docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{work}:/work", BROKER_IMAGE, "sh", "-c",
           f"mosquitto_passwd -b -c /work/passwd gateway $(cat /work/gateway-password) && "
           f"mosquitto_passwd -b /work/passwd bridge $(cat /work/bridge-password)")
        # --internal: no route off the network, so nothing reaches the Internet.
        sh("docker", "network", "create", "--internal", NETWORK)
        sh("docker", "run", "-d", "--name", RUNNER, "--network", NETWORK, "--user", "0", "-v", f"{ROOT}:/workspace:ro",
           "-v", f"{work}:/work:ro", "-e", "PYTHONDONTWRITEBYTECODE=1", args.image,
           "python3", "/workspace/scripts/test_client_devices.py", "--serve")
        sh("docker", "run", "-d", "--name", BROKER, "--network", f"container:{RUNNER}", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{work}:/work:ro", "-v", f"{work}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro", BROKER_IMAGE)
        ip = sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", RUNNER)
        wait(lambda: "READY" in sh("docker", "logs", RUNNER, check=False), "runner did not start", 90)
        h = Harness(work, ip)
        wait(lambda: (h.ipc(0, "messaging")["data"] or {}).get("bridge", {}).get("connected"), "the bridge did not connect")
        broker_version = sh("docker", "exec", BROKER, "mosquitto", "-h", check=False).splitlines()[0:1] or ["mosquitto"]
        no_internet = subprocess.run(["docker", "exec", RUNNER, "python3", "-c",
            "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"], capture_output=True).returncode != 0
        assert no_internet, "the runner network must have no Internet route"

        # Enrollment: operator IPC only, from CSRs (P-256 and Ed25519).
        short_enrolled = time.time()
        short = h.enroll("sensor-short", validity=SHORT_SECONDS)
        assert short["ok"], short
        sensor = h.enroll("sensor-1")
        controller = h.enroll("ctl-1", kind="ed25519")
        victim = h.enroll("sensor-2")
        assert sensor["ok"] and controller["ok"] and victim["ok"], (sensor, controller, victim)
        assert not h.enroll("sensor-9", uid=SUB_UID)["ok"], "a component enrolled a client device"
        assert not h.enroll("sensor-1")["ok"], "re-enrolled an active thing"
        assert not h.enroll("suifleet-bridge")["ok"], "enrolled the bridge's client ID"
        status = h.status()
        assert set(status["things"]) == {"sensor-short", "sensor-1", "ctl-1", "sensor-2"}, status
        ca_der = ssl.PEM_cert_to_DER_cert(h.ca.read_text())
        ca_sha256 = hashlib.sha256(ca_der).hexdigest()
        assert status["ca_sha256"] == ca_sha256
        check("enrollment: operator IPC issued P-256 and Ed25519 client certificates from CSRs; components, "
              "duplicate and reserved (bridge client ID) enrollments refused")

        # LAN discovery: a UDP broadcast from another container on the network
        # (the subnet's directed broadcast: an internal network has no default route).
        import ipaddress
        subnet = sh("docker", "network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", NETWORK)
        broadcast = str(ipaddress.ip_network(subnet).broadcast_address)
        probe = ("import json,socket,sys\n"
                 "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); s.settimeout(3)\n"
                 f"s.sendto(b'SUIFLEET-DISCOVER/1'+b'\\0'*80,('{broadcast}',{UDP_PORT}))\n"
                 "try:\n s.recvfrom(4096); print('SHORT-REPLIED')\nexcept socket.timeout: pass\n"
                 f"s.sendto(b'SUIFLEET-DISCOVER/1'+b'\\0'*1200,('{broadcast}',{UDP_PORT}))\n"
                 "data,peer=s.recvfrom(4096); print(json.dumps({'peer':peer[0],'hint':json.loads(data)}))\n")
        out = sh("docker", "run", "--rm", "--network", NETWORK, "--entrypoint", "python3", args.image, "-c", probe)
        assert "SHORT-REPLIED" not in out, out
        found = json.loads(out.splitlines()[-1])
        assert found["peer"] == ip and found["hint"]["ca_sha256"] == ca_sha256 and found["hint"]["authenticated"] is False, found
        assert f"{ip}:{MQTT_PORT}" in found["hint"]["mqtt"] and f"gateway.test:{MQTT_PORT}" in found["hint"]["mqtt"], found
        check(f"LAN discovery: a UDP broadcast to {broadcast} found the gateway at {ip} with its CA fingerprint and endpoints; "
              "a short (amplifying) probe got no reply")

        # Authenticated discovery: mutual TLS to the detected address and the
        # configured name; the document is signed by the device key.
        address = h.control("/address")["data"]
        code, envelope = h.https(sensor["identity"], "GET", "/discover")
        assert code == 200, (code, envelope)
        document = verify_discovery(envelope, address, ca_sha256)
        assert document["thing"] == "sensor-1" and document["ca"]["sha256"] == ca_sha256, document
        assert document["gateway"]["address"] == address and document["mqtt"] == found["hint"]["mqtt"], document
        assert document["certificate"]["sha256"] == sensor["sha256"], document
        assert h.https(sensor["identity"], "GET", "/discover", server_hostname="gateway.test")[0] == 200
        try:
            h.https(sensor["identity"], "GET", "/discover", server_hostname="other.test")
            raise AssertionError("the server certificate was accepted for another name")
        except ssl.SSLCertVerificationError:
            pass
        check("discovery: HTTPS with mutual TLS; the gateway certificate verifies for the detected address and "
              "gateway.test only; the document names the CA, endpoints and the client's certificate, and carries the "
              "device key's signed binding of that CA (Ed25519 Sui personal message, verified independently with openssl)")

        # Client to client through the gateway: mosquitto_sub (OpenSSL, Ed25519) and Python (P-256).
        process, lines, subscribed = h.mosquitto_sub("ctl-1", "sensors/#", 1, 30)
        assert subscribed.wait(30), lines
        client = h.session("sensor-1")
        assert isinstance(client, Client), client
        assert client.send(mqtt_publish("sensors/sensor-1/temp", b'{"t":21}', packet_id=1))
        assert client.packet()[0] == 4, "PUBACK"
        process.wait(timeout=30)
        assert messages(lines) == ['sensors/sensor-1/temp {"t":21}'], lines
        check("MQTT between two LAN clients through the gateway: Python ssl (P-256) published at QoS 1, "
              "mosquitto_sub (libmosquitto/OpenSSL, Ed25519) received it")

        # LAN clients and components through the SF-032 bridge, both ways.
        assert h.ipc(SUB_UID, "subscribe", "sub", "lan/commands/#", {"qos": 1})["ok"]
        assert client.send(mqtt_subscribe(2, [("device1/sensors/#", 1)]))
        assert client.packet()[:3:2] == (9, b"\x00\x02\x01"), "SUBACK granted QoS 1"
        assert client.send(mqtt_publish("lan/commands/reboot", b'{"cmd":"reboot"}', packet_id=3))
        assert client.packet()[0] == 4
        wait(lambda: h.ipc(SUB_UID, "receive", "sub", "lan/commands/#")["data"], "the LAN command did not reach the component")
        message = h.ipc(SUB_UID, "receive", "sub", "lan/commands/#")["data"]
        assert message["payload"] == {"cmd": "reboot"}, message
        assert h.ipc(SUB_UID, "ack", "sub", "lan/commands/#", message["id"])["data"]["acknowledged"]
        assert h.ipc(PUB_UID, "publish", "pub", "sensors/a/temp", {"t": 7})["ok"]
        packet = client.packet(timeout=30)
        while packet and packet[0] != 3:
            packet = client.packet(timeout=30)
        assert packet and b"device1/sensors/a/temp" in packet[2] and packet[2].endswith(b'{"t":7}'), packet
        check("LAN client <-> component: an authorized LAN publish reached a component through the bridge; a component "
              "publication reached the LAN client's authorized subscription")

        # Denials.
        try:
            bare = Client(ip, None, ca=h.ca)
            bare.send(mqtt_connect("sensor-1"))
            assert bare.packet() is None, "no client certificate"
        except (OSError, ssl.SSLError):
            pass
        rogue = h.certs / "rogue"
        sh("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
           "-keyout", f"{rogue}.key", "-out", f"{rogue}.crt", "-subj", "/CN=sensor-1")
        assert mqtt_session(ip, h.ca, (Path(f"{rogue}.crt"), Path(f"{rogue}.key")), "sensor-1") is None, "untrusted certificate"
        assert mqtt_session(ip, h.ca, sensor["identity"], "sensor-2") == 5, "foreign client ID"
        v5 = Client(ip, sensor["identity"], ca=h.ca)
        v5.send(mqtt_connect("sensor-1", level=5))
        assert v5.packet()[2] == b"\x00\x01", "MQTT 5 refused with CONNACK 1"
        assert client.send(mqtt_subscribe(4, [("sensors/#", 1), ("device1/sensors/#", 0)]))
        assert client.packet()[2] == b"\x00\x04\x80\x00", "unauthorized filter refused, authorized granted"
        process, lines, subscribed = h.mosquitto_sub("ctl-1", "sensors/#", 1, 8)
        assert subscribed.wait(30), lines
        assert client.send(mqtt_publish("sensors/sensor-2/temp", b'{"spoof":1}', packet_id=5))
        assert client.closed_within(5) is not None, "unauthorized publish did not end the session"
        process.wait(timeout=30)
        assert messages(lines) == [], lines
        try:
            socket.create_connection((ip, 1883), timeout=3).close()
            raise AssertionError("the broker is reachable from the LAN")
        except OSError:
            pass
        denied = h.status()["denied"]
        assert denied.get("handshake", 0) >= 2 and denied.get("connect", 0) >= 1 and denied.get("protocol", 0) >= 1, denied
        assert denied.get("subscribe", 0) >= 1 and denied.get("publish", 0) >= 1, denied
        check("denial: no client certificate and an untrusted self-signed one fail the handshake; a foreign client ID "
              "gets CONNACK 5 and MQTT 5 CONNACK 1; an unauthorized filter gets SUBACK 0x80 beside a granted one; an "
              "unauthorized publish ends the session and reaches nobody; the broker is not reachable from the LAN")

        # Revocation of a live session.
        live = h.session("sensor-2")
        assert isinstance(live, Client), live
        revoked_at = time.monotonic()
        assert h.ipc(0, "client_devices", payload={"action": "revoke", "thing": "sensor-2"})["ok"]
        ended = live.closed_within(10)
        assert ended is not None, "the revoked session stayed open"
        ended = time.monotonic() - revoked_at
        assert h.session("sensor-2") is None, "a revoked identity reconnected"
        assert h.https(victim["identity"], "GET", "/discover") == (403, {"error": "revoked"})
        check(f"revocation: the live session ended {ended:.1f} s after the revoke; reconnect and HTTPS refused (403 revoked)")

        # Rotation over HTTPS with a grace period for the old certificate.
        # One client ID has one broker session at a time (MQTT takeover), so
        # the old certificate's live session is opened after the new one closes.
        old = sensor["identity"]
        new_key, new_csr = h.keypair("sensor-1-new")
        code, issued = h.https(old, "POST", "/rotate", new_csr)
        assert code == 200, (code, issued)
        rotated_at = time.monotonic()
        (h.certs / "sensor-1-new.crt").write_text(issued["certificate"])
        new = (h.certs / "sensor-1-new.crt", new_key)
        fresh = mqtt_session(ip, h.ca, new, "sensor-1")
        assert isinstance(fresh, Client), "the new certificate"
        fresh.close()
        holder = mqtt_session(ip, h.ca, old, "sensor-1")
        assert isinstance(holder, Client), "the old certificate within the grace period"
        assert holder.closed_within(GRACE_SECONDS + 5) is not None, "the old certificate's session outlived the grace period"
        retired = time.monotonic() - rotated_at
        assert retired >= GRACE_SECONDS - 1, retired
        assert mqtt_session(ip, h.ca, old, "sensor-1") is None, "the superseded certificate reconnected"
        assert h.https(old, "POST", "/rotate", h.keypair("sensor-1-again")[1]) == (403, {"error": "superseded"})
        code, envelope = h.https(new, "GET", "/discover")
        assert code == 200 and verify_discovery(envelope, address, ca_sha256)["certificate"]["sha256"] == issued["sha256"]
        check(f"rotation: a new certificate issued over HTTPS works at once; the old one connected during the "
              f"{GRACE_SECONDS} s grace, its live session ended after {retired:.1f} s, then it was refused (superseded)")

        # Daemon restart: CA, registry, rotation and revocation persist.
        h.control("/restart")
        wait(lambda: h.status() is not None, "daemon did not return")
        assert h.status()["ca_sha256"] == ca_sha256, "the CA changed across a restart"
        after = mqtt_session(ip, h.ca, new, "sensor-1")
        assert isinstance(after, Client), "the rotated certificate after restart"
        after.close()
        assert mqtt_session(ip, h.ca, old, "sensor-1") is None and h.session("sensor-2") is None
        check("restart: the CA and registry persisted; the rotated certificate connects, the superseded and the revoked "
              "ones stay refused")

        # Chain unavailable: the gateway keeps serving; expiry is enforced offline.
        h.control("/chain-stop")
        wait(lambda: "Sync failed" in h.control("/log")["data"], "the daemon did not notice the chain going away")
        holder = h.session("sensor-short")
        remaining = short_enrolled + SHORT_SECONDS - time.time()
        assert isinstance(holder, Client) and remaining > 3, (holder, remaining)
        online = mqtt_session(ip, h.ca, new, "sensor-1")
        assert isinstance(online, Client) and online.send(mqtt_publish("sensors/sensor-1/x", b"1", packet_id=9))
        assert online.packet()[0] == 4, "publishing works with the chain unavailable"
        online.close()
        ended = holder.closed_within(remaining + 10)
        assert ended is not None, "the session outlived its certificate"
        lateness = time.time() - (short_enrolled + SHORT_SECONDS)
        assert -2 <= lateness <= 6, lateness
        assert h.session("sensor-short") is None, "an expired certificate reconnected"
        assert "Sync failed" in h.control("/log")["data"]
        check(f"offline: with the chain simulator stopped (daemon sync failing) the gateway still authenticated and "
              f"authorized LAN clients; a {SHORT_SECONDS} s certificate's live session ended {lateness:+.1f} s from its "
              "expiry and it could not reconnect")

        status = h.status()
        evidence = {"ticket": "SF-035", "head": sh("git", "-C", str(ROOT), "rev-parse", "HEAD"), "broker": BROKER_IMAGE,
                    "broker_version": broker_version[0], "network": "internal Docker network, no Internet route",
                    "clients": ["Python ssl + raw MQTT 3.1.1 (host, P-256)", "mosquitto_sub 2.0.20 / OpenSSL (container, Ed25519)",
                                "Python UDP broadcast probe (container)"],
                    "checks": checks, "denied": status["denied"],
                    "fixture": {"file": "interfaces/compat/client-device-cases.json", "requests": 16,
                                "executed_by": "make edge-test: clientdevices::tests::shared_client_device_authorization_cases"}}
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
    except Exception:
        print(sh("docker", "logs", "--tail", "40", RUNNER, check=False))
        try:
            print(h.control("/log").get("data", "") if h else "")
        except Exception:
            pass
        raise
    finally:
        if not args.keep:
            for name in (BROKER, RUNNER):
                sh("docker", "rm", "-f", name, check=False)
            sh("docker", "network", "rm", NETWORK, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--image", default="suifleet-dev:sf004", help="runner image (the dev image)")
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--keep", action="store_true")
    arguments = parser.parse_args()
    serve() if arguments.serve else host(arguments)
