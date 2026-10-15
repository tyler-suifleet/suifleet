#!/usr/bin/env python3
"""SF-018 real-daemon pause/cancel/deadline/window races over the gRPC simulator.
No public chain, wallets or hardware. Apply counts prove deferred work never ran.
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
from test_ota import ARTIFACT, CAP, DEVICE, GROUP, Simulator, start_grpc, wait_for
from test_desired_state import manifest


def run(binary):
    sim = Simulator()
    sim.allow_reports = True; sim.fail_execution = False; sim.truncate_next_artifact = False
    gates = {}
    fault = None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, body, status=200):
            self.send_response(status); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if req["method"] == "suix_getDynamicFieldObject" and req["params"][0] == fault and req["params"][1]["type"].endswith("::ControlKey"):
                self.respond(json.dumps({"jsonrpc":"2.0", "id":req["id"], "error":{"message":"injected control read outage"}}).encode())
                return
            try: reply = {"result": sim.rpc(req["method"], req["params"])}
            except Exception as e: sim.errors.append(repr(e)); reply = {"error": {"message": str(e)}}
            self.respond(json.dumps({"jsonrpc": "2.0", "id": req["id"], **reply}).encode())
        def do_GET(self):
            name = self.path.removeprefix("/v1/blobs/")
            if name.startswith("artifact-"):
                gate = gates.get(name)
                if gate:
                    gate[0].set()
                    assert gate[1].wait(40), "test did not release download"
                self.respond(ARTIFACT)
            elif name in sim.recipes: self.respond(json.dumps(sim.recipes[name]).encode())
            else: self.respond(b"absent", 404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-control-") as tmp:
            root = Path(tmp); key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
            url = f"http://127.0.0.1:{server.server_port}"
            (root / "deployment.json").write_text(json.dumps(manifest(sim, url, rpc_url)))
            runs = root / "runs"; hang = root / "hang"; entered = root / "entered"
            updater = root / "apply.py"
            updater.write_text(f"import pathlib,time\npathlib.Path({str(entered)!r}).touch()\nwhile pathlib.Path({str(hang)!r}).exists(): time.sleep(.05)\nwith open({str(runs)!r},'a') as f: f.write('applied\\n')\n")
            cfg = root / "config.toml"
            cfg.write_text('[sui]\nmanifest_path="deployment.json"\n'
                f'[device]\nkeypair_path={json.dumps(str(key))}\ndevice_object_id="{DEVICE}"\ndevice_cap_id="{CAP}"\n'
                f'[apply]\ncommand={json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds=30\n'
                f'[runtime]\nstate_dir={json.dumps(str(root / "state"))}\npoll_seconds=1\nreplay_history=true\n'
                '')
            log = (root / "daemon.log").open("w")
            def start(): return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(cfg)}, stdout=log, stderr=log)
            def stop():
                nonlocal process
                process.terminate(); process.wait(timeout=10); process = None
            def state():
                p = root / "state/state.json"
                return json.loads(p.read_text()) if p.exists() else {}
            def count(): return len(runs.read_text().splitlines()) if runs.exists() else 0
            def deferred(id, reason): return state().get("deferred", {}).get(id, {}).get("reason") == reason
            def outcome(id): return state().get("deployments", {}).get(id, {}).get("status")
            def control(id, **kw):
                previous = sim.controls.get(id, {})
                sim.controls[id] = {"revision": previous.get("revision", 0) + 1, "state": 0,
                    "not_before_ms": 0, "expires_ms": 0, "window_start_ms": 0, "window_end_ms": 0, "notice_ms": 0, **kw}
            def wait(predicate, message): wait_for(predicate, message, process)
            def settle():
                baseline = sim.chain_checks
                wait(lambda: sim.chain_checks >= baseline + 3, "daemon did not finish polls")
                time.sleep(.2)
            def add(id, **kw):
                control(id, **kw); sim.add(id)
                sim.recipes[f"recipe-{id}"]["artifactBlobId"] = f"artifact-{id}"
            def now(): return int(time.time() * 1000)
            try:
                add("0x501", state=1)
                process = start()
                wait(lambda: deferred("0x501", "paused"), "pause not deferred")
                assert count() == 0 and not outcome("0x501")
                stop(); process = start(); settle()
                assert count() == 0 and deferred("0x501", "paused")
                control("0x501")
                wait(lambda: outcome("0x501") == 1, "resumed event lost after cursor/restart")
                assert count() == 1
                print("PASS: pause survives cursor advancement/restart; resume applies exactly once")

                add("0x502", state=2)
                add("0x503", expires_ms=now()-1000)
                wait(lambda: deferred("0x502", "cancelled") and deferred("0x503", "expired"), "offline cancellation/expiry lost")
                stop(); process = start(); settle()
                assert count() == 1
                assert state()["deferred"]["0x503"]["terminal"]
                print("PASS: offline expiry/cancel are durable terminal observations; no updater or false receipt")

                add("0x504", not_before_ms=now()+2500, notice_ms=3500)
                wait(lambda: deferred("0x504", "not yet scheduled"), "not-before ignored")
                notified = state()["deferred"]["0x504"]["notified_ms"]
                stop(); process = start()
                assert state()["deferred"]["0x504"]["notified_ms"] == notified
                wait(lambda: outcome("0x504") == 1, "scheduled notice did not complete")
                assert count() == 2
                print("PASS: scheduled deployment waits for both not-before and durable advance notice")

                for id, action in [("0x505", "pause"), ("0x506", "cancel"), ("0x507", "window"), ("0x508", "expiry")]:
                    download, release = threading.Event(), threading.Event()
                    gates[f"artifact-{id}"] = (download, release)
                    times = {"window_end_ms": now()+6000, "window_start_ms": now()-1000} if action == "window" else {"expires_ms": now()+6000} if action == "expiry" else {}
                    add(id, **times)
                    wait(download.is_set, f"{action} did not reach download")
                    if action == "pause": control(id, state=1)
                    elif action == "cancel": control(id, state=2)
                    else:
                        end = times.get("window_end_ms", times.get("expires_ms"))
                        # WSL may step CLOCK_REALTIME backwards while sleep uses
                        # CLOCK_MONOTONIC. Wait for the actual UTC deadline and
                        # leave clock-adjustment margin before releasing bytes.
                        wait(lambda: now() >= end + 5000, f"{action} UTC deadline did not elapse")
                    release.set()
                    reason = {"pause":"paused", "cancel":"cancelled", "window":"outside maintenance window", "expiry":"expired"}[action]
                    wait(lambda: deferred(id, reason), f"{action} final admission failed")
                    settle(); assert count() == 2 and not outcome(id), (action, state())
                    if action == "pause": control(id, state=2)
                    print(f"PASS: {action} during fetch rechecked before first disruptive work")
                control("0x507", window_start_ms=now()-1000, window_end_ms=now()+30000)
                wait(lambda: outcome("0x507") == 1, "edited window not resumed")
                assert count() == 3
                hang.touch(); entered.unlink(missing_ok=True); add("0x509")
                wait(entered.exists, "updater did not start")
                control("0x509", state=2); hang.unlink()
                wait(lambda: outcome("0x509") == 1, "cancellation killed/failed admitted apply")
                settle(); assert count() == 4 and not sim.errors, sim.errors
                print("PASS: cancellation during admitted apply allows truthful success, without reapply")
                stop()
                sim.memberships.append((GROUP, DEVICE))
                sim.managed.add("0x50b"); add("0x50b", state=1)
                sim.desired[GROUP] = (1, {"app-0x50b": "0x50b"})
                process = start()
                wait(lambda: deferred("0x50b", "paused"), "desired pause ignored")
                stop()
                sim.managed.add("0x50c"); add("0x50c")
                event = sim.events[-1]["parsedJson"]; event["component_name"] = "app-0x50b"
                sim.recipes[event["recipe_blob_id"]]["componentName"] = "app-0x50b"
                sim.desired[GROUP] = (2, {"app-0x50b": "0x50c"}); control("0x50b")
                process = start()
                wait(lambda: outcome("0x50c") == 1, "latest desired revision did not apply")
                settle(); assert count() == 5 and not outcome("0x50b") and not sim.errors
                print("PASS: resumed superseded managed deployment stays deferred; only latest desired revision applies")
                arrived, released = threading.Event(), threading.Event()
                gates["artifact-0x50d"] = (arrived, released)
                add("0x50d")
                wait(arrived.is_set, "outage case did not start preparation")
                fault = "0x50d"; released.set()
                wait(lambda: "Admission unavailable" in (root / "daemon.log").read_text(), "post-download read outage not surfaced")
                assert count() == 5 and not outcome("0x50d")
                fault = None
                wait(lambda: outcome("0x50d") == 1, "read outage did not retry safely")
                assert count() == 6 and not sim.errors
                print("PASS: final control-read outage fails closed without apply/failure receipt; retry applies once")


            except Exception:
                log.flush(); print((root / "daemon.log").read_text()[-16000:], file=sys.stderr); raise
            finally:
                for _, released in gates.values(): released.set()
                hang.unlink(missing_ok=True)
                if process: stop()
                log.close()
    finally:
        shim.terminate(); shim.wait(timeout=10); server.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--binary", type=Path, default=Path("suifleet-edge/target/debug/suifleet").resolve())
    run(parser.parse_args().binary)
