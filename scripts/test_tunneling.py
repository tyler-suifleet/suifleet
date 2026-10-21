#!/usr/bin/env python3
"""SF-063: real packaged AWS localproxy + shipped systemd adapter, offline.
Needs Docker. A disposable privileged, network-none systemd container provides
mTLS AWS-shaped MQTT notifications and a TLS/WebSocket tunnel endpoint. No AWS
accounts, credentials, network, Sui transactions or hardware are used.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOST = "data.tunneling.iot.us-east-1.amazonaws.com"
TOPIC = "$aws/things/thing1/tunnels/notify"
UNIT = "suifleet-tunnel.service"
UID = 63363


def sh(*cmd, check=True):
    r = subprocess.run(cmd, text=True, capture_output=True)
    if check and r.returncode:
        raise AssertionError(f"command {cmd[0]} failed: {r.stdout[-2000:]}{r.stderr[-2000:]}")
    return r.stdout.strip()


def wait_for(fn, label, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("timed out: " + label)


def exact(sock, n):
    out = b""
    while len(out) < n:
        part = sock.recv(n - len(out))
        if not part:
            raise EOFError()
        out += part
    return out


def frame(sock, payload, opcode=2):
    length = len(payload)
    header = bytes([0x80 | opcode, length]) if length < 126 else bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
    sock.sendall(header + payload)


def receive(sock):
    a, b = exact(sock, 2)
    length = b & 127
    if length == 126:
        length = struct.unpack(">H", exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", exact(sock, 8))[0]
    assert length <= 131072
    mask = exact(sock, 4) if b & 128 else None
    data = exact(sock, length)
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return a & 15, data


def message(kind, service="SSH", payload=None, stream=1):
    # AWS Message.proto: type=1, streamId=2, payload=4, serviceId=5,
    # availableServiceIds=6. Deliberately small independent wire vectors.
    data = bytes([8, kind])
    if kind == 5:
        data += bytes([50, len(service)]) + service.encode()
    else:
        data += bytes([16, stream, 42, len(service)]) + service.encode()
        if payload is not None:
            assert len(payload) < 128
            data += bytes([34, len(payload)]) + payload
    return struct.pack(">H", len(data)) + data


class Tunnel:
    def __init__(self, directory):
        self.directory = directory
        self.clients, self.errors = [], []
        self.bad_certificate = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 443))
        self.listener.listen()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            raw, _ = self.listener.accept()
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cert = "rogue" if self.bad_certificate else "server"
            ctx.load_cert_chain(str(self.directory / f"{cert}.pem"), str(self.directory / f"{cert}.key"))
            raw.settimeout(10)
            try:
                sock = ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                raw.close()
                return  # Expected negative server-identity test.
            request = b""
            while not request.endswith(b"\r\n\r\n"):
                request += exact(sock, 1)
                assert len(request) < 16384
            lines = request.decode().split("\r\n")
            assert lines[0] == "GET /tunnel?local-proxy-mode=destination HTTP/1.1", lines[0]
            headers = {name.lower(): value for name, value in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            assert headers["sec-websocket-protocol"] == "aws.iot.securetunneling-3.0"
            digest = hashlib.sha256(headers["access-token"].encode()).hexdigest()
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: aws.iot.securetunneling-3.0\r\n\r\n").encode())
            client = {"digest": digest, "socket": sock, "closed": False, "data": bytearray()}
            self.clients.append(client)
            # Test controls outgoing frames; incoming frames are collected here.
            while True:
                opcode, data = receive(sock)
                if opcode == 8:
                    break
                if opcode == 9:
                    frame(sock, data, 10)
                elif opcode == 2:
                    client["data"].extend(data)
        except (EOFError, OSError):
            pass
        except Exception as error:
            self.errors.append(str(error))
        finally:
            if "client" in locals():
                client["closed"] = True
            raw.close()


def inside():
    from aws_iot_harness import AwsIot
    checks = []

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    directory = Path("/etc/suifleet-tunnel")
    directory.mkdir(mode=0o755)
    pki = directory / "pki"
    pki.mkdir()
    for ca in ("ca", "rogue-ca"):
        sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={ca}",
           "-keyout", str(pki / f"{ca}.key"), "-out", str(pki / f"{ca}.pem"), "-addext", "basicConstraints=critical,CA:TRUE")
    for name, cn, ca, usage in (("server", HOST, "ca", "serverAuth"), ("rogue", HOST, "rogue-ca", "serverAuth"), ("client", "thing1", "ca", "clientAuth")):
        sh("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={cn}",
           "-keyout", str(pki / f"{name}.key"), "-out", str(pki / f"{name}.csr"))
        ext = pki / "ext"
        ext.write_text(f"basicConstraints=CA:FALSE\nextendedKeyUsage={usage}\nsubjectAltName=DNS:{HOST},DNS:localhost\n")
        sh("openssl", "x509", "-req", "-in", str(pki / f"{name}.csr"), "-CA", str(pki / f"{ca}.pem"), "-CAkey", str(pki / f"{ca}.key"),
           "-set_serial", str(100 + len(list(pki.iterdir()))), "-days", "2", "-extfile", str(ext), "-out", str(pki / f"{name}.pem"))
    sh("useradd", "--uid", str(UID), "--no-create-home", "--shell", "/usr/sbin/nologin", "suifleet-tunnel")
    key = pki / "client.key"
    key.chmod(0o600)
    os.chown(key, UID, UID)
    shutil.copy(pki / "ca.pem", "/usr/local/share/ca-certificates/sf063-test.crt")
    sh("update-ca-certificates")
    with open("/etc/hosts", "a") as hosts:
        hosts.write(f"\n127.0.0.1 {HOST}\n")
    binary = Path("/usr/bin/suifleet")
    shutil.copy("/binary", binary)
    binary.chmod(0o755)
    # Package extraction happens only inside the disposable test container.
    sh("tar", "-xzf", "/package.tar.gz", "-C", "/opt")
    shutil.copy("/opt/localproxy", "/usr/libexec/suifleet-localproxy")
    empty = Path("/usr/share/suifleet-tunnel/empty")
    empty.mkdir(parents=True)
    shutil.copy("/opt/proxy-settings.json", empty.parent / "proxy-settings.json")
    shutil.copy("/opt/suifleet-tunnel.service", f"/etc/systemd/system/{UNIT}")
    # Normal container setup: no network-online wait is needed on loopback.
    sh("systemctl", "daemon-reload")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    connections = []

    def echo():
        while True:
            sock, _ = server.accept()
            connections.append(sock)
            def copy(sock):
                try:
                    while data := sock.recv(4096):
                        sock.sendall(data)
                except OSError:
                    pass
                finally:
                    sock.close()
            threading.Thread(target=copy, args=(sock,), daemon=True).start()
    threading.Thread(target=echo, daemon=True).start()
    config = json.loads(Path("/opt/config.example.json").read_text())
    config.update(enabled=True, proxy_sha256=hashlib.sha256(Path("/usr/libexec/suifleet-localproxy").read_bytes()).hexdigest(),
                  destinations={"SSH": f"127.0.0.1:{port}", "FAIL": "127.0.0.1:1"}, tunnel_timeout_seconds=3)
    config["aws_iot"].update(endpoint="localhost", thing_name="thing1", client_id="thing1", port=8883,
                             ca_file=str(pki / "ca.pem"), certificate_file=str(pki / "client.pem"), private_key_file=str(key), credential_check_seconds=5)
    path = directory / "config.json"
    path.write_text(json.dumps(config))
    broker = AwsIot(str(pki / "ca.pem"), str(pki / "server.pem"), str(pki / "server.key"))
    broker.register("client", str(pki / "client.pem"), "thing1")
    broker.policies["thing1"] = {"subscribe": [TOPIC], "publish": []}
    broker.listen(8883)
    tunnel = Tunnel(pki)

    def start():
        sh("systemctl", "start", UNIT)
        wait_for(lambda: broker.sessions.get("thing1") and broker.sessions["thing1"].connection and
                 not broker.sessions["thing1"].connection.closed and TOPIC in broker.sessions["thing1"].subscriptions, "MQTT subscription")
        time.sleep(.15)  # SUBACK has reached the adapter.

    tokens = []
    def notify(label, **patch):
        token = "SF063-ephemeral-CAT-" + label
        tokens.append(token)
        body = dict(clientAccessToken=token, clientMode="destination", region="us-east-1", services=["SSH"])
        body.update(patch)
        broker.route(TOPIC, json.dumps(body).encode())
        return hashlib.sha256(token.encode()).hexdigest()

    def client(digest):
        return wait_for(lambda: next((c for c in tunnel.clients if c["digest"] == digest), None), "proxy TLS/WebSocket handshake " + str(tunnel.errors))

    def closed(c):
        wait_for(lambda: c["closed"], "proxy cleanup")

    def proxy_pids():
        return [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit() and
                (p / "comm").exists() and (p / "comm").read_text().strip() == "suifleet-localp"]

    start()
    for label, patch in (("bad-target", {"services": ["SHELL"]}), ("source", {"clientMode": "source"}),
                         ("region", {"region": "eu-west-1"}), ("unknown-field", {"command": "whoami"})):
        notify(label, **patch)
    time.sleep(.4)
    assert not tunnel.clients and not connections
    check("denied destinations/mode/region/notification commands launch no proxy")
    c = client(notify("forward"))
    frame(c["socket"], message(5))
    frame(c["socket"], message(2))
    wait_for(lambda: connections, "allow-listed local TCP connection")
    frame(c["socket"], message(1, payload=b"sf063-echo-payload"))
    wait_for(lambda: b"sf063-echo-payload" in c["data"], "round-trip bytes through actual AWS proxy")
    pids = proxy_pids()
    assert len(pids) == 1, pids
    for pid in pids:
        assert all(token.encode() not in Path(f"/proc/{pid}/cmdline").read_bytes() for token in tokens)
        # A different UID (standing in for an untrusted component) cannot read CATs.
        result = subprocess.run(["setpriv", "--reuid=63364", "--regid=63364", "--clear-groups", "cat", f"/proc/{pid}/environ"], capture_output=True)
        assert result.returncode != 0 and not result.stdout
    notify("concurrency")
    time.sleep(.2)
    assert len(tunnel.clients) == 1
    closed(c)
    assert not proxy_pids()
    notify("forward")  # replay after expiry
    notify("concurrency")  # refused-at-capacity tokens cannot be reused
    time.sleep(.3)
    assert len(tunnel.clients) == 1
    check("actual proxy forwards bytes; CAT absent from argv/cross-UID proc; concurrency/timeout/replay enforced")
    c = client(notify("disconnect"))
    broker.down = True
    broker.kick()
    closed(c)
    assert not proxy_pids()
    check("notification connection loss revokes all local proxy processes")
    broker.down = False
    wait_for(lambda: broker.sessions["thing1"].connection and not broker.sessions["thing1"].connection.closed, "MQTT reconnect")
    time.sleep(.3)
    c = client(notify("replace"))
    old = int(sh("systemctl", "show", UNIT, "-p", "MainPID", "--value"))
    generation = len([e for e in broker.events if e["event"] == "connected"])
    sh("systemctl", "restart", UNIT)
    wait_for(lambda: len([e for e in broker.events if e["event"] == "connected"]) > generation and TOPIC in broker.sessions["thing1"].subscriptions, "replacement MQTT subscription")
    closed(c)
    assert not proxy_pids() and old != int(sh("systemctl", "show", UNIT, "-p", "MainPID", "--value"))
    check("service/component replacement removes prior proxy and token without replay")
    time.sleep(.3)
    c = client(notify("adapter-crash"))
    generation = len([e for e in broker.events if e["event"] == "connected"])
    sh("systemctl", "kill", "--kill-whom=main", "--signal=KILL", UNIT)
    closed(c)
    wait_for(lambda: not proxy_pids(), "systemd crash descendant cleanup")
    wait_for(lambda: sh("systemctl", "is-active", UNIT, check=False) == "active", "service crash restart")
    wait_for(lambda: len([e for e in broker.events if e["event"] == "connected"]) > generation and TOPIC in broker.sessions["thing1"].subscriptions, "crash successor MQTT subscription")
    time.sleep(.3)
    count = len(tunnel.clients)
    assert not proxy_pids()
    c = client(notify("connection-failure", services=["FAIL"]))
    frame(c["socket"], message(5, service="FAIL"))
    frame(c["socket"], message(2, service="FAIL"))
    closed(c)
    assert len(connections) == 1
    check("adapter SIGKILL cleans descendants; failed local destination never falls back or restarts CAT")
    tunnel.bad_certificate = True
    notify("untrusted-tunnel")
    wait_for(lambda: not proxy_pids(), "untrusted endpoint proxy exit")
    time.sleep(.5)
    assert len(tunnel.clients) == count + 1
    tunnel.bad_certificate = False
    check("actual proxy refuses untrusted tunnel TLS server")
    c = client(notify("credential-revocation"))
    key.unlink()
    closed(c)
    assert not proxy_pids()
    sh("systemctl", "stop", UNIT)
    check("credential removal revokes active sessions; explicit stop leaves no proxies")
    logs = sh("journalctl", "-u", UNIT, "--no-pager")
    assert all(token not in logs for token in tokens)
    assert not broker.published and not tunnel.errors, tunnel.errors
    assert not list(Path("/var/lib").glob("suifleet-tunnel*"))
    check("no CAT in journal, durable spool or application MQTT publications")
    print("EVIDENCE " + json.dumps({"checks": checks, "proxy_sha256": config["proxy_sha256"],
          "localproxy_source": Path("/opt/source-revision.txt").read_text().strip(),
          "systemd": sh("systemctl", "--version").splitlines()[0], "external_acceptance": "unrun: AWS account/service, physical target, mainnet"}))


def host(args):
    # Separate tag/name and private mounts: no developer container or shared ports.
    image = "suifleet-tunnel-systemd:sf063"
    dockerfile = """FROM suifleet-tunnel-package:8a71618acf53
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv python3 procps util-linux && rm -rf /var/lib/apt/lists/* && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
"""
    r = subprocess.run(["docker", "build", "-q", "-t", image, "-"], input=dockerfile, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    name = "suifleet-tunnel-test-" + uuid.uuid4().hex[:8]
    sh("docker", "run", "-d", "--name", name, "--privileged", "--network", "none", "--cgroupns=private",
       "--tmpfs", "/run", "--tmpfs", "/run/lock", "-v", f"{ROOT}:/workspace:ro",
       "-v", f"{args.binary.resolve()}:/binary:ro", "-v", f"{args.package.resolve()}:/package.tar.gz:ro", image)
    try:
        wait_for(lambda: sh("docker", "exec", name, "systemctl", "is-system-running", check=False) in ("running", "degraded"), "systemd boot", 60)
        result = subprocess.run(["docker", "exec", "-e", "PYTHONDONTWRITEBYTECODE=1", name, "python3", "/workspace/scripts/test_tunneling.py", "--inside"], text=True, capture_output=True)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr[-5000:])
            print(sh("docker", "exec", name, "journalctl", "-u", UNIT, "--no-pager", "-n", "40", check=False))
            raise SystemExit(result.returncode)
        evidence = json.loads(result.stdout.strip().splitlines()[-1].removeprefix("EVIDENCE "))
        evidence["head"] = sh("git", "rev-parse", "HEAD")
        evidence["dirty"] = bool(sh("git", "status", "--porcelain"))
        evidence["package_sha256"] = hashlib.sha256(args.package.read_bytes()).hexdigest()
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        sh("docker", "rm", "-f", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--binary", type=Path, default=ROOT / "suifleet-edge/target/debug/suifleet")
    parser.add_argument("--package", type=Path, default=Path("/tmp/suifleet-tunnel.tar.gz"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    inside() if args.inside else host(args)
