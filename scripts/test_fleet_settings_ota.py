#!/usr/bin/env python3
"""Per-fleet cost settings (SF-130) with the real daemon against the local
chain simulator of scripts/test_ota.py / test_budget_ota.py, without keys,
gas or hardware.

The simulator keeps the fleet's `fleet_settings::Settings` record in a
dynamic field of the FleetBudget, as the contract does. Checked, by timing the
daemon's version batches and event scans:

- with no record the device checks at its local poll interval;
- a newer-version record and an out-of-bounds record are refused and the
  settings in force (here the local configuration) stay applied;
- while a deployment is in its advance-notice period inside an open
  maintenance window (a fast-check state of the SF-123 check cadence), a
  valid record's fast checks apply from the next check: the following check
  comes after the fleet's 30 s fast interval, not the local 1 s;
- the owner then turns fast checks off; the daemon picks that up at its next
  check (within one interval) and then waits for the 5-minute routine
  interval, making no chain requests meanwhile;
- the deferred deployment is never applied early.

This verifies daemon integration, not the contract (Move tests,
scripts/test_settings_upgrade.cjs) or a public network.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ota as ota
import test_budget_ota as budget
from deployment_manifest import SETTINGS_TYPES

for _name in SETTINGS_TYPES:
    ota.TYPE_ORIGINS[_name] = ota.PACKAGE  # Introduced by a later upgrade.
KEY = f"{ota.PACKAGE}::fleet_settings::SettingsKey"
VALUE = f"{ota.PACKAGE}::fleet_settings::Settings"
FAST_S, ROUTINE_S = 30, 300


def settings(**changes):
    record = {"version": "1", "check_interval_ms": "1800000", "rollout_check_interval_ms": "60000",
              "health_interval_ms": "86400000", "archive_interval_ms": "604800000", "archive_retention_epochs": 3}
    return record | {k: str(v) if k != "archive_retention_epochs" else v for k, v in changes.items()}


class SettingsSimulator(budget.BudgetSimulator):
    def __init__(self):
        super().__init__()
        self.gas = 1_000_000  # Above the low-water mark: no draws.
        self.settings = None
        self.settings_reads = []  # (monotonic time, record returned)
        self.scans = []
        self.checks = []

    def rpc(self, method, params):
        if method == "shim_read" and params[0] == "BatchGetObjects":
            self.checks.append(time.monotonic())
        if method == "shim_fields":
            return super().rpc(method, params) + [[budget.BUDGET, KEY, "00", VALUE]]
        if method == "suix_getDynamicFieldObject" and params[1]["type"] == KEY:
            assert params[0] == budget.BUDGET, params
            self.settings_reads.append((time.monotonic(), self.settings))
            return {"data": {"content": {"fields": {"value": self.settings}}}}
        if method == "shim_events" and params[0].endswith("::deployment_manager::DeploymentCreated"):
            self.scans.append(time.monotonic())
        return super().rpc(method, params)


def run(binary):
    sim = SettingsSimulator()

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
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                if not isinstance(error, ConnectionError):
                    sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                self.respond(ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = ota.start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-settings-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            applied = root / "applied.jsonl"
            updater = root / "apply.py"
            updater.write_text("with open(" + repr(str(applied)) + ", 'a') as output:\n    output.write('applied\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=ota.PACKAGE, upgrade_cap_id=ota.UPGRADE_CAP, package_version="3", type_origins=ota.TYPE_ORIGINS,
                shared_objects={"registry": {"id": ota.REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{ota.DEVICE}"\ndevice_cap_id = "{ota.CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = {json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds = 2\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\nrollout_poll_seconds = 1\n'
                f'[budget]\nbudget_id = "{budget.BUDGET}"\nlow_water_mist = {budget.LOW_WATER}\n')
            log_path = root / "daemon.log"

            def log():
                return re.sub(r"\x1b\[[0-9;]*m", "", log_path.read_text())

            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def reads_after(t):
                return [at for at in sim.checks if at > t]

            def local_cadence(label):
                """Two more reads, each about one local poll apart."""
                start = time.monotonic()
                ota.wait_for(lambda: len(reads_after(start)) >= 3, f"{label}: daemon stopped local polling", process, timeout=10)
                gaps = [b - a for a, b in zip(reads_after(start), reads_after(start)[1:])]
                assert all(gap < 3 for gap in gaps), (label, gaps)

            def one_batch_per_poll(label):
                # Let the preceding full refresh finish before measuring idle polls.
                time.sleep(0.2)
                before = dict(sim.reads)
                ota.wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= before.get("BatchGetObjects", 0) + 3,
                             f"{label}: version gate stopped", process, timeout=10)
                time.sleep(0.2)
                delta = {kind: n - before.get(kind, 0) for kind, n in sim.reads.items()}
                assert delta["BatchGetObjects"] == 3 and sum(delta.values()) == 3, (label, delta)
                print(f"PASS: {label}: one version batch per idle poll: {delta}")

            try:
                with log_path.open("w") as output:
                    process = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=output, stderr=output)
                    local_cadence("no record")
                    one_batch_per_poll("absent settings")

                    sim.settings = settings(version=2)
                    ota.wait_for(lambda: "unsupported fleet settings version 2" in log(), "newer record not refused", process)
                    local_cadence("newer record")
                    one_batch_per_poll("newer record")
                    sim.settings = None
                    ota.wait_for(lambda: "Fleet settings readable again" in log(), "absent record not recovered", process)
                    sim.settings = settings(check_interval_ms=1000)
                    ota.wait_for(lambda: "fleet check interval out of bounds" in log(), "out-of-bounds record not refused", process)
                    local_cadence("out-of-bounds record")
                    one_batch_per_poll("out-of-bounds record")
                    assert "Fleet settings applied" not in log()
                    print("PASS: newer-version and out-of-bounds records were refused; the settings in force stayed applied")

                    # A deployment in its advance-notice period inside an open
                    # maintenance window: the check cadence uses fast checks.
                    sim.settings = None
                    now_ms = int(time.time() * 1000)
                    sim.controls["0x300"] = {"revision": 1, "state": 0, "not_before_ms": 0, "expires_ms": 0,
                                             "window_start_ms": now_ms - 3_600_000, "window_end_ms": now_ms + 86_400_000,
                                             "notice_ms": 600_000}
                    sim.add("0x300")
                    ota.wait_for(lambda: state().get("deferred", {}).get("0x300", {}).get("reason") == "advance notice",
                                 "notice not observed", process)
                    sim.settings = settings(check_interval_ms=ROUTINE_S * 1000, rollout_check_interval_ms=FAST_S * 1000)
                    changed = time.monotonic()
                    ota.wait_for(lambda: "rollout_check_interval_ms=30000" in log(), "valid record not applied", process)
                    # A settings refresh may finish in the check already in
                    # flight when the owner changes the record. Anchor the
                    # next cadence to that completed batch, not a second read.
                    first = sim.checks[-1]
                    assert abs(first - changed) < 3, "record not read at the next check"
                    ota.wait_for(lambda: len(reads_after(first)) >= 1, "no fast check", process, timeout=FAST_S + 10)
                    fast = reads_after(first)[0] - first
                    assert FAST_S - 1 <= fast <= FAST_S + 4, f"fast check after {fast:.1f}s"
                    print(f"PASS: a valid record applied at the next check; during the notice the next check came {fast:.1f}s later (fleet fast interval {FAST_S}s, local 1s)")

                    # The owner turns fast checks off between two checks.
                    previous = reads_after(first)[0]
                    sim.settings = settings(check_interval_ms=ROUTINE_S * 1000, rollout_check_interval_ms=0)
                    ota.wait_for(lambda: len(reads_after(previous)) >= 1, "change not read within one interval", process, timeout=FAST_S + 10)
                    picked = reads_after(previous)[0]
                    assert picked - previous <= FAST_S + 4, f"change picked up after {picked - previous:.1f}s"
                    ota.wait_for(lambda: "rollout_check_interval_ms=0" in log(), "change not applied", process)
                    quiet = time.monotonic()
                    time.sleep(FAST_S + 10)
                    assert not reads_after(picked) and not [t for t in sim.scans if t > quiet], "checked before the routine interval"
                    print(f"PASS: turning fast checks off was picked up {picked - previous:.1f}s later (within one interval); "
                          f"no chain checks for the next {FAST_S + 10}s of the {ROUTINE_S}s routine interval")

                    assert state()["deferred"]["0x300"]["reason"] == "advance notice" and not applied.exists(), "applied during notice"
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=10)
                    assert process.returncode == 0
                    process = None
            except BaseException:
                print("daemon log tail:\n" + "\n".join(log().splitlines()[-30:]), file=sys.stderr)
                raise
            assert not sim.errors and not sim.refused and not sim.draws, (sim.errors, sim.refused, sim.draws)
            print("PASS: the notice-period deployment was not applied early; no draws or simulator errors")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        shim.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
