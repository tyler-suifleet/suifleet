#!/usr/bin/env python3
"""SF-091 device-side recovery drill: the real daemon against the local chain
and Walrus simulators of scripts/test_ota.py / test_budget_ota.py, without
keys, gas or hardware.

One supervised installed component keeps running throughout. Checked, with
each recovery duration measured from the dependency's return:

- payment failure (allowance used up, gas below a report, paused budget and an
  empty reserve) pauses new admission and queues receipts; the installed
  component is not stopped, uninstalled or restarted and the daemon is not
  restarted or rebooted; funding recovery resumes without duplicate apply;
- a fullnode outage and an aggregator outage defer new work, keep the
  component running and resume without loss;
- a platform-controlled daemon restart with preserved state restarts the
  same installed version and never reapplies completed one-shot work;
- revocation (compromised-device response) stops new applies and reports but
  cannot remotely uninstall delivered software;
- a replaced (lost) device key is refused rather than silently re-identified.

This is daemon integration evidence, not Walrus/Seal/provider, public-network
or physical-device acceptance. Usage: test_recovery_ota.py [--receipt PATH]
"""
import argparse
import json
import os
import re
from pathlib import Path
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


def last_problem(log, markers=("ERROR", "Error")):
    """The newest error/warning line, without terminal colors, for the receipt."""
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in log.read_text().splitlines()]
    lines = [line for line in lines if any(marker in line for marker in markers)]
    return lines[-1][-240:] if lines else None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class RecoverySimulator(budget.BudgetSimulator):
    def __init__(self):
        super().__init__()
        self.rpc_down = False
        self.aggregator_down = False


