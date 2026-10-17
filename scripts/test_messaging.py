#!/usr/bin/env python3
"""Durable pub/sub and the local MQTT bridge (SF-032) with the real daemon,
real distinct Unix UIDs and a real Eclipse Mosquitto 2.0.20 broker, against
the local chain simulator: no keys, gas, public network or hardware.

On the host (needs Docker): starts Mosquitto (the standard broker serving LAN
clients; persistence on) and a disposable root runner container, both on the
host network. The runner (`--serve`) starts the simulator and the daemon and a
loopback control API through which the host makes IPC calls as component UIDs.
LAN clients are Mosquitto's own mosquitto_pub/mosquitto_sub.

  ACL                 -> unauthorized publish/subscribe refused; literal filters
  LAN -> component    -> lan/commands/# bridged into pub/sub; QoS 1 receive/ack
  component -> LAN    -> sensors/... bridged out under device1/
  broker down         -> component publications spooled on disk, delivered in
                         order after the broker returns (reconnect)
  spool exhausted     -> beyond the limit publications are refused and counted;
                         exactly the spooled ones arrive, none silently lost
  daemon restart      -> spooled and unacknowledged QoS 1 messages survive
  inbound backpressure-> a full subscriber queue leaves LAN messages with the
                         broker; after draining, they all arrive
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BROKER_IMAGE = "eclipse-mosquitto:2.0.20@sha256:21421af7b32bf9ce508e9090c8eb13bb81f410ca778dc205506180a6f862d0eb"
BROKER, RUNNER = "suifleet-msg-broker", "suifleet-msg-runner"
BROKER_PORT, CONTROL_PORT = 18830, 18899
SPOOL_LIMIT = 20
PUB_UID, SUB_UID = 63001, 63002


def sh(*command, check=True):
    result = subprocess.run(command, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)}: {result.stdout}{result.stderr}")
    return result.stdout.strip()


# --- Runner (inside the root container) -----------------------------------------

def serve():
    sys.path.insert(0, "/workspace/scripts")
    sys.path.insert(0, "/workspace/sdk/python")
    import tempfile
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
    root = Path(tempfile.mkdtemp(prefix="suifleet-msg-"))
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
    run_dir = root / "run"
    run_dir.mkdir(mode=0o755)
    socket = run_dir / "ipc.sock"
    (root / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
        f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
        f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
        f'[runtime]\nstate_dir = "{root / "state"}"\npoll_seconds = 1\n'
        f'[ipc]\nsocket = "{socket}"\nadministrators = [0]\n'
        f'[ipc.components.pub]\nuid = {PUB_UID}\noperations = ["publish"]\n'
        '[[ipc.components.pub.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\nresources = ["sensors/*"]\n'
        f'[ipc.components.sub]\nuid = {SUB_UID}\noperations = ["subscribe", "receive", "ack", "unsubscribe"]\n'
        '[[ipc.components.sub.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["lan/commands/#"]\n'
        f'[pubsub]\nmax_messages_per_subscription = {SPOOL_LIMIT}\n'
        f'[pubsub.bridge]\nbroker = "127.0.0.1:{BROKER_PORT}"\nkeep_alive_seconds = 10\n'
        '[[pubsub.bridge.mappings]]\ntopic = "sensors/#"\nsource = "Pubsub"\ntarget = "LocalMqtt"\ntarget_topic_prefix = "device1/"\n'
        '[[pubsub.bridge.mappings]]\ntopic = "lan/commands/#"\nsource = "LocalMqtt"\ntarget = "Pubsub"\n')
    daemon = {"process": None}
    log = (root / "daemon.log").open("a")

    def start_daemon():
        daemon["process"] = subprocess.Popen(["/workspace/suifleet-edge/target/debug/suifleet"],
            env={**os.environ, "EDGE_CONFIG": str(root / "config.toml")}, stdout=log, stderr=log)
        deadline = time.time() + 30
        while not socket.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert socket.exists(), "IPC socket did not appear"

    def stop_daemon():
        process = daemon["process"]
        process.terminate()
        process.wait(timeout=30)
        if socket.exists():
            socket.unlink()  # The daemon removed it, or an operator would after inspection.

    def ipc(uid, operation, component="", topic="", payload=None):
        code = ("import os,sys,json; os.setgid(int(sys.argv[1])); os.setuid(int(sys.argv[1])); "
                "sys.path.insert(0,'/workspace/sdk/python'); from suifleet_ipc import Client; "
                "c=Client(sys.argv[2], component=sys.argv[3]); "
                "print(json.dumps(c.request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))))")
        result = subprocess.run([sys.executable, "-c", code, str(uid), str(socket), component, operation, topic, json.dumps(payload)],
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
                elif self.path == "/log":
                    result = {"ok": True, "data": (root / "daemon.log").read_text()[-4000:]}
                else:
                    result = {"ok": False, "error": "unknown"}
            except Exception as error:
                result = {"ok": False, "error": repr(error)}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    for uid, name in ((PUB_UID, "msgpub"), (SUB_UID, "msgsub")):
        subprocess.run(["useradd", "-u", str(uid), "-M", "-s", "/usr/sbin/nologin", name], check=False, capture_output=True)
    start_daemon()
    print("READY", flush=True)
    ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control).serve_forever()


# --- Host ------------------------------------------------------------------------

def control(path, **body):
    import urllib.request
    request = urllib.request.Request(f"http://127.0.0.1:{CONTROL_PORT}{path}", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def ipc(uid, operation, component="", topic="", payload=None):
    return control("/ipc", uid=uid, operation=operation, component=component, topic=topic, payload=payload)


def lan_pub(topic, message):
    sh("docker", "exec", BROKER, "mosquitto_pub", "-h", "127.0.0.1", "-p", str(BROKER_PORT), "-q", "1", "-t", topic, "-m", message)


def lan_sub(topic, count, timeout=30, client="lanclient"):
    """Messages from a persistent LAN session (created on first use)."""
    out = sh("docker", "exec", BROKER, "mosquitto_sub", "-h", "127.0.0.1", "-p", str(BROKER_PORT), "-q", "1", "-c", "-i", client,
             "-t", topic, "-C", str(count), "-W", str(timeout), "-v", check=False)
    return [line for line in out.splitlines() if line]


def messaging():
    return ipc(0, "messaging")["data"]


def wait(condition, message, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(message)


def host(args):
    for binary in ("suifleet", "examples/grpc_sim_shim"):
        assert (ROOT / "suifleet-edge/target/debug" / binary).exists(), f"build {binary} first"
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="suifleet-msg-host-"))
    (work / "mosquitto.conf").write_text(f"listener {BROKER_PORT} 127.0.0.1\nallow_anonymous true\npersistence true\n"
                                         "persistence_location /mosquitto/data/\nmax_queued_messages 1000\n")
    (work / "data").mkdir()
    os.chmod(work / "data", 0o777)
    for name in (BROKER, RUNNER):
        sh("docker", "rm", "-f", name, check=False)
    checks = []

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    try:
        sh("docker", "run", "-d", "--name", BROKER, "--network", "host", "-v", f"{work}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro",
           "-v", f"{work}/data:/mosquitto/data", BROKER_IMAGE)
        sh("docker", "run", "-d", "--name", RUNNER, "--network", "host", "--user", "0", "-v", f"{ROOT}:/workspace:ro",
           "-e", "PYTHONDONTWRITEBYTECODE=1", args.image, "python3", "/workspace/scripts/test_messaging.py", "--serve")
        wait(lambda: "READY" in sh("docker", "logs", RUNNER, check=False), "runner did not start", 90)
        wait(lambda: (messaging() or {}).get("bridge", {}) and messaging()["bridge"].get("connected"), "bridge did not connect")
        version = sh("docker", "exec", BROKER, "mosquitto", "-h", check=False).splitlines()[0:1] or ["mosquitto"]

        # ACLs: unauthorized publish/subscribe refused; filters are literal.
        assert not ipc(PUB_UID, "publish", "pub", "actuators/x", {"v": 1})["ok"]
        assert not ipc(SUB_UID, "subscribe", "sub", "lan/+/x", {"qos": 1})["ok"], "a wildcard grant was expanded"
        assert not ipc(SUB_UID, "subscribe", "pub", "lan/commands/#", {"qos": 1})["ok"], "acted as another component"
        assert not ipc(PUB_UID, "messaging")["ok"]
        assert ipc(SUB_UID, "subscribe", "sub", "lan/commands/#", {"qos": 1})["ok"]
        check("ACLs: unauthorized publish and subscribe refused, wildcard grants literal, no cross-component access")

        # LAN client -> component through the bridge.
        lan_pub("lan/commands/reboot", '{"cmd":"reboot"}')
        wait(lambda: ipc(SUB_UID, "receive", "sub", "lan/commands/#")["data"], "the LAN command did not reach the component")
        message = ipc(SUB_UID, "receive", "sub", "lan/commands/#")["data"]
        assert message["payload"] == {"cmd": "reboot"} and message["topic"] == "lan/commands/reboot", message
        assert message["sender"].startswith("@bridge"), message
        assert ipc(SUB_UID, "ack", "sub", "lan/commands/#", message["id"])["data"]["acknowledged"]
        check("LAN client -> component: QoS 1 bridged into durable pub/sub, received and acknowledged")

        # Component -> LAN client.
        lan_sub("device1/sensors/#", 0, timeout=1)  # create the persistent LAN session
        assert ipc(PUB_UID, "publish", "pub", "sensors/a/temp", {"t": 21})["ok"]
        got = lan_sub("device1/sensors/#", 1)
        assert got == ['device1/sensors/a/temp {"t":21}'], got
        check("component -> LAN client: published under device1/ on the local broker")

        # Broker down: publications spool on disk and arrive in order afterwards.
        sh("docker", "stop", BROKER)
        wait(lambda: not messaging()["bridge"].get("connected"), "bridge did not notice the broker going away")
        for i in range(5):
            assert ipc(PUB_UID, "publish", "pub", f"sensors/s/{i}", {"i": i})["ok"]
        spooled = [s for s in messaging()["subscriptions"] if s["principal"].startswith("@bridge")]
        assert spooled[0]["queued"] >= 4, spooled  # one may already be in flight
        sh("docker", "start", BROKER)
        got = lan_sub("device1/sensors/#", 5, timeout=60)
        assert [json.loads(line.split(" ", 1)[1])["i"] for line in got] == [0, 1, 2, 3, 4], got
        check("broker down: 5 publications spooled on disk, delivered in order after reconnect")

        # Spool exhaustion: refused and counted beyond the limit; nothing silently lost.
        sh("docker", "stop", BROKER)
        wait(lambda: not messaging()["bridge"].get("connected"), "bridge did not notice the broker going away")
        accepted = refused = 0
        for i in range(SPOOL_LIMIT + 5):
            if ipc(PUB_UID, "publish", "pub", f"sensors/x/{i}", {"n": i})["ok"]:
                accepted += 1
            else:
                refused += 1
        bridge_sub = [s for s in messaging()["subscriptions"] if s["principal"].startswith("@bridge")][0]
        assert refused >= 5 and bridge_sub["refused"] >= 5 and accepted <= SPOOL_LIMIT + 1, (accepted, refused, bridge_sub)

        # Daemon restart with the spool full: it survives.
        control("/restart")
        sh("docker", "start", BROKER)
        got = lan_sub("device1/sensors/#", accepted, timeout=90)
        numbers = [json.loads(line.split(" ", 1)[1])["n"] for line in got]
        assert numbers == list(range(accepted)), numbers
        assert not lan_sub("device1/sensors/#", 1, timeout=3), "more arrived than were accepted"
        check(f"spool exhausted: {accepted} accepted, {refused} refused and counted; after a daemon restart exactly the "
              f"{accepted} spooled publications arrived in order")

        # Inbound backpressure: a full subscriber queue leaves LAN messages with the broker.
        for i in range(SPOOL_LIMIT + 3):
            lan_pub(f"lan/commands/c{i}", json.dumps({"c": i}))
        wait(lambda: any(m["refused"] > 0 for m in messaging()["bridge"]["mappings"] if m["source"] == "LocalMqtt"),
             "the bridge did not report inbound refusals")
        received = []
        deadline = time.time() + 120
        while len(received) < SPOOL_LIMIT + 3 and time.time() < deadline:
            message = ipc(SUB_UID, "receive", "sub", "lan/commands/#")["data"]
            if message:
                received.append(message["payload"]["c"])
                ipc(SUB_UID, "ack", "sub", "lan/commands/#", message["id"])
            else:
                time.sleep(0.5)
        assert sorted(set(received)) == list(range(SPOOL_LIMIT + 3)), received
        check(f"inbound backpressure: {SPOOL_LIMIT + 3} LAN messages into a {SPOOL_LIMIT}-message queue; refusals counted, "
              "none lost (redelivered by the broker after draining; duplicates possible at least once)")

        evidence = {"ticket": "SF-032", "head": sh("git", "-C", str(ROOT), "rev-parse", "HEAD"), "broker": BROKER_IMAGE,
                    "broker_version": version[0], "checks": checks,
                    "fixture": {"file": "interfaces/compat/pubsub-cases.json", "cases": 53,
                                "executed_by": "make edge-test: pubsub::tests::shared_topic_filter_match_and_authorization_cases"}}
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
    except Exception:
        print(sh("docker", "logs", "--tail", "40", RUNNER, check=False))
        try:
            print(control("/log").get("data", ""))
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
