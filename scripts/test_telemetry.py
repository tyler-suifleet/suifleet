#!/usr/bin/env python3
"""Device telemetry (SF-019) with the real daemon against the local
chain/Walrus simulator and a stand-in telemetry ingress, without keys or gas.

  ingress down          -> snapshots spooled (bounded: oldest dropped, counted)
  daemon restart        -> spool kept; sequence continues above it
  ingress up, `retry`   -> the deferred snapshot and later ones kept, resent in order
                           (after a restart with a larger buffer_max)
  ingress `rejected`    -> that snapshot dropped, never resent
  supervised svc v1     -> inventory lists it running; device healthy once synced
  svc crashes past its  -> degraded, with the reason; the deployment receipt on
  restart budget           chain stays the compact Applied status
  key moved to the      -> the daemon's snapshots are signed by the device signing
  signing service          service (SF-053), which refuses anything but a canonical
                           snapshot of this chain/registry/key
Every captured envelope is then verified by the dApp's verifier (Mysten SDK)
against the device key's address. The real indexer ingress, its checks and
fleet status rules are covered by `make indexer-test`.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import ADDRESS, CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

ROOT = Path(__file__).resolve().parents[1]
INTERVAL = 10  # The shortest heartbeat interval the daemon accepts.


def wait_for(condition, message, process=None, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.1)
    raise AssertionError(message)


class Ingress:
    """Records POST /v1/telemetry batches; answers per `mode`."""

    def __init__(self):
        self.mode = "down"
        self.verdicts = []  # Scripted verdicts for the next envelopes.
        self.received = []  # (status, envelope, snapshot) in arrival order.
        self.batches = []  # Sequence numbers of each request.
        self.lock = threading.Lock()

    def accepted(self):
        with self.lock:
            return [snapshot for status, _, snapshot in self.received if status == "accepted"]

    def handle(self, body):
        # Like the indexer, each envelope is judged on its own; one already
        # stored is a duplicate.
        results = []
        with self.lock:
            stored = {s["seq"] for status, _, s in self.received if status == "accepted"}
            self.batches.append([])
            for envelope in body["messages"]:
                snapshot = json.loads(base64.b64decode(envelope["message"]))
                status = self.verdicts.pop(0) if self.verdicts else "duplicate" if snapshot["seq"] in stored else "accepted"
                self.received.append((status, envelope, snapshot))
                self.batches[-1].append(snapshot["seq"])
                results.append({"status": status, "seq": snapshot["seq"]})
        return {"results": results}


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False
    ingress = Ingress()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/fleet/v1/telemetry":
                if ingress.mode == "down":
                    return self.respond(b"unavailable", 503)
                return self.respond(json.dumps(ingress.handle(request)).encode())
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    signer = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-telemetry-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
                shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            state_dir = root / "state"
            spool = state_dir / "telemetry"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\n'
                'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 1\n'
                f'[telemetry]\nurl = "{url}/fleet/"\ninterval_seconds = {INTERVAL}\nbuffer_max = 2\n')

            def spooled():
                return sorted(int(p.stem) for p in spool.glob("*.json") if p.stem.isdigit())

            def spool_state():
                try:
                    return json.loads((spool / "state.json").read_text())
                except (OSError, ValueError):
                    return {}

            def outcome(id):
                try:
                    return json.loads((state_dir / "state.json").read_text()).get("deployments", {}).get(id, {})
                except (OSError, ValueError):
                    return {}

            def status():
                try:
                    return json.loads((state_dir / "supervised/status.json").read_text()).get("svc", {})
                except (OSError, ValueError):
                    return {}

            with (root / "daemon.log").open("w") as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

                # Ingress down: snapshots accumulate, bounded at two.
                process = start()
                wait_for(lambda: spool_state().get("dropped", 0) >= 1, "a full spool did not drop its oldest snapshot",
                         process, timeout=4 * INTERVAL)
                assert len(spooled()) == 2, spooled()
                assert spooled()[-1] == spool_state()["seq"], (spooled(), spool_state())
                assert not ingress.received
                print("PASS: ingress down: snapshots spooled, bounded at buffer_max, oldest dropped and counted")

                # A supervised component, applied while telemetry is offline.
                sim.add("0x401")
                event = sim.events[-1]["parsedJson"]
                event["component_name"], event["version"] = "svc", "1.0.0"
                sim.recipes[event["recipe_blob_id"]].update(componentName="svc", componentVersion="1.0.0",
                                                            lifecycle={"run": "exec sleep 300"})
                wait_for(lambda: outcome("0x401").get("status") == 1 and status().get("phase") == "running",
                         "svc v1 was not applied and running", process)

                # Restart while still offline: the spool survives, sequence continues.
                before = spooled()
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                assert spooled() == before, (spooled(), before)
                process = start()
                # The oldest file is removed before state.json counts it: wait for both.
                wait_for(lambda: spooled() and spooled()[-1] > before[-1] and spool_state().get("dropped", 0) >= 2,
                         "no snapshot after the restart", process, timeout=2 * INTERVAL)
                assert len(spooled()) == 2 and spool_state()["dropped"] >= 2, (spooled(), spool_state())
                print("PASS: daemon restart kept the spool; sequence continued above it")

                # Ingress up, deferring the oldest envelope once, later rejecting
                # one. (A larger buffer, so the deferred one is not also the
                # oldest dropped when the next snapshot is spooled.)
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                pending, dropped = spooled(), spool_state()["dropped"]
                config.write_text(config.read_text().replace("buffer_max = 2", "buffer_max = 8"))
                ingress.verdicts = ["retry"]
                ingress.mode = "up"
                process = start()
                def verdicts(seq):
                    with ingress.lock:
                        return [status for status, _, snapshot in ingress.received if snapshot["seq"] == seq]

                wait_for(lambda: "accepted" in verdicts(pending[0]), "the deferred snapshot was not resent", process,
                         timeout=3 * INTERVAL)
                assert ingress.received[0][0] == "retry" and ingress.batches[0][0] == pending[0], ingress.batches
                assert verdicts(pending[0]) == ["retry", "accepted"], verdicts(pending[0])
                assert len(ingress.batches) >= 2 and ingress.batches[1][0] == pending[0], "not resent first"
                count = len(ingress.accepted())
                ingress.verdicts = ["rejected"]
                wait_for(lambda: not ingress.verdicts and len(ingress.accepted()) > count,
                         "uploads stopped after a rejection", process, timeout=3 * INTERVAL)
                rejected = next(s["seq"] for status, _, s in ingress.received if status == "rejected")
                wait_for(lambda: not spooled(), "the spool did not drain", process, timeout=2 * INTERVAL)
                assert verdicts(rejected) == ["rejected"], "a rejected snapshot was resent"
                assert all(batch == sorted(set(batch)) for batch in ingress.batches), ingress.batches
                accepted = [s["seq"] for s in ingress.accepted()]
                assert len(accepted) == len(set(accepted)) and set(pending) <= set(accepted), (accepted, pending)
                assert min(accepted) == pending[0], accepted
                assert ingress.accepted()[-1]["dropped"] == dropped >= 2, "the dropped count was not reported"
                print("PASS: ingress up: oldest first; a deferred snapshot kept and resent in order; a rejected one dropped")

                # Inventory and health of the running component.
                def latest():
                    accepted = ingress.accepted()
                    return accepted[-1] if accepted else {}

                wait_for(lambda: latest().get("health") == "healthy", "device never reported healthy", process,
                         timeout=3 * INTERVAL)
                snapshot = latest()
                svc = [c for c in snapshot["components"] if c["name"] == "svc"]
                assert svc and svc[0]["version"] == "1.0.0" and svc[0]["phase"] == "running" and svc[0]["installed"], snapshot
                assert snapshot["device_id"] == DEVICE and snapshot["signer"] == ADDRESS and snapshot["chain_identifier"] == sim.chain
                assert snapshot["reasons"] == [] and snapshot["sync"]["consecutive_failures"] == 0, snapshot
                assert snapshot["pending_receipts"] == 0 and outcome("0x401").get("reported"), snapshot
                print("PASS: snapshot lists svc 1.0.0 running and installed; healthy only once synced, with no reasons")

                # Crash past the restart budget: degraded, with the reason.
                for _ in range(2):
                    pid = status().get("pid")
                    wait_for(lambda: status().get("phase") == "running" and status().get("pid"), "svc not running", process)
                    os.kill(status()["pid"], signal.SIGKILL)
                    wait_for(lambda: status().get("pid") != pid, "svc did not leave its crashed run", process)
                wait_for(lambda: status().get("phase") == "broken", "svc did not exhaust its restart budget", process)
                wait_for(lambda: latest().get("health") == "degraded", "a broken component was not reported", process,
                         timeout=2 * INTERVAL)
                assert any(r.startswith("component svc: broken") for r in latest()["reasons"]), latest()
                assert [c["phase"] for c in latest()["components"] if c["name"] == "svc"] == ["broken"], latest()
                print("PASS: svc broken after crashes: degraded with the reason, never healthy")

                # The same key in the device signing service (SF-053; its
                # development caller rule, this UID: the systemd main-process
                # rule is covered by the unit tests and test-key-isolation-systemd).
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                sock = root / "sign.sock"
                (root / "signer.toml").write_text(
                    f'manifest_path = "deployment.json"\ndevice_cap_id = "{CAP}"\nkeypair_path = {json.dumps(str(key))}\n'
                    f'socket = {json.dumps(str(sock))}\ncaller_uid = {os.getuid()}\n')
                signer = subprocess.Popen([str(binary), "--signer-service", str(root / "signer.toml")], stdout=log, stderr=log)
                wait_for(sock.exists, "the signing service did not listen", signer)
                config.write_text(config.read_text().replace(f'keypair_path = {json.dumps(str(key))}\n', "").replace(
                    "[walrus]\n", f'[device.signer]\nkind = "service"\nsocket = {json.dumps(str(sock))}\n[walrus]\n'))
                assert "keypair_path" not in config.read_text()

                def ask(body):
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.connect(str(sock))
                        connection.sendall((json.dumps(body) + "\n").encode())
                        return json.loads(connection.makefile().readline())

                assert ask({"op": "address"})["address"] == ADDRESS
                for message in [b"any personal message", json.dumps(latest()).encode()]:
                    refused = ask({"op": "sign_telemetry", "message": base64.b64encode(message).decode()})
                    assert "error" in refused and "signature" not in refused, refused
                last = latest()["seq"]
                process = start()
                wait_for(lambda: latest().get("seq", 0) > last, "no snapshot signed by the signing service", process,
                         timeout=3 * INTERVAL)
                assert latest()["signer"] == ADDRESS and latest()["device_id"] == DEVICE, latest()
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None
                signer.terminate()
                signer.wait(timeout=10)
                print("PASS: key in the signing service: snapshots signed through it; arbitrary or non-canonical messages refused")

            # Receipts stay compact on chain; telemetry never went there.
            assert not sim.errors, sim.errors
            assert sorted(id for id, _ in sim.reports) == ["0x401"], sim.reports
            assert len(ingress.received) > len(sim.reports)
            print(f"PASS: one compact on-chain receipt; {len(ingress.received)} telemetry envelopes off chain")

            sys.stdout.flush()
            captured = root / "envelopes.json"
            captured.write_text(json.dumps([envelope for _, envelope, _ in ingress.received]))
            subprocess.run(["node", str(ROOT / "scripts/test_telemetry.cjs"), "--verify", str(captured), ADDRESS, DEVICE,
                            sim.chain, REGISTRY], check=True)
    finally:
        if process and process.poll() is None:
            process.kill()
        if signer and signer.poll() is None:
            signer.kill()
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(ROOT / "suifleet-edge/target/debug/suifleet"))
    run(Path(parser.parse_args().binary))
