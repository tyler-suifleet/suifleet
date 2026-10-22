#!/usr/bin/env python3
"""SF-061 AWS IoT Core connection with the real daemon, against a real Eclipse
Mosquitto 2.0.20 broker with AWS-style mutual TLS and an AWS-shaped IoT Core
endpoint with the Device Shadow service (scripts/aws_iot_harness.py). Local
chain simulator; test certificates generated per run; no AWS account, keys,
gas, public network or hardware.

Both containers share one network namespace with no network (`--network
none`, the broker joins the runner's namespace). The host drives scenarios
through a control API inside the runner (via `docker exec`) and starts and
stops the broker.

  Mosquitto (mutual TLS, identity = certificate CN, ACL file, CRL):
    bridge both ways at QoS 1; an ACL-denied subscription reported; broker
    outage spooled and delivered in order; certificate rotation, then the
    old certificate revoked by CRL; a revoked identity refused until the
    credentials are fixed.
  AWS-shaped endpoint:
    an untrusted server certificate refused; port 443 with ALPN
    x-amzn-mqtt-ca (refused without it); client id bound to the thing;
    denied subscribe (SUBACK 0x80) without stalling other traffic; denied
    publish (disconnect) held in the spool and delivered once the policy
    allows it, or rejected with reject_denied_publications; outage with spooling and the cloud's persistent
    session; Device Shadow sync with conflicts by owner and a version race;
    deletion both ways; partial and complete rotation, unknown-CA, revoked
    and wrong-thing identities; kill -9 with spooled messages; the Sui key
    refused as AWS key; no key material in logs or state.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BROKER_IMAGE = "eclipse-mosquitto:2.0.20@sha256:21421af7b32bf9ce508e9090c8eb13bb81f410ca778dc205506180a6f862d0eb"
BROKER, RUNNER = "suifleet-awsiot-broker", "suifleet-awsiot-runner"
MOSQUITTO_PORT, CONTROL_PORT = 18883, 18997
HOST = "iot.test.local"
# Control-API arguments select from these constants; requests never name paths.
IDENTITIES = {name: name for name in ("thing1a", "thing1b", "thing2", "rogue-thing1")}
PARTS = {"cert": ("pem", "cert.pem"), "key": ("key", "key.pem")}
PHASES = {"mosquitto": "mosquitto", "aws": "aws", "impostor": "impostor"}
APP = 63101
THING = "thing1"


def sh(*command, check=True, input=None):
    result = subprocess.run(command, text=True, capture_output=True, input=input)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)}: {result.stdout}{result.stderr}")
    return result.stdout.strip()


# --- Runner (root, inside the network-none container) -----------------------

PKI = r'''
set -eu
cd /pki
ec() { echo "-newkey ec -pkeyopt ec_paramgen_curve:prime256v1"; }
for ca in ca rogue-ca; do
  openssl req -x509 $(ec) -nodes -days 3 -subj "/CN=SuiFleet test $ca" -keyout $ca.key -out $ca.pem \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign 2>/dev/null
done
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:iot.test.local,DNS:localhost,IP:127.0.0.1\n' > server.ext
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n' > client.ext
issue() { # name subject ca ext keyspec
  openssl req -new $5 -nodes -subj "/CN=$2" -keyout $1.key -out $1.csr 2>/dev/null
  openssl x509 -req -in $1.csr -CA $3.pem -CAkey $3.key -set_serial 0x$(openssl rand -hex 8) -days 3 -extfile $4 -out $1.pem 2>/dev/null
  rm $1.csr
}
issue server iot.test.local ca server.ext "$(ec)"
issue rogue-server iot.test.local rogue-ca server.ext "$(ec)"
issue thing1a thing1 ca client.ext "-newkey rsa:2048"
issue thing1b thing1 ca client.ext "$(ec)"
issue thing2 thing2 ca client.ext "$(ec)"
issue admin admin ca client.ext "$(ec)"
issue rogue-thing1 thing1 rogue-ca client.ext "$(ec)"
mkdir -p cadb && : > cadb/index.txt && echo 1000 > cadb/crlnumber
printf '[ca]\ndefault_ca=d\n[d]\ndatabase=/pki/cadb/index.txt\ncrlnumber=/pki/cadb/crlnumber\ndefault_md=sha256\ndefault_crl_days=3\ncertificate=/pki/ca.pem\nprivate_key=/pki/ca.key\n' > cadb/ca.cnf
openssl ca -config cadb/ca.cnf -gencrl -out crl.pem 2>/dev/null
printf 'user admin\ntopic readwrite #\nuser thing1\ntopic write dt/thing1/#\ntopic read cmd/thing1/#\n' > acl
chmod 600 *.key
'''


def serve():
    sys.path.insert(0, "/workspace/scripts")
    from aws_iot_harness import AwsIot
    import test_ota
    from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc
    assert os.geteuid() == 0
    with open("/etc/hosts", "a") as hosts:
        hosts.write(f"127.0.0.1 {HOST}\n")
    subprocess.run(["bash", "-c", PKI], check=True)
    pki = Path("/pki")
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

    # The AWS-shaped endpoint: 8883, 443 (ALPN) and an impostor with a server
    # certificate from another CA.
    aws = AwsIot(str(pki / "ca.pem"), str(pki / "server.pem"), str(pki / "server.key"))
    aws.listen(8883)
    aws.listen(443, alpn="x-amzn-mqtt-ca")
    aws.listen(9443, server_cert=str(pki / "rogue-server.pem"), server_key=str(pki / "rogue-server.key"))
    for name, thing in (("thing1a", "thing1"), ("thing1b", "thing1"), ("thing2", "thing2")):
        aws.register(name, str(pki / f"{name}.pem"), thing)
    for thing in ("thing1", "thing2"):
        aws.policies[thing] = {"publish": [f"dt/{thing}/*", f"$aws/things/{thing}/shadow/*"],
                               "subscribe": [f"cmd/{thing}/*", f"$aws/things/{thing}/shadow/*"]}

    root = Path(tempfile.mkdtemp(prefix="suifleet-awsiot-"))
    root.chmod(0o755)
    # A coordinator worktree can be mode 0700. Stage only the public SDK
    # inside this disposable runner so the distinct test UID can import it.
    ipc_sdk = root / "ipc-sdk"
    ipc_sdk.mkdir(mode=0o755)
    sdk_file = ipc_sdk / "suifleet_ipc.py"
    sdk_file.write_bytes(Path("/workspace/sdk/python/suifleet_ipc.py").read_bytes())
    sdk_file.chmod(0o644)
    key = root / "device.key"
    key.write_text("07" * 32)
    key.chmod(0o600)
    credentials = root / "aws"
    credentials.mkdir(mode=0o700)
    url = f"http://127.0.0.1:{chain.server_port}"
    (root / "deployment.json").write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
        chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
        type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})))
    socket = root / "ipc.sock"
    binary = "/workspace/suifleet-edge/target/debug/suifleet"

    def install(name, part):
        """Atomically replace the configured certificate or key (rotation)."""
        suffix, installed = PARTS[part]
        source = pki / f"{IDENTITIES[name]}.{suffix}"
        target = credentials / installed
        temporary = target.with_suffix(".new")
        temporary.write_bytes(source.read_bytes())
        temporary.chmod(0o600)
        temporary.rename(target)

    (credentials / "ca.pem").write_bytes((pki / "ca.pem").read_bytes())
    install("thing1a", "cert")
    install("thing1a", "key")

    def config(phase, private_key=None, reject=False, defender=None):
        phase = PHASES[phase]
        state = root / (f"state-{phase}" + (f"-defender-{defender}" if defender else ""))
        port = {"mosquitto": MOSQUITTO_PORT, "aws": 443, "impostor": 9443}[phase]
        text = ('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\n'
            f'[ipc]\nsocket = "{socket}"\nadministrators = [0]\n'
            f'[ipc.components.app]\nuid = {APP}\n'
            'operations = ["publish", "subscribe", "receive", "ack", "shadow_get", "shadow_update", "shadow_delete"]\n'
            '[[ipc.components.app.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\n'
            'resources = ["telemetry/*", "forbidden/*", "denied/*"]\n'
            '[[ipc.components.app.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\n'
            f'resources = ["cmd/{THING}/#", "$aws/things/{THING}/shadow/name/config/update/delta"]\n'
            '[[ipc.components.app.shadow_policies]]\noperations = ["*"]\n'
            f'resources = ["$aws/things/{THING}/shadow", "$aws/things/{THING}/shadow/name/*"]\n'
            '[pubsub]\nmax_messages_per_subscription = 50\n[pubsub.bridge]\n'
            '[[pubsub.bridge.mappings]]\ntopic = "telemetry/#"\nsource = "Pubsub"\ntarget = "IotCore"\n'
            f'target_topic_prefix = "dt/{THING}/"\n'
            f'[[pubsub.bridge.mappings]]\ntopic = "cmd/{THING}/#"\nsource = "IotCore"\ntarget = "Pubsub"\n')
        if phase == "mosquitto":
            # Denied by the broker's ACL file.
            text += ('[[pubsub.bridge.mappings]]\ntopic = "denied/#"\nsource = "Pubsub"\ntarget = "IotCore"\ntarget_topic_prefix = "dt/other/"\n'
                     '[[pubsub.bridge.mappings]]\ntopic = "cmd/other/#"\nsource = "IotCore"\ntarget = "Pubsub"\n')
        else:
            # Denied by the thing's AWS IoT policy.
            text += ('[[pubsub.bridge.mappings]]\ntopic = "forbidden/#"\nsource = "Pubsub"\ntarget = "IotCore"\n'
                     '[[pubsub.bridge.mappings]]\ntopic = "cmd/forbidden/#"\nsource = "IotCore"\ntarget = "Pubsub"\n'
                     f'[shadows]\nthing_name = "{THING}"\n[shadows.aws_iot]\nshadows = ["", "config"]\n'
                     'interval_seconds = 3600\nmax_backoff_seconds = 4\n')
        text += (f'[aws_iot]\nendpoint = "{HOST}"\nport = {port}\nthing_name = "{THING}"\n'
                 f'ca_file = "{credentials / "ca.pem"}"\ncertificate_file = "{credentials / "cert.pem"}"\n'
                 f'private_key_file = "{private_key or credentials / "key.pem"}"\nkeep_alive_seconds = 30\ncredential_check_seconds = 5\n')
        if reject:
            text += 'reject_denied_publications = true\n'
        if defender:
            assert defender in ("accept", "reject", "retry")
            text += '[device_defender]\nsample_interval_seconds = 300\npublish_retry_count = 2\nbuffer_max = 2\n'
        path = root / f"config-{phase}.toml"
        path.write_text(text)
        return path

    daemon = {"process": None}
    log = (root / "daemon.log").open("a")

    def start(phase, reject=False, defender=None):
        daemon["process"] = subprocess.Popen([binary], env={**os.environ, "EDGE_CONFIG": str(config(phase, reject=reject, defender=defender))}, stdout=log, stderr=log)
        deadline = time.time() + 30
        while not socket.exists() and time.time() < deadline:
            assert daemon["process"].poll() is None, "the daemon exited at startup"
            time.sleep(0.1)
        assert socket.exists(), "IPC socket did not appear"

    def stop(kill=False):
        process = daemon["process"]
        if process:
            (process.kill if kill else process.terminate)()
            process.wait(timeout=30)
        daemon["process"] = None
        if socket.exists():
            socket.unlink()

    def ipc(uid, operation, topic="", payload=None):
        component = {APP: "app", 0: ""}[uid]
        code = ("import os,sys,json; uid=int(sys.argv[1]); "
                "uid and (os.setgid(uid), os.setuid(uid)); "
                f"sys.path.insert(0,{str(ipc_sdk)!r}); from suifleet_ipc import Client, IpcError\n"
                "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
        out = subprocess.run([sys.executable, "-c", code, str(uid), str(socket), component, operation, topic, json.dumps(payload)],
                             text=True, capture_output=True)
        if out.returncode:
            return {"ok": False, "error": out.stderr.strip()[-300:]}
        return json.loads(out.stdout)

    def probe_without_alpn():
        """A TLS client offering no ALPN to the 443 listener is refused."""
        import ssl
        import socket as sockets
        context = ssl.create_default_context(cafile=str(pki / "ca.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(pki / "thing1b.pem"), str(pki / "thing1b.key"))
        with sockets.create_connection(("127.0.0.1", 443), timeout=10) as raw, context.wrap_socket(raw, server_hostname=HOST) as tls:
            tls.sendall(bytes([0x10, 18, 0, 4]) + b"MQTT" + bytes([4, 0, 0, 30, 0, 6]) + b"thing1")
            try:
                return tls.recv(4) == b""
            except (ConnectionError, ssl.SSLError):
                return True

    def scan():
        """Key material anywhere the daemon writes: its log and state."""
        # A running daemon replaces state through temporary files, so a
        # listing can name a file that is gone before it is read. Scan only
        # a stopped daemon's files: complete, and nothing skipped.
        assert daemon["process"] is None, "scan requires a stopped daemon"
        markers = [b"PRIVATE KEY"]
        for name in ("thing1a", "thing1b", "thing2", "rogue-thing1"):
            body = [line for line in (pki / f"{name}.key").read_bytes().splitlines() if line and not line.startswith(b"-----")]
            markers += body[:2]
        found = []
        for path in [root / "daemon.log", *[p for p in root.glob("state-*/**/*") if p.is_file()]]:
            data = path.read_bytes()
            found += [f"{path.name}: {m[:12]!r}" for m in markers if m in data]
        return found

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                path = self.path
                if path == "/ipc":
                    result = ipc(body["uid"], body["operation"], body.get("topic", ""), body.get("payload"))
                elif path == "/start":
                    start(body["phase"], body.get("reject", False), body.get("defender"))
                    result = {"ok": True}
                elif path == "/stop":
                    stop(body.get("kill", False))
                    result = {"ok": True}
                elif path == "/defender-state":
                    label = body["label"]
                    assert label in ("accept", "reject", "retry")
                    file = root / f"state-aws-defender-{label}/device-defender/state.json"
                    result = json.loads(file.read_text()) if file.exists() else {}
                elif path == "/check-config":
                    private_key = key if body.get("sui_key") else None
                    out = subprocess.run([binary, "--check-config", str(config(body["phase"], private_key))], text=True, capture_output=True)
                    result = {"ok": out.returncode == 0, "output": (out.stdout + out.stderr)[-600:]}
                elif path == "/install":
                    install(body["name"], body["part"])
                    result = {"ok": True}
                elif path == "/aws":
                    op = body["op"]
                    if op == "snapshot":
                        result = aws.snapshot()
                    elif op == "publish":
                        aws.route(body["topic"], json.dumps(body["payload"]).encode())
                        result = {"ok": True}
                    elif op == "update":
                        result = {"version": aws.update(THING, body["name"], {"state": body["state"]})}
                    elif op == "delete":
                        aws.delete(THING, body["name"])
                        result = {"ok": True}
                    elif op == "race":
                        aws.race[(THING, body["name"])] = body["state"]
                        result = {"ok": True}
                    elif op == "active":
                        aws.set_active(body["name"], body["active"])
                        result = {"ok": True}
                    elif op == "down":
                        aws.down = body["down"]
                        if aws.down:
                            aws.kick()
                        result = {"ok": True}
                    elif op == "allow":
                        allowed = aws.policies[THING][body["action"]]
                        if body["allow"]:
                            allowed.append(body["pattern"])
                        else:
                            allowed.remove(body["pattern"])
                        result = {"ok": True}
                    elif op == "kick":
                        aws.kick()
                        result = {"ok": True}
                    elif op == "probe-no-alpn":
                        result = {"refused": probe_without_alpn()}
                    else:
                        raise ValueError(op)
                elif path == "/revoke":
                    subprocess.run(["openssl", "ca", "-config", "/pki/cadb/ca.cnf", "-revoke", f"/pki/{IDENTITIES[body['name']]}.pem"], check=True, capture_output=True)
                    subprocess.run(["openssl", "ca", "-config", "/pki/cadb/ca.cnf", "-gencrl", "-out", "/pki/crl.pem"], check=True, capture_output=True)
                    os.chown("/pki/crl.pem", int(os.environ["HOST_UID"]), int(os.environ["HOST_GID"]))
                    result = {"ok": True}
                elif path == "/scan":
                    result = {"found": scan()}
                elif path == "/log":
                    result = {"log": (root / "daemon.log").read_text()[-6000:]}
                else:
                    raise ValueError(path)
            except Exception as error:
                result = {"ok": False, "error": repr(error)}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    subprocess.run(["useradd", "-u", str(APP), "-M", "-s", "/usr/sbin/nologin", "awsiotapp"], check=False, capture_output=True)
    for path in pki.rglob("*"):
        os.chown(path, int(os.environ["HOST_UID"]), int(os.environ["HOST_GID"]))
    print("READY", flush=True)
    ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control).serve_forever()


# --- Host ------------------------------------------------------------------------

def control(path, **body):
    out = sh("docker", "exec", "-i", RUNNER, "curl", "-sS", "--max-time", "120", "-H", "content-type: application/json",
             "--data-binary", "@-", f"http://127.0.0.1:{CONTROL_PORT}{path}", input=json.dumps(body))
    result = json.loads(out)
    if isinstance(result, dict) and result.get("ok") is False and "error" in result and path != "/ipc" and path != "/check-config":
        raise AssertionError(f"{path}: {result['error']}")
    return result


def ipc(operation, topic="", payload=None, uid=APP):
    return control("/ipc", uid=uid, operation=operation, topic=topic, payload=payload)


def aws(op, **body):
    return control("/aws", op=op, **body)


def stats():
    return ipc("messaging", uid=0)["data"]["bridge"]


def iot():
    return stats()["iot_core"]


def mapping(topic):
    return next(m for m in stats()["mappings"] if m["topic"] == topic)


def wait(condition, message, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condition():
                return
        except (KeyError, TypeError, StopIteration):
            pass
        time.sleep(0.5)
    raise AssertionError(message)


def mosquitto(tool, *args):
    return sh("docker", "exec", BROKER, tool, "-h", "127.0.0.1", "-p", str(MOSQUITTO_PORT), "--cafile", "/pki/ca.pem",
              "--cert", "/pki/admin.pem", "--key", "/pki/admin.key", "-q", "1", *args, check=False)


def cloud_sub(topic, count, timeout=30):
    """Messages for a persistent admin session on the broker (created on first use)."""
    limit = [] if count is None else ["-C", str(count)]
    out = mosquitto("mosquitto_sub", "-c", "-i", "cloudclient", "-t", topic, *limit, "-W", str(timeout), "-v")
    return [line for line in out.splitlines() if line]


def receive_all(filter_, expected, timeout=60):
    got, deadline = [], time.time() + timeout
    while len(got) < expected and time.time() < deadline:
        message = ipc("receive", filter_)["data"]
        if message:
            got.append(message)
            ipc("ack", filter_, message["id"])
        else:
            time.sleep(0.3)
    return got


def published(topic_prefix):
    return [(t, json.loads(p)) for _, t, p, _ in aws("snapshot")["published"] if t.startswith(topic_prefix)]


def connections():
    return aws("snapshot")["connections"]


def local(name):
    r = ipc("shadow_get", name, uid=0)
    return {k: v for k, v in r["data"]["state"].items() if k != "delta"} if r["ok"] else None


def cloud(name):
    shadow = aws("snapshot")["shadows"].get(f"{THING}/{name}")
    return shadow["state"] if shadow and shadow["exists"] else None


def host(args):
    for binary in ("suifleet", "examples/grpc_sim_shim"):
        assert (ROOT / "suifleet-edge/target/debug" / binary).exists(), f"build {binary} first"
    work = Path(tempfile.mkdtemp(prefix="suifleet-awsiot-host-"))
    (work / "pki").mkdir(mode=0o755)
    (work / "data").mkdir(mode=0o700)
    (work / "mosquitto.conf").write_text(
        f"listener {MOSQUITTO_PORT} 127.0.0.1\nallow_anonymous false\nrequire_certificate true\nuse_identity_as_username true\n"
        "cafile /pki/ca.pem\ncertfile /pki/server.pem\nkeyfile /pki/server.key\ncrlfile /pki/crl.pem\nacl_file /pki/acl\n"
        "persistence true\npersistence_location /mosquitto/data/\nmax_queued_messages 1000\n")
    for name in (BROKER, RUNNER):
        sh("docker", "rm", "-f", name, check=False)
    checks = []

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    def start_broker():
        sh("docker", "run", "-d", "--name", BROKER, "--network", f"container:{RUNNER}", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{work}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro", "-v", f"{work}/pki:/pki:ro",
           "-v", f"{work}/data:/mosquitto/data", BROKER_IMAGE)

    try:
        sh("docker", "run", "-d", "--name", RUNNER, "--network", "none", "--user", "0", "-v", f"{ROOT}:/workspace:ro",
           "-v", f"{work}/pki:/pki", "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}",
           "-e", "PYTHONDONTWRITEBYTECODE=1", args.image, "python3", "/workspace/scripts/test_aws_iot.py", "--serve")
        wait(lambda: "READY" in sh("docker", "logs", RUNNER, check=False), "runner did not start", 120)
        start_broker()
        version = sh("docker", "exec", BROKER, "mosquitto", "-h", check=False).splitlines()[0:1] or ["mosquitto"]

        # --- Separate trust domain -------------------------------------------------
        refused = control("/check-config", phase="aws", sui_key=True)
        assert not refused["ok"] and "Sui device key" in refused["output"], refused
        assert control("/check-config", phase="aws")["ok"]
        check("trust domains: --check-config refuses the Sui device key as aws_iot.private_key_file; the separate AWS "
              "thing certificate/key configuration verifies")

        # --- Mosquitto: real broker, mutual TLS ------------------------------------
        control("/start", phase="mosquitto")
        wait(lambda: iot()["connected"], "the IoT connection to Mosquitto did not come up")
        subscription = ipc("subscribe", f"cmd/{THING}/#", {"qos": 1})
        assert subscription["ok"], subscription
        assert not cloud_sub("dt/#", None, timeout=1)  # create the persistent cloud-side session
        assert ipc("publish", "telemetry/temp", {"t": 21})["ok"]
        got = cloud_sub(f"dt/#", 1)
        assert got == [f'dt/{THING}/telemetry/temp {{"t":21}}'], got
        mosquitto("mosquitto_pub", "-t", f"cmd/{THING}/reboot", "-m", '{"cmd":"reboot"}')
        got = receive_all(f"cmd/{THING}/#", 1)
        assert [(m["topic"], m["payload"], m["sender"]) for m in got] == [(f"cmd/{THING}/reboot", {"cmd": "reboot"}, "@bridge/IotCore")], got
        assert ipc("publish", "denied/x", {"d": 1})["ok"]
        wait(lambda: mapping("denied/#")["forwarded"] == 1, "the denied publication was not handed to the broker")
        assert not cloud_sub("dt/#", None, timeout=2), "the broker delivered an ACL-denied publication"
        mosquitto("mosquitto_pub", "-t", "cmd/other/x", "-m", '{"other":1}')
        mosquitto("mosquitto_pub", "-t", f"cmd/{THING}/after", "-m", '{"after":1}')
        got = receive_all(f"cmd/{THING}/#", 1)
        assert [m["topic"] for m in got] == [f"cmd/{THING}/after"] and mapping("cmd/other/#")["forwarded"] == 0, got
        # Mosquitto grants a denied MQTT 3.1.1 subscription at SUBACK but never delivers on it.
        assert iot()["refused_subscriptions"] == []
        check(f"Mosquitto {version[0]} mutual TLS (client identity = certificate CN, ACL file, CRL): component -> cloud "
              "under dt/thing1/ and cloud -> component at QoS 1; an ACL-denied publication is dropped by the broker "
              "(MQTT 3.1.1 PUBACK) and never delivered; the ACL-denied subscription cmd/other/# receives nothing "
              "(Mosquitto grants it at SUBACK for MQTT 3.1.1)")

        sh("docker", "stop", BROKER)
        wait(lambda: not iot()["connected"], "the outage was not noticed")
        for i in range(5):
            assert ipc("publish", f"telemetry/s{i}", {"i": i})["ok"]
        sh("docker", "start", BROKER)
        got = cloud_sub("dt/#", 5, timeout=90)
        assert [json.loads(line.split(" ", 1)[1])["i"] for line in got] == [0, 1, 2, 3, 4], got
        check("Mosquitto outage: 5 publications spooled on disk while the broker was stopped, delivered in order after reconnect")

        before = iot()
        control("/install", name="thing1b", part="key")  # half a rotation: key and certificate do not match
        wait(lambda: iot()["credential_error"], "a mismatched certificate/key set was not reported")
        assert iot()["connected"] and iot()["certificate_sha256"] == before["certificate_sha256"]
        control("/install", name="thing1b", part="cert")
        wait(lambda: iot()["rotations"] == 1 and iot()["connected"] and iot()["certificate_sha256"] != before["certificate_sha256"],
             "the rotated credentials were not taken into use")
        assert iot()["credential_error"] is None
        control("/revoke", name="thing1a")
        sh("docker", "restart", BROKER)
        wait(lambda: iot()["connected"] and iot()["connects"] > before["connects"] + 1, "no reconnect after the broker restart")
        assert ipc("publish", "telemetry/after-rotation", {"r": 1})["ok"]
        got = cloud_sub("dt/#", 1)
        assert got == [f'dt/{THING}/telemetry/after-rotation {{"r":1}}'], got
        check("Mosquitto rotation: a half-installed set (new key, old certificate) is reported and ignored while the old "
              "connection continues; the complete set reconnects with the new certificate (RSA -> P-256); with the old "
              "certificate revoked by CRL and the broker restarted, traffic continues on the new identity")

        control("/install", name="thing1a", part="cert")
        control("/install", name="thing1a", part="key")
        wait(lambda: iot()["rotations"] == 2 and not iot()["connected"] and iot()["last_error"], "the revoked identity was not refused", 90)
        assert ipc("publish", "telemetry/while-revoked", {"w": 1})["ok"]
        control("/install", name="thing1b", part="cert")
        control("/install", name="thing1b", part="key")
        got = cloud_sub("dt/#", 1, timeout=120)
        assert got == [f'dt/{THING}/telemetry/while-revoked {{"w":1}}'], got
        check("Mosquitto revoked identity: rotating to the CRL-revoked certificate is refused by the broker and reported; "
              "the publication waits in the spool and is delivered once valid credentials are installed")
        control("/stop")
        sh("docker", "rm", "-f", BROKER)

        # --- AWS-shaped endpoint -----------------------------------------------------
        control("/install", name="thing1a", part="cert")
        control("/install", name="thing1a", part="key")
        control("/start", phase="impostor")
        wait(lambda: iot()["last_error"] and "certificate" in iot()["last_error"].lower(), "an untrusted server certificate was accepted")
        assert not iot()["connected"] and not connections()
        error = iot()["last_error"]
        control("/stop")
        check(f"untrusted endpoint: a server certificate from another CA is refused ({error[:120]})")

        control("/start", phase="aws")
        wait(lambda: iot()["connected"], "the IoT connection did not come up on 443")
        conn = connections()
        assert conn == [{"client_id": THING, "thing": THING, "alpn": "x-amzn-mqtt-ca", "port": 443, "certificate": "thing1a"}], conn
        assert iot()["alpn"] == "x-amzn-mqtt-ca"
        assert aws("probe-no-alpn")["refused"]
        check("port 443: ALPN x-amzn-mqtt-ca negotiated by default, client id = thing name, thing certificate accepted; "
              "a client without ALPN is refused there")

        wait(lambda: iot()["refused_subscriptions"] == ["cmd/forbidden/#"], "the policy-denied subscription was not reported")
        assert ipc("subscribe", f"cmd/{THING}/#", {"qos": 1})["ok"]
        assert ipc("publish", "telemetry/a", {"n": 1})["ok"]
        wait(lambda: published(f"dt/{THING}/") == [(f"dt/{THING}/telemetry/a", {"n": 1})], "telemetry did not reach the cloud")
        aws("publish", topic=f"cmd/{THING}/x", payload={"c": 1})
        got = receive_all(f"cmd/{THING}/#", 1)
        assert [(m["topic"], m["payload"]) for m in got] == [(f"cmd/{THING}/x", {"c": 1})], got
        check("AWS-shaped endpoint: component -> dt/thing1/ and cloud -> component at QoS 1; the policy-denied "
              "subscription cmd/forbidden/# gets SUBACK 0x80, is reported, and the other subscriptions work")

        def denied_publishes():
            return [d for d in aws("snapshot")["denied"] if d[1] == "publish"]

        connects = iot()["connects"]
        assert ipc("publish", "forbidden/x", {"bad": 1})["ok"]
        assert ipc("publish", "telemetry/b", {"n": 2})["ok"]
        wait(lambda: "policy denial" in (mapping("forbidden/#").get("held") or ""), "the denied publication was not held", 120)
        # Held, not discarded: retried on later connections and still spooled.
        wait(lambda: len(denied_publishes()) >= 4, "the held publication was not retried", 120)
        assert denied_publishes() == [[THING, "publish", "forbidden/x"]] * len(denied_publishes()), denied_publishes()
        assert mapping("forbidden/#")["rejected"] == 0 and iot()["connects"] >= connects + 3, (mapping("forbidden/#"), iot())
        assert (f"dt/{THING}/telemetry/b", {"n": 2}) not in published(f"dt/{THING}/")
        # The operator fixes the policy: the original message is delivered, then what queued behind it.
        aws("allow", action="publish", pattern="forbidden/*", allow=True)
        wait(lambda: ("forbidden/x", {"bad": 1}) in published("forbidden/"), "the held publication was not delivered", 120)
        wait(lambda: (f"dt/{THING}/telemetry/b", {"n": 2}) in published(f"dt/{THING}/"), "traffic stalled behind the held publication", 60)
        assert "held" not in mapping("forbidden/#") and mapping("forbidden/#")["rejected"] == 0
        aws("allow", action="publish", pattern="forbidden/*", allow=False)
        check("policy-denied publication: an AWS-style disconnect (TLS truncated, no close_notify) on each attempt holds "
              "the message in the spool and reports it (never discarded); retried until the policy allows it, then "
              "delivered with the telemetry queued behind it")

        control("/stop")
        control("/start", phase="aws", reject=True)
        wait(lambda: iot()["connected"], "the IoT connection did not come back")
        denied_before = len(denied_publishes())
        assert ipc("publish", "forbidden/y", {"bad": 2})["ok"]
        assert ipc("publish", "telemetry/c", {"n": 3})["ok"]
        wait(lambda: mapping("forbidden/#")["rejected"] == 1, "the denied publication was not rejected", 120)
        wait(lambda: (f"dt/{THING}/telemetry/c", {"n": 3}) in published(f"dt/{THING}/"), "traffic stalled behind the rejected publication", 60)
        assert denied_publishes()[denied_before:] == [[THING, "publish", "forbidden/y"]] * 3, denied_publishes()
        assert "policy denial" in mapping("forbidden/#")["last_rejected"]
        control("/stop")
        control("/start", phase="aws")
        wait(lambda: iot()["connected"], "the IoT connection did not come back")
        check("reject_denied_publications = true: after 3 such disconnects the message is rejected and counted, and the "
              "following telemetry is delivered")

        # Device Shadow: local -> cloud, cloud -> local with a delta notification.
        assert ipc("subscribe", f"$aws/things/{THING}/shadow/name/config/update/delta", {"qos": 1})["ok"]
        assert ipc("shadow_update", "config", {"state": {"reported": {"mode": "boot", "fw": 1}}})["ok"]
        wait(lambda: cloud("config") == {"reported": {"mode": "boot", "fw": 1}}, "the local shadow was not pushed")
        aws("update", name="config", state={"desired": {"mode": "run"}})
        wait(lambda: local("config") == {"desired": {"mode": "run"}, "reported": {"mode": "boot", "fw": 1}}, "the cloud change was not pulled", 30)
        got = receive_all(f"$aws/things/{THING}/shadow/name/config/update/delta", 1)
        assert got and got[0]["payload"]["state"] == {"mode": "run"}, got
        check("Device Shadow: a local report is pushed; a cloud desired change is pulled within seconds although the "
              "poll interval is 3600 s (update/documents notification) and announced as a local delta")

        # Offline edits on both sides; conflicts settled by owner.
        aws("down", down=True)
        wait(lambda: not iot()["connected"], "the outage was not noticed")
        assert ipc("shadow_update", "config", {"state": {"desired": {"x": "device", "only_device": 1}, "reported": {"mode": "device"}}})["ok"]
        aws("update", name="config", state={"desired": {"x": "cloud", "only_cloud": 1}, "reported": {"mode": "cloud", "cloud_report": 1}})
        for i in range(3):
            assert ipc("publish", f"telemetry/o{i}", {"o": i})["ok"]
        aws("publish", topic=f"cmd/{THING}/offline", payload={"c": 2})
        aws("down", down=False)
        merged = {"desired": {"mode": "run", "x": "cloud", "only_device": 1, "only_cloud": 1},
                  "reported": {"mode": "device", "fw": 1, "cloud_report": 1}}
        wait(lambda: local("config") == merged and cloud("config") == merged, "offline edits did not reconcile", 90)
        status = ipc("shadows", uid=0, payload={"shadow": "config"})["data"]
        assert status["sync"]["conflicts"] >= 2, status
        wait(lambda: [p["o"] for t, p in published(f"dt/{THING}/telemetry/o")] == [0, 1, 2], "spooled telemetry did not arrive in order")
        got = receive_all(f"cmd/{THING}/#", 1)
        assert got and got[0]["payload"] == {"c": 2}, got
        check("outage: offline local and cloud shadow edits reconciled by three-way merge (desired.x -> cloud owner, "
              "reported.mode -> device owner, one-sided keys kept on both sides); 3 spooled publications delivered in "
              "order and a cloud command queued by the persistent session delivered after reconnect")

        # A version race: another writer updates between the device's read and write.
        aws("race", name="config", state={"desired": {"raced": True}})
        assert ipc("shadow_update", "config", {"state": {"reported": {"after_race": 1}}})["ok"]
        wait(lambda: (local("config") or {}).get("desired", {}).get("raced") is True and cloud("config")["reported"].get("after_race") == 1
             and local("config") == cloud("config"), "the version race did not converge", 60)
        assert [THING, "config", "update", 409] in aws("snapshot")["rejections"], "the race did not cause a version conflict"
        check("version race: the device's update is refused with 409 after a concurrent cloud write; it re-reads, merges "
              "and stores, keeping both changes")

        # Deletion both ways.
        assert ipc("shadow_update", "", {"state": {"reported": {"classic": 1}}})["ok"]
        wait(lambda: cloud("") == {"reported": {"classic": 1}}, "the classic shadow was not pushed")
        assert ipc("shadow_delete", "")["ok"]
        wait(lambda: cloud("") is None, "the local deletion did not propagate")
        aws("update", name="", state={"desired": {"again": 1}})
        wait(lambda: local("") == {"desired": {"again": 1}}, "the recreated cloud shadow was not pulled")
        aws("delete", name="")
        wait(lambda: local("") is None, "the cloud deletion was not pulled")
        check("deletion: a local deletion deletes the AWS shadow; a cloud recreation and a cloud deletion are pulled")

        # Rotation and refused identities.
        first = iot()["certificate_sha256"]
        control("/install", name="thing1b", part="cert")
        control("/install", name="thing1b", part="key")
        wait(lambda: connections() and connections()[0]["certificate"] == "thing1b", "the rotated certificate was not used")
        aws("active", name="thing1a", active=False)
        assert iot()["certificate_sha256"] != first and iot()["rotations"] == 1
        assert ipc("publish", "telemetry/rotated", {"n": 3})["ok"]
        wait(lambda: (f"dt/{THING}/telemetry/rotated", {"n": 3}) in published(f"dt/{THING}/"), "no traffic after rotation")
        refusals = []
        for bad, expect in (("rogue-thing1", "tls-refused"), ("thing1a", "certificate-not-active"), ("thing2", "connect-refused")):
            control("/install", name=bad, part="cert")
            control("/install", name=bad, part="key")
            wait(lambda: not iot()["connected"] and any(e["event"] == expect for e in aws("snapshot")["events"][-6:]),
                 f"the {bad} identity was not refused", 90)
            refusals.append(f"{bad}: {expect}")
            assert ipc("publish", f"telemetry/during-{bad}", {"bad": bad})["ok"]
        control("/install", name="thing1b", part="cert")
        control("/install", name="thing1b", part="key")
        wait(lambda: iot()["connected"] and connections()[0]["certificate"] == "thing1b", "no recovery with valid credentials", 120)
        wait(lambda: [p["bad"] for t, p in published(f"dt/{THING}/telemetry/during-")] == ["rogue-thing1", "thing1a", "thing2"],
             "publications made with refused identities were not delivered after recovery", 60)
        check("identities: unknown CA (TLS refused), deactivated certificate (closed after handshake) and another thing's "
              "certificate (CONNACK 5, client id must be its thing) are each refused; publications wait in the spool and arrive in "
              f"order once valid rotated credentials are installed ({'; '.join(refusals)})")

        # kill -9 with spooled messages; the shadow base survives.
        aws("down", down=True)
        wait(lambda: not iot()["connected"], "the outage was not noticed")
        for i in range(3):
            assert ipc("publish", f"telemetry/k{i}", {"k": i})["ok"]
        updates = aws("snapshot")["shadow_updates"]
        control("/stop", kill=True)
        control("/start", phase="aws")
        aws("down", down=False)
        wait(lambda: [p["k"] for t, p in published(f"dt/{THING}/telemetry/k")] == [0, 1, 2], "spooled telemetry lost across kill -9", 90)
        wait(lambda: iot()["connected"] and ipc("shadows", uid=0, payload={"shadow": "config"})["data"]["sync"]["pushes"] == 0
             and local("config") == cloud("config"), "shadow sync did not resume")
        time.sleep(3)
        assert aws("snapshot")["shadow_updates"] == updates, "unchanged shadows were rewritten after the restart"
        check("kill -9 during an outage: spooled publications delivered in order after restart; the persisted sync base "
              "means no redundant shadow writes")

        control("/stop")
        found = control("/scan")["found"]
        assert not found, found
        check("no private key material in the daemon log or state directory (bridge.json, spools, shadows)")

        evidence = {"ticket": "SF-061", "head": sh("git", "-C", str(ROOT), "rev-parse", "HEAD"), "broker": BROKER_IMAGE,
                    "broker_version": version[0], "aws_endpoint": "scripts/aws_iot_harness.py (AWS-shaped, local)", "checks": checks,
                    "unrun": ["Real AWS IoT Core endpoint, account policy and Device Shadow service; needs separate user authorization",
                              "Physical Pi/target operation (#32)"]}
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
    except Exception:
        print(sh("docker", "logs", "--tail", "60", RUNNER, check=False))
        try:
            print(control("/log").get("log", ""))
            print(json.dumps(aws("snapshot")["events"][-20:], indent=1))
        except Exception:
            pass
        raise
    finally:
        if not args.keep:
            for name in (BROKER, RUNNER):
                sh("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--image", default="suifleet-dev:sf004", help="runner image (the dev image)")
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--keep", action="store_true")
    arguments = parser.parse_args()
    serve() if arguments.serve else host(arguments)
