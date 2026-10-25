#!/usr/bin/env python3
"""Remote logs (SF-020) with the real daemon against the local chain/Walrus
simulator and a stand-in log ingress, without keys or gas.

  supervised svc logs a confidential   -> shipped redacted: the overlay value, an
  overlay value, an api key, a raw key    api key and a raw hex key never leave
                                          the device; ordinary lines are kept
  120 more lines, budget 50/interval   -> read over several intervals, in order
  log ingress down                     -> batches spooled on the device
  ingress up                           -> the spool drains; every line arrives once
  daemon restart                       -> cursors persist: nothing is sent twice
  the daemon's own log                 -> shipped as source "daemon"
Every captured batch's signature is verified with Mysten's SDK against the
device key's address. The real indexer's checks, search, bundles and alerts
are covered by `make indexer-test`.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import ADDRESS, CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

ROOT = Path(__file__).resolve().parents[1]
# Every string leaf of an overlay is confidential, whatever its name.
OVERLAY_VALUE = "Ov3rlayValueQ9-brokerlogin"
RAW_KEY = "ab" * 32
# Prints its lines once (a marker in the persistent work directory), so a
# restarted run adds nothing and any duplicate would be a re-send.
RUN = ("if [ ! -e printed ]; then echo 'connecting to broker with " + OVERLAY_VALUE + "'; echo 'api_key=k3y-abcdef0123'; "
       "echo 'loaded " + RAW_KEY + "'; echo 'ordinary line kept'; "
       "i=0; while [ $i -lt 120 ]; do echo tick $i; i=$((i+1)); done; touch printed; else echo restarted; fi; "
       "touch ready; exec sleep 600")


def wait_for(condition, message, process=None, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.2)
    raise AssertionError(message)


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False
    state = {"logs_up": False, "envelopes": [], "seen": set()}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def respond(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/fleet/v1/telemetry":
                return self.respond(json.dumps({"results": [{"status": "accepted"} for _ in body["messages"]]}).encode())
            if self.path == "/fleet/v1/logs":
                with lock:
                    if not state["logs_up"]:
                        return self.respond(b"down", 503)
                    results = []
                    for envelope in body["messages"]:
                        message = base64.b64decode(envelope["message"])
                        seq = json.loads(message)["seq"]
                        if seq in state["seen"]:
                            results.append({"status": "duplicate"})
                        else:
                            state["seen"].add(seq)
                            state["envelopes"].append(envelope)
                            results.append({"status": "accepted"})
                return self.respond(json.dumps({"results": results}).encode())
            try:
                response = {"jsonrpc": "2.0", "id": body["id"], "result": sim.rpc(body["method"], body["params"])}
            except Exception as error:
                sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def batches():
        with lock:
            return [json.loads(base64.b64decode(e["message"])) for e in state["envelopes"]]

    def lines(source=None):
        return [r["line"] for b in batches() for r in b["records"] if source is None or r["source"] == source]

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-logs-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            overlays = root / "overlays"
            overlays.mkdir(mode=0o700)
            (overlays / "svc.json").write_text(json.dumps({"/broker_login": OVERLAY_VALUE}))
            (overlays / "svc.json").chmod(0o600)
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
                shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            state_dir = root / "state"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\nevent_discovery_seconds = 1\ncomponent_startup_seconds = 1\n'
                f'[components]\nconfiguration_overlay_dir = {json.dumps(str(overlays))}\n'
                f'[telemetry]\nurl = "{url}/fleet/"\ninterval_seconds = 10\n'
                '[logs]\ninterval_seconds = 10\nmax_lines_per_interval = 50\nbuffer_max = 64\n')

            def spooled():
                spool = state_dir / "logs" / "spool"
                return sorted(p for p in spool.glob("*.json") if p.name != "state.json") if spool.exists() else []

            def journal():
                try:
                    return json.loads((state_dir / "state.json").read_text())
                except (OSError, ValueError):
                    return {}

            with (root / "daemon.log").open("w") as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

                process = start()
                wait_for(lambda: journal().get("initialized"), "daemon did not start", process)
                sim.add("0x501")
                event = sim.events[-1]["parsedJson"]
                event["component_name"] = "svc"
                sim.recipes[event["recipe_blob_id"]].update(componentName="svc", lifecycle={"run": RUN, "readiness": "test -e ready"})
                wait_for(lambda: journal().get("deployments", {}).get("0x501", {}).get("status") == 1, "svc was not applied", process)

                # Ingress down: batches accumulate on the device.
                wait_for(lambda: len(spooled()) >= 2, "no log batches were spooled while the ingress was down", process, timeout=60)
                assert not state["envelopes"]
                print(f"PASS: log ingress down: {len(spooled())} signed batches spooled on the device")

                with lock:
                    state["logs_up"] = True
                wait_for(lambda: "tick 119" in lines("component:svc"), "the backlog did not drain", process, timeout=120)
                svc = lines("component:svc")
                ticks = [line for line in svc if line.startswith("tick ")]
                assert ticks == [f"tick {i}" for i in range(120)], f"lines out of order or duplicated: {ticks[:5]}…"
                assert "ordinary line kept" in svc
                assert "connecting to broker with [REDACTED]" in svc, svc[:4]
                assert "api_key=[REDACTED]" in svc and "loaded [REDACTED]" in svc, svc[:4]
                raw = json.dumps(state["envelopes"]) + "".join(json.dumps(b) for b in batches())
                for value in (OVERLAY_VALUE, "k3y-abcdef0123", RAW_KEY):
                    assert value not in raw, f"{value[:6]}… left the device"
                assert sum(b["redactions"] for b in batches()) >= 3
                assert all(len(b["records"]) <= 50 for b in batches()), "a batch exceeded the per-interval line budget"
                daemon = lines("daemon")
                assert any("Deployment finished" in line or "Component installed" in line for line in daemon), daemon[:5]
                print(f"PASS: {len(svc)} component lines shipped in order over several intervals (budget 50); the overlay value, "
                      "an api key and a raw hex key were redacted on the device; the daemon's own log shipped too")

                # Restart: cursors persist, nothing is sent again.
                before = len(lines())
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=30)
                process = start()
                wait_for(lambda: journal().get("initialized"), "daemon did not restart", process)
                time.sleep(25)
                wait_for(lambda: not spooled(), "the spool did not drain after the restart", process, timeout=60)
                ticks = [line for line in lines("component:svc") if line.startswith("tick ")]
                assert len(ticks) == len(set(ticks)) == 120, f"lines were sent twice after a restart ({len(ticks)})"
                assert len(lines()) > before, "the restarted daemon shipped nothing (its own startup lines expected)"
                assert "restarted" in lines("component:svc"), "the restarted component's new line was not shipped"
                print("PASS: after a daemon restart the cursors held: no component line was sent twice")

                process.send_signal(signal.SIGTERM)
                process.wait(timeout=30)
                process = None

            captured = root / "envelopes.json"
            captured.write_text(json.dumps(state["envelopes"]))
            subprocess.run(["node", str(ROOT / "scripts/test_logs_verify.cjs"), str(captured), ADDRESS], check=True)
            assert not sim.errors, sim.errors
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=ROOT / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