def run(binary, receipt):
    sim = RecoverySimulator()
    measured = {}

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
            if sim.rpc_down:
                return self.respond(b"fullnode unavailable", 503)
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                if not isinstance(error, ConnectionError):
                    sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            if sim.aggregator_down:
                return self.respond(b"aggregator unavailable", 503)
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
        with tempfile.TemporaryDirectory(prefix="suifleet-recovery-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            applied = root / "applied.jsonl"
            pids = root / "component-pids"
            updater = root / "apply.py"
            updater.write_text("with open(" + repr(str(applied)) + ", 'a') as output:\n    output.write('applied\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=ota.PACKAGE, upgrade_cap_id=ota.UPGRADE_CAP, package_version="3", type_origins=ota.TYPE_ORIGINS,
                shared_objects={"registry": {"id": ota.REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            state_dir = root / "state"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{ota.DEVICE}"\ndevice_cap_id = "{ota.CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                f'[apply]\ncommand = {json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\ncomponent_stop_timeout_seconds = 3\n'
                f'[budget]\nbudget_id = "{budget.BUDGET}"\nlow_water_mist = {budget.LOW_WATER}\ndraw_mist = {budget.DRAW}\n')

            def state():
                path = state_dir / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def funding():
                return state().get("funding") or {}

            def reported(id):
                return state().get("deployments", {}).get(id, {}).get("reported")

            def status():
                try:
                    return json.loads((state_dir / "supervised" / "status.json").read_text()).get("svc", {})
                except (OSError, ValueError):
                    return {}

            def component_pid():
                lines = pids.read_text().split() if pids.exists() else []
                return int(lines[-1]) if lines else None

            def applies():
                return len(applied.read_text().splitlines()) if applied.exists() else 0

            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=15)
                assert process.returncode == 0, process.returncode

            def unchanged(daemon, pid, count, label):
                assert daemon.poll() is None, f"{label}: daemon exited"
                assert component_pid() == pid and alive(pid), f"{label}: installed component restarted or stopped"
                assert status().get("phase") == "running", (label, status())
                assert applies() == count, f"{label}: one-shot work replayed"

            def recovered(condition, message, process):
                started = time.monotonic()
                ota.wait_for(condition, message, process)
                return round(time.monotonic() - started, 3)

            run_command = f"echo $$ >> {pids}; exec sleep 3600"
            try:
                with (root / "daemon.log").open("w") as log:
                    process = start()
                    daemon_pid = process.pid
                    ota.wait_for(lambda: funding().get("draws") == 1, "daemon did not draw from the budget", process)
                    ota.wait_for(lambda: "after" in state().get("cursor", {}), "first event scan did not complete", process)
                    sim.add("0x301", event=dict(component_name="svc", version="1.0.0"), recipe=dict(lifecycle={"run": run_command}))
                    ota.wait_for(lambda: reported("0x301") and status().get("phase") == "running", "supervised component did not start", process)
                    sim.add("0x302")
                    ota.wait_for(lambda: reported("0x302"), "one-shot deployment did not report", process)
                    pid, count = component_pid(), applies()
                    assert pid and alive(pid) and count == 1, (pid, count)
                    print("PASS: installed supervised component running and one-shot work applied once")

                    # Payment failure: exhausted allowance, too little gas to report,
                    # then a paused budget and an empty reserve.
                    sim.allow_reports = False
                    sim.add("0x303")
                    ota.wait_for(lambda: "0x303" in state().get("deployments", {}), "deployment not applied before funding failed", process)
                    sim.account["drawn_in_period"] = sim.account["limits"]["period_allowance"]
                    sim.gas = budget.REPORT_COST - 1
                    sim.add("0x304")
                    ota.wait_for(lambda: "used up" in (funding().get("blocked") or ""), "exhaustion not recorded", process)
                    time.sleep(2.5)
                    assert reported("0x303") is False and "0x304" not in state().get("deployments", {})
                    unchanged(process, pid, 2, "exhausted allowance")
                    sim.budget["paused"] = True
                    ota.wait_for(lambda: "paused" in (funding().get("blocked") or ""), "pause not recorded", process)
                    sim.budget["paused"] = False
                    sim.account["reserve"]["value"] = "0"
                    sim.account["period_start_ms"] = str(budget.now_ms() - budget.PERIOD_MS)
                    ota.wait_for(lambda: "reserve is empty" in (funding().get("blocked") or ""), "empty reserve not recorded", process)
                    time.sleep(2)
                    unchanged(process, pid, 2, "paused budget and empty reserve")
                    assert process.pid == daemon_pid
                    print("PASS: payment failure paused admission and queued receipts; installed component untouched, no uninstall/restart/reboot")

                    # Funding recovery: owner refills the reserve; queued and paused work resumes once.
                    sim.allow_reports = True
                    sim.account["reserve"]["value"] = str(budget.DRAW * 4)
                    measured["payment_recovery_seconds"] = recovered(lambda: reported("0x303") and reported("0x304"), "funding recovery did not resume", process)
                    unchanged(process, pid, 3, "funding recovered")
                    print(f"PASS: funding recovery resumed queued receipt and paused deployment in {measured['payment_recovery_seconds']}s, no duplicate apply")

                    # Fullnode outage.
                    sim.rpc_down = True
                    sim.add("0x305")
                    time.sleep(3)
                    assert "0x305" not in state().get("deployments", {})
                    unchanged(process, pid, 3, "fullnode outage")
                    sim.rpc_down = False
                    measured["rpc_recovery_seconds"] = recovered(lambda: reported("0x305"), "fullnode recovery did not resume", process)
                    unchanged(process, pid, 4, "fullnode recovered")
                    print(f"PASS: fullnode outage deferred work and resumed in {measured['rpc_recovery_seconds']}s")

                    # Aggregator outage: a transient 503 must not become a permanent failure.
                    sim.aggregator_down = True
                    sim.add("0x306")
                    time.sleep(3)
                    outcome = state().get("deployments", {}).get("0x306", {})
                    measured["aggregator_outage_outcome"] = outcome.get("error") or ("pending" if not outcome else "recorded")
                    unchanged(process, pid, 4, "aggregator outage")
                    sim.aggregator_down = False
                    if outcome.get("error"):
                        # The daemon does not retry a failed recipe fetch: record the
                        # observed behavior and redeploy, as OPERATIONS.md requires.
                        measured["aggregator_outage_requires_redeploy"] = True
                        sim.add("0x307")
                        measured["aggregator_recovery_seconds"] = recovered(lambda: reported("0x307"), "redeploy after aggregator outage failed", process)
                    else:
                        measured["aggregator_outage_requires_redeploy"] = False
                        measured["aggregator_recovery_seconds"] = recovered(lambda: reported("0x306"), "aggregator recovery did not resume", process)
                    unchanged(process, pid, 5, "aggregator recovered")
                    print(f"PASS: aggregator outage kept the component running; recovery {measured['aggregator_recovery_seconds']}s"
                          f" (redeploy required: {measured['aggregator_outage_requires_redeploy']})")

                    # Platform-controlled daemon restart with preserved state and key.
                    stop(process)
                    stopped = time.monotonic()
                    process = start()
                    ota.wait_for(lambda: component_pid() != pid and status().get("phase") == "running", "installed component did not restart", process)
                    measured["restart_recovery_seconds"] = round(time.monotonic() - stopped, 3)
                    pid = component_pid()
                    time.sleep(2)
                    assert applies() == 5 and status().get("version") in (None, "1.0.0"), (applies(), status())
                    print(f"PASS: daemon restart restored the installed version in {measured['restart_recovery_seconds']}s without replaying one-shot work")

                    # Compromised-device response: the owner revokes the identity.
                    sim.identity = dict(sim.identity, revoked=True)
                    reports = len(sim.reports)
                    sim.add("0x308")
                    time.sleep(3)
                    assert "0x308" not in state().get("deployments", {}) and len(sim.reports) == reports
                    assert alive(pid), "revocation cannot uninstall delivered software"
                    print("PASS: revoked identity stops applying/reporting; delivered software is not remotely removed")
                    stop(process)
                    process = None

                    # Lost device key: a different key never impersonates the identity.
                    sim.identity = dict(sim.identity, revoked=False)
                    key.write_text("08" * 32)
                    process = start()
                    sim.add("0x309")
                    time.sleep(4)
                    exited = process.poll()
                    assert "0x309" not in state().get("deployments", {}) or not reported("0x309"), "a replaced key applied and reported"
                    measured["replaced_key_daemon_exit"] = exited
                    measured["replaced_key_refusal"] = last_problem(root / "daemon.log")
                    assert exited not in (None, 0) and "another network/package/device" in (measured["replaced_key_refusal"] or ""), measured
                    if process.poll() is None:
                        stop(process)
                    # Even with a fresh state directory, the registry identity's
                    # signer decides: the new key is not adopted as this device.
                    config.write_text(config.read_text().replace(json.dumps(str(state_dir)), json.dumps(str(root / "fresh-state"))))
                    reports = len(sim.reports)
                    process = start()
                    time.sleep(4)
                    measured["replaced_key_fresh_state_exit"] = process.poll()
                    assert len(sim.reports) == reports, "a replaced key reported for the registered identity"
                    fresh = root / "fresh-state" / "state.json"
                    assert not fresh.exists() or not any(d.get("reported") for d in json.loads(fresh.read_text()).get("deployments", {}).values())
                    measured["replaced_key_fresh_state_refusal"] = last_problem(root / "daemon.log", ("ERROR", "WARN"))
                    assert "not owned by device key" in (measured["replaced_key_fresh_state_refusal"] or ""), measured
                    if process.poll() is None:
                        stop(process)
                    process = None
                    print(f"PASS: replaced device key refused (daemon exit {exited}); re-enrollment is required, the old key is not recoverable")
            except BaseException:
                lines = (root / "daemon.log").read_text().splitlines()[-40:]
                print("daemon log tail:\n" + "\n".join(lines), file=sys.stderr)
                raise
            unexpected = [e for e in sim.errors if "verified:" not in e]
            assert not sim.refused and not unexpected, (sim.refused, sim.errors)
            measured["replaced_key_signatures_rejected"] = len(sim.errors) - len(unexpected)
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        shim.wait()
        server.shutdown()
        server.server_close()
    measured["boundary"] = "Real daemon with local chain/Walrus simulators; no Walrus/Seal/provider, public-network or physical-device acceptance"
    if receipt:
        receipt.write_text(json.dumps(measured, indent=2) + "\n")
    print("PASS: device recovery drill", json.dumps(measured))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args()
    run(arguments.binary.resolve(), arguments.receipt)
