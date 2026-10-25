#!/usr/bin/env python3
"""Restart the real daemon in privileged-helper mode over firmware updates
left in every in-flight state, and check none becomes a fabricated Failed.

Journal entries without a status, with their firmware spool state:
  0x301 success result already written      -> Applied, result acknowledged
  0x302 claim held by a live helper         -> pending; Applied once it finishes
  0x303 abandoned claim (helper died)       -> pending, then swept: indeterminate,
                                               never reported, survives restarts
  0x304 unclaimed request                   -> withdrawn at startup, never runs: Failed
  0x305 helper mode, nothing submitted      -> Failed (updater never ran)
  0x306 older journal entry, no request     -> unknown (no explicit command result)
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for


def request_id(deployment):
    return hashlib.sha256(deployment.encode()).hexdigest()


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-firmware-restart-") as temporary:
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
            spool = state_dir / "firmware"
            requests, results = spool / "requests", spool / "results"
            marker = root / "updater-ran"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[apply]\ncommand = "/bin/false"\ntimeout_seconds = 2\nmode = "privileged-helper"\n'
                f'firmware_spool = {json.dumps(str(spool))}\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\nevent_discovery_seconds = 60\n')
            helper_config = root / "firmware.toml"
            helper_config.write_text(f'command = {json.dumps("/bin/sh -c " + chr(39) + "echo ran >> " + str(marker) + chr(39))}\n'
                f'timeout_seconds = 2\nspool_dir = {json.dumps(str(spool))}\nwork_dir = {json.dumps(str(root / "helper-work"))}\n')
            journal_path = state_dir / "state.json"

            def state():
                return json.loads(journal_path.read_text()) if journal_path.exists() else {}

            def outcome(id):
                return state().get("deployments", {}).get(id, {})

            def reported():
                return sorted(id for id, _ in sim.reports)

            with (root / "daemon.log").open("w") as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

                def stop(process):
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=10)
                    assert process.returncode == 0

                def helper():
                    subprocess.run([str(binary), "--firmware-helper", str(helper_config)], stdout=log, stderr=log, check=True)

                # On-chain records for reporting; enrollment's baseline skips them.
                for id in ("0x301", "0x302", "0x303", "0x304", "0x305", "0x306"):
                    sim.add(id)
                # Enrollment creates the journal; then simulate a run interrupted with
                # firmware updates in flight.
                process = start()
                wait_for(lambda: state().get("initialized"), "enrollment did not establish cursor", process)
                stop(process)
                process = None
                journal = state()
                for id in ("0x301", "0x302", "0x303", "0x304", "0x305"):
                    journal["deployments"][id] = {"status": None, "reported": False, "error": None,
                                                  "firmware_request": request_id(id)}
                journal["deployments"]["0x306"] = {"status": None, "reported": False, "error": None}
                journal_path.write_text(json.dumps(journal))
                requests.mkdir(parents=True)
                results.mkdir(parents=True)

                def result(id, ok, message, interrupted=False):
                    rid = request_id(id)
                    (results / f"{rid}.json").write_text(json.dumps(
                        {"id": rid, "ok": ok, "message": message, "interrupted": interrupted}))

                def stage(id, suffix):
                    rid = request_id(id)
                    (requests / f"{rid}.artifact").write_bytes(b"image")
                    (requests / f"{rid}.{suffix}").write_text(json.dumps(
                        {"id": rid, "sha256": hashlib.sha256(b"image").hexdigest()}))
                    return requests / f"{rid}.{suffix}"

                result("0x301", True, "applied")
                live = open(stage("0x302", "claimed"))
                fcntl.flock(live, fcntl.LOCK_EX)  # A helper still running the updater.
                stage("0x303", "claimed")  # Its helper died: nobody holds the lock.
                stage("0x304", "json")  # Published but never claimed.

                process = start()
                wait_for(lambda: reported() == ["0x301", "0x304", "0x305"],
                         "startup did not settle and report the definite outcomes", process)
                assert outcome("0x301")["status"] == 1
                assert not (results / f"{request_id('0x301')}.json").exists(), "collected result not acknowledged"
                for id in ("0x304", "0x305"):
                    assert outcome(id)["status"] == 2, (id, outcome(id))
                assert not (requests / f"{request_id('0x304')}.json").exists(), "unclaimed request not withdrawn"
                for id in ("0x302", "0x303", "0x306"):
                    assert outcome(id)["status"] is None and not outcome(id)["reported"], (id, outcome(id))

                # Warm idle caching first: helper completion changes only local
                # spool files and must not wait for an event scan or chain change.
                wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= 2,
                         "firmware recovery did not reach idle batch polling", process)
                # The path unit's next run sweeps the abandoned claim and skips the live one.
                helper()
                assert not marker.exists(), "a swept or withdrawn request must never run the updater"
                wait_for(lambda: outcome("0x303").get("indeterminate"), "interrupted result was not collected", process)
                assert outcome("0x303")["status"] is None and "interrupted" in outcome("0x303")["error"]
                assert outcome("0x302")["status"] is None

                # The live helper finishes: its result is reconciled and reported.
                result("0x302", True, "applied")
                fcntl.flock(live, fcntl.LOCK_UN)
                live.close()
                for suffix in ("claimed", "artifact"):  # As the helper cleans up after answering.
                    (requests / f"{request_id('0x302')}.{suffix}").unlink()
                wait_for(lambda: outcome("0x302").get("status") == 1 and outcome("0x302").get("reported"),
                         "late helper result was not reconciled", process)
                stop(process)
                process = None

                # Indeterminate stays indeterminate across another restart.
                batches = sim.reads.get("BatchGetObjects", 0)
                process = start()
                # Startup validation followed by at least two actual idle polls.
                wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= batches + 2, "restarted daemon did not poll", process)
                stop(process)
                process = None
                assert outcome("0x303")["status"] is None and outcome("0x303")["indeterminate"]
                assert reported() == ["0x301", "0x302", "0x304", "0x305"], sim.reports
                assert dict(sim.reports) == {"0x301": 1, "0x302": 1, "0x304": 2, "0x305": 2}
                assert not list(requests.iterdir()), list(requests.iterdir())
                assert not sim.errors, sim.errors
            print("PASS: restart reconciles a written result and a live claim instead of reporting Failed")
            print("PASS: abandoned claim becomes indeterminate (never reported, survives restart); "
                  "unclaimed/unsubmitted entries settle as Failed; legacy direct interruption stays unknown without running the updater")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        server.shutdown()
        shim.kill()
        shim.wait()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
