#!/usr/bin/env python3
"""SF-085 service health in telemetry and alerts, with the real daemon. Run as
root in a disposable, network-isolated container (make test-service-health):
root only creates real, distinct component UIDs for SO_PEERCRED. Local chain
simulator, a stand-in telemetry ingress, a minimal MQTT 3.1.1 broker, a
failing stream-export destination and a shadow service; no keys, gas, public
network or hardware.

  healthy baseline     -> schema-2 snapshot, bridge connected, no alerts
  fill a queue         -> a subscriber stops acknowledging; its QoS 1 queue
                          fills and publications are refused (backpressure)
  stop the broker      -> the bridge disconnects and stays down
  fail an export       -> the destination answers 503; a full overwrite stream
                          retires messages before they are exported
  shadow service down, -> sync fails repeatedly; after it returns, a key changed
  edits on both sides     on both sides is settled as a conflict
  recovery             -> broker back, queue drained, export and sync succeed:
                          the alerts resolve and the device is healthy again

The alerts are derived from the captured signed envelopes by the real indexer
(`suifleet-indexer telemetry-alerts`), every envelope is verified by the dApp's
verifier, and none carries a payload, topic, export URL or credential.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER, SUBSCRIBER = 64001, 64002
INTERVAL = 10  # The shortest heartbeat interval the daemon accepts.
QUEUE_LIMIT = 5
# Strings that must never leave the device in telemetry.
SECRETS = ["plant-7/valves", "SECRET-PAYLOAD", "export-secret", "shadow-secret", "ingest-private-path", "lan-secret-topic",
           "local-mode", "cloud-mode"]
ALL_KINDS = {"messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit", "stream-data-lost",
             "export-failing", "shadow-sync-failing", "shadow-conflict"}


def wait_for(condition, message, process=None, timeout=25, every=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(every)
    raise AssertionError(message)


class Broker:
    """Minimal MQTT 3.1.1 broker: CONNACK, SUBACK, PUBACK and PINGRESP. Enough
    for the bridge's connection state; Mosquitto interop is SF-032's test."""

    def __init__(self):
        self.port, self.connects, self.listener, self.clients, self.lock = None, 0, None, [], threading.Lock()

    def start(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port or 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        threading.Thread(target=self.accept, args=(self.listener,), daemon=True).start()

    def stop(self):
        try:
            self.listener.shutdown(socket.SHUT_RDWR)  # Wakes the blocked accept().
        except OSError:
            pass
        self.listener.close()
        with self.lock:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
            self.clients = []

    def accept(self, listener):
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(client)
            threading.Thread(target=self.serve, args=(client,), daemon=True).start()

    @staticmethod
    def read(client, n):
        data = b""
        while len(data) < n:
            chunk = client.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def serve(self, client):
        try:
            while True:
                first = self.read(client, 1)[0]
                length, multiplier = 0, 1
                while True:
                    byte = self.read(client, 1)[0]
                    length += (byte & 0x7F) * multiplier
                    multiplier *= 128
                    if not byte & 0x80:
                        break
                body = self.read(client, length)
                kind = first >> 4
                if kind == 1:  # CONNECT
                    self.connects += 1
                    client.sendall(bytes([0x20, 2, 0, 0]))
                elif kind == 8:  # SUBSCRIBE: grant QoS 1 for each filter
                    rest, codes = body[2:], []
                    while rest:
                        size = int.from_bytes(rest[:2], "big")
                        rest = rest[3 + size:]
                        codes.append(1)
                    client.sendall(bytes([0x90, 2 + len(codes)]) + body[:2] + bytes(codes))
                elif kind == 3 and (first >> 1) & 3:  # PUBLISH at QoS 1
                    size = int.from_bytes(body[:2], "big")
                    client.sendall(bytes([0x40, 2]) + body[2 + size:4 + size])
                elif kind == 12:  # PINGREQ
                    client.sendall(bytes([0xD0, 0]))
        except (EOFError, OSError):
            pass


class Http:
    """A local HTTP endpoint whose POST handler is `handle(path, headers, body) -> (status, json)`."""

    def __init__(self, handle):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                status, value = handle(self.path, self.headers, body)
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


def run(evidence=None):
    assert os.geteuid() == 0, "run in a disposable root container (make test-service-health)"
    passed = []
    import builtins
    real_print = builtins.print

    def print(*args):  # noqa: A001 - record PASS lines for the receipt
        text = " ".join(map(str, args))
        if text.startswith("PASS: "):
            passed.append(text[6:])
        real_print(*args, flush=True)
    binary = ROOT / "suifleet-edge/target/debug/suifleet"
    indexer = ROOT / "suifleet-indexer/target/debug/suifleet-indexer"
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

    def chain_rpc(path, headers, body):
        request = json.loads(body)
        try:
            return 200, dict(jsonrpc="2.0", id=request["id"], result=sim.rpc(request["method"], request["params"]))
        except Exception as error:
            return 200, dict(jsonrpc="2.0", id=request["id"], error=dict(code=-32000, message=str(error)))

    chain = Http(chain_rpc)
    shim, rpc_url = start_grpc(chain.url)

    envelopes, lock = [], threading.Lock()

    def ingress(path, headers, body):
        assert path == "/fleet/v1/telemetry", path
        messages = json.loads(body)["messages"]
        with lock:
            envelopes.extend(messages)
        return 200, {"results": [{"status": "accepted"} for _ in messages]}

    telemetry = Http(ingress)
    export = {"mode": "failing", "batches": 0}

    def destination(path, headers, body):
        assert path == "/ingest-private-path" and headers["authorization"] == "Bearer export-secret", (path, dict(headers))
        if export["mode"] == "failing":
            return 503, {}
        export["batches"] += 1
        return 200, {}

    exports = Http(destination)
    cloud = {"mode": "ok", "shadows": {}}

    def shadow_service(path, headers, body):
        if cloud["mode"] == "down":
            return 503, {}
        request = json.loads(body)
        version, document = cloud["shadows"].get(request["shadow"], [0, None])
        if request["op"] == "get":
            return 200, {"version": version, "document": document}
        if request["expected_version"] != version:
            return 409, {"version": version}
        cloud["shadows"][request["shadow"]] = [version + 1, request["document"]]
        return 200, {"version": version + 1}

    shadows = Http(shadow_service)
    broker = Broker()
    broker.start()

    with tempfile.TemporaryDirectory(prefix="sf085-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        address = subprocess.run([str(binary), "--derive-address", str(key)], text=True, capture_output=True, check=True).stdout.strip()
        for name, secret in (("export.token", "export-secret"), ("shadow.token", "shadow-secret")):
            (root / name).write_text(secret + "\n")
            (root / name).chmod(0o600)
        (root / "deployment.json").write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
            chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
            type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": chain.url, "aggregator_url": chain.url})))
        state, socket_path = root / "state", root / "ipc.sock"
        config = root / "config.toml"
        config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{chain.url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\n'
            f'[telemetry]\nurl = "{telemetry.url}/fleet/"\ninterval_seconds = {INTERVAL}\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            f'[ipc.components.pub]\nuid = {PUBLISHER}\noperations = ["publish"]\n'
            '[[ipc.components.pub.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\nresources = ["plant-7/*"]\n'
            f'[ipc.components.sub]\nuid = {SUBSCRIBER}\noperations = ["subscribe", "receive", "ack"]\n'
            '[[ipc.components.sub.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["plant-7/*"]\n'
            f'[pubsub]\nmax_messages_per_subscription = {QUEUE_LIMIT}\n'
            f'[pubsub.bridge]\nbroker = "127.0.0.1:{broker.port}"\nkeep_alive_seconds = 5\n'
            '[[pubsub.bridge.mappings]]\ntopic = "lan-secret-topic/#"\nsource = "LocalMqtt"\ntarget = "Pubsub"\n'
            '[streams]\n[[streams.definitions]]\nname = "metrics"\nmax_size = 2048\nstrategy_on_full = "OverwriteOldestData"\n'
            f'[streams.definitions.export]\nurl = "{exports.url}/ingest-private-path"\nbearer_token_file = "{root / "export.token"}"\n'
            'batch_interval_seconds = 1\nmax_backoff_seconds = 1\n'
            '[shadows]\nthing_name = "pump-1"\nmax_shadows = 4\n'
            f'[shadows.sync]\nurl = "{shadows.url}/v1/shadows"\nbearer_token_file = "{root / "shadow.token"}"\n'
            'shadows = ["cfg"]\ninterval_seconds = 1\nmax_backoff_seconds = 1\n')
        check = subprocess.run([str(binary), "--check-config", str(config)], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        log = (root / "daemon.log").open("a")
        daemon = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)
        wait_for(lambda: socket_path.exists(), "IPC socket did not appear", daemon)

        def ipc(uid, operation, topic="", payload=None, component=""):
            code = ("import os,sys,json; uid=int(sys.argv[1]); "
                    "uid and (os.setgid(uid), os.setuid(uid)); "
                    "sys.path.insert(0, sys.argv[7]); from suifleet_ipc import Client, IpcError\n"
                    "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                    "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
            out = subprocess.run([sys.executable, "-c", code, str(uid), str(socket_path), component, operation, topic, json.dumps(payload),
                                  str(ROOT / "sdk/python")], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        def explained():
            """Every captured envelope through the real indexer's offline check."""
            with lock:
                captured = list(envelopes)
            path = root / "envelopes.json"
            path.write_text(json.dumps(captured))
            out = subprocess.run([str(indexer), "telemetry-alerts", str(path)], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            results = json.loads(out.stdout)
            for result in results:
                assert "error" not in result and result["signer"] == address, result
            return results

        def latest():
            results = explained()
            return results[-1] if results else None

        def snapshot_after(since_seq, condition, message, timeout=6 * INTERVAL):
            found = {}

            def check():
                for result in explained():
                    if result["seq"] > since_seq and condition(result):
                        found["r"] = result
                        return True
                return False
            wait_for(check, message, daemon, timeout=timeout, every=1)
            return found["r"]

        def kinds(result):
            return {a["kind"] for a in result["alerts"]}

        try:
            # Healthy baseline: the bridge is connected and nothing alerts.
            base = snapshot_after(0, lambda r: r["services"]["bridge"]["connected"] and r["health"] == "healthy",
                                  "no healthy schema-2 snapshot with a connected bridge")
            assert base["schema"] == 2 and base["services"]["version"] == 1 and not base["alerts"], base
            assert base["services"]["streams"]["streams"][0]["export"]["consecutive_failures"] == 0
            print(f"PASS: healthy baseline: signed schema-2 snapshot (seq {base['seq']}), bridge connected, no alerts")

            started = latest()["seq"]
            # Fill a queue: the subscriber stops acknowledging.
            assert ipc(SUBSCRIBER, "subscribe", "plant-7/valves", {"qos": 1}, "sub")["ok"]
            for i in range(QUEUE_LIMIT):
                assert ipc(PUBLISHER, "publish", "plant-7/valves", {"cmd": "SECRET-PAYLOAD", "i": i}, "pub")["ok"]
            refused = ipc(PUBLISHER, "publish", "plant-7/valves", {"cmd": "SECRET-PAYLOAD", "i": QUEUE_LIMIT}, "pub")
            assert not refused["ok"], refused
            # Stop the broker.
            broker.stop()
            # Fail an export: 503s while a small overwrite stream retires unexported messages.
            for i in range(40):
                assert ipc(0, "stream_append", "metrics", {"reading": "SECRET-PAYLOAD", "i": i})["ok"]
            # Shadow service down: sync fails; both sides then change the same key.
            assert ipc(0, "shadow_update", "cfg", {"state": {"desired": {"mode": "initial"}}})["ok"]
            wait_for(lambda: cloud["shadows"].get("cfg", [0, None])[1] is not None, "shadow was not synchronized", daemon)
            cloud["mode"] = "down"
            assert ipc(0, "shadow_update", "cfg", {"state": {"desired": {"mode": "local-mode"}}})["ok"]
            version, document = cloud["shadows"]["cfg"]
            cloud["shadows"]["cfg"] = [version + 1, {**document, "desired": {"mode": "cloud-mode"}}]
            print("PASS: queue filled (publication refused), broker stopped, export failing, stream overwriting unexported data, shadow service down")

            # Sustained conditions are judged at two heartbeat intervals.
            degraded = snapshot_after(started, lambda r: {"messaging-backpressure", "bridge-disconnected", "export-failing",
                                                          "shadow-sync-failing"} <= kinds(r),
                                      "sustained queue, bridge, export and sync alerts not observed")
            reasons = " | ".join(degraded["reasons"])
            assert degraded["health"] == "degraded", degraded
            for expected in ("pub/sub queue of sub 100% full", "MQTT bridge disconnected", "stream metrics export failing",
                             'shadow "cfg" sync failing'):
                assert expected in reasons, reasons
            queue = next(q for q in degraded["services"]["messaging"]["queues"] if q["principal"] == "sub")
            assert (queue["queued"], queue["fill_percent"], queue["refused"]) == (QUEUE_LIMIT, 100, 1), queue
            exported = degraded["services"]["streams"]["streams"][0]
            assert exported["export"]["lost"] > 0 and exported["export"]["batches"] == 0, exported
            alerts = {a["kind"]: a for a in degraded["alerts"]}
            assert alerts["export-failing"]["severity"] == "critical" and "503" in alerts["export-failing"]["message"], alerts
            assert all(a["action"] for a in degraded["alerts"])
            cloud["mode"] = "ok"
            seen = set()
            for result in explained():
                if result["seq"] > started:
                    seen |= kinds(result)
            # One-off events (refusals, retired messages, conflicts) alert in the report that first counts them.
            settled = snapshot_after(started, lambda r: "shadow-conflict" in kinds(r), "the shadow conflict was not reported")
            for result in explained():
                if result["seq"] > started:
                    seen |= kinds(result)
            assert seen == ALL_KINDS, sorted(ALL_KINDS - seen)
            assert settled["services"]["shadows"]["sync"][0]["conflicts"] >= 1
            print(f"PASS: observed all {len(ALL_KINDS)} alert kinds from the real daemon's signed reports: {', '.join(sorted(seen))}; "
                  f"sustained ones degrade the device ({len(degraded['reasons'])} reasons), each alert has an operator action")

            # Recovery: everything back; alerts resolve.
            broker.start()
            export["mode"] = "ok"
            while True:
                message = ipc(SUBSCRIBER, "receive", "plant-7/valves", None, "sub")
                if not message["ok"] or message["data"] is None:
                    break
                assert ipc(SUBSCRIBER, "ack", "plant-7/valves", message["data"]["id"], "sub")["ok"]
            recovered_from = latest()["seq"]
            # The bridge reconnects within its backoff (at most 60 s).
            healthy = snapshot_after(recovered_from, lambda r: r["health"] == "healthy" and not r["alerts"],
                                     "the alerts did not resolve after recovery", timeout=12 * INTERVAL)
            assert healthy["services"]["bridge"]["connected"] and healthy["services"]["streams"]["streams"][0]["export"]["lag"] == 0
            assert export["batches"] > 0 and broker.connects >= 2
            print(f"PASS: recovery: broker back, queue drained, export and sync succeed; seq {healthy['seq']} is healthy with no alerts")

            # Nothing private in any envelope; all verify with the dApp's verifier.
            with lock:
                captured = list(envelopes)
            for envelope in captured:
                text = base64.b64decode(envelope["message"]).decode()
                leaked = [s for s in SECRETS if s in text]
                assert not leaked, leaked
            print(f"PASS: none of {len(captured)} signed snapshots contains a payload, topic, export URL path, token or shadow value")
            node_modules = ROOT / "dapp/node_modules"
            if node_modules.exists():
                path = root / "envelopes.json"
                path.write_text(json.dumps(captured))
                verify = subprocess.run(["node", str(ROOT / "scripts/test_telemetry.cjs"), "--verify", str(path), address, DEVICE,
                                         sim.chain, REGISTRY], text=True, capture_output=True)
                assert verify.returncode == 0, verify.stdout + verify.stderr
                print(verify.stdout.strip())
            else:
                real_print("SKIP: dApp verifier (dapp/node_modules missing; run make dapp-install)")
        except BaseException:
            log.flush()
            real_print((root / "daemon.log").read_text()[-6000:])
            raise
        finally:
            daemon.terminate()
            daemon.wait(timeout=30)
            shim.terminate()
    if evidence:
        Path(evidence).write_text(json.dumps({
            "ticket": "SF-085", "head": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip(),
            "checks": passed}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", help="write the passed checks to this JSON file")
    run(parser.parse_args().evidence)
