#!/usr/bin/env python3
"""Desired-state reconciliation (SF-013): the real daemon against the chain/Walrus
simulator of scripts/test_ota.py (through the gRPC shim), without keys or gas.

Covers supersession while offline, a newly joined member, group removal,
cross-group conflicts, an atomic multi-component plan whose failing step stops
the rest until the desired state changes, crash recovery that neither
re-executes an updater nor runs later steps, reselection of an already-run
deployment (A -> B -> A) and one-shot updates of a managed component, which
are never recorded as convergence, and coexistence with one-shot deployments.
In privileged-helper mode, a newer desired state seen while a step is still in
flight is planned once that step settles, across restarts.
Real-chain behaviour is covered by scripts/test_desired_state_localnet.py.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_ota import (ARTIFACT, CAP, DEVICE, GROUP, OPERATOR, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP,
                      Simulator, start_grpc, wait_for)

G2, G3 = "0x20", "0x30"


def serve(sim):
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
                sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                self.respond(ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def manifest(sim, url, rpc_url):
    return dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
        rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
        type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})


def run(binary):
    sim = Simulator()
    sim.truncate_next_artifact = False
    sim.allow_reports = True
    sim.fail_execution = False
    sim.groups[G2] = {"admin": OPERATOR, "device_ids": [DEVICE]}
    sim.groups[G3] = {"admin": OPERATOR, "device_ids": []}
    sim.memberships += [(GROUP, DEVICE), (G2, DEVICE)]

    def deploy(id, component, groups, *, managed=True, bad_hash=False):
        sim.add(id, groups=groups, direct=False, bad_hash=bad_hash, event=dict(component_name=component))
        if managed:
            sim.managed.add(id)

    def desire(group, revision, components):
        sim.desired[group] = (revision, components)

    server = serve(sim)
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-desired-") as temporary:
            root = Path(temporary)
            key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
            applied, hang = root / "applied.jsonl", root / "hang"
            updater = root / "apply.py"
            # Records each run; blocks while `hang` exists (to crash mid-apply).
            updater.write_text("import os, pathlib, sys, time\n"
                               f"pathlib.Path({str(root / 'updater.pid')!r}).write_text(str(os.getpid()))\n"
                               f"while pathlib.Path({str(hang)!r}).exists(): time.sleep(0.1)\n"
                               f"open({str(applied)!r}, 'a').write(sys.argv[1] + '\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            (root / "deployment.json").write_text(json.dumps(manifest(sim, url, rpc_url)))
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[apply]\ncommand = {json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds = 30\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n')
            log = (root / "daemon.log").open("w")

            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcomes():
                return {id: o["status"] for id, o in state().get("deployments", {}).items()}

            def runs():
                return len(applied.read_text().splitlines()) if applied.exists() else 0

            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            def settle():
                """Wait for two further complete polls (each re-checks the chain)."""
                for _ in range(2):
                    checks = sim.chain_checks
                    wait_for(lambda: sim.chain_checks > checks, "daemon stopped polling", process)
                time.sleep(0.3)

            # 1. Offline through two revisions, then first start: only the latest
            #    firmware is applied, although enrollment skips one-shot history.
            deploy("0x201", "firmware", [GROUP]); desire(GROUP, 1, {"firmware": "0x201"})
            deploy("0x202", "firmware", [GROUP]); desire(GROUP, 2, {"firmware": "0x202"})
            process = start()
            wait_for(lambda: outcomes().get("0x202") == 1, "latest desired firmware was not applied", process)
            settle()
            assert "0x201" not in outcomes() and runs() == 1, ("superseded revision applied", outcomes())
            assert state()["managed"] == {"firmware": "0x202"}
            wait_for(lambda: ("0x202", 1) in sim.reports, "desired deployment not reported through its group", process)
            print("PASS: offline device applies only the latest revision (superseded firmware skipped) and reports it")

            # 2. A newly joined member receives the group's existing desired state.
            deploy("0x203", "logger", [G3]); desire(G3, 1, {"logger": "0x203"})
            settle()
            assert "0x203" not in outcomes(), "non-member applied a group's desired state"
            sim.groups[G3]["device_ids"].append(DEVICE); sim.memberships.append((G3, DEVICE))
            wait_for(lambda: outcomes().get("0x203") == 1, "new member did not receive existing desired state", process)
            print("PASS: device added to a group receives that group's existing desired state")

            # 3. Leaving the group retires its component; nothing is applied.
            sim.groups[G3]["device_ids"].remove(DEVICE)
            wait_for(lambda: "logger" not in state().get("managed", {}), "removed member kept following the group", process)
            assert runs() == 2
            print("PASS: device removed from a group stops following it (component retired, not re-applied)")

            # 4. Two groups disagree on a component: neither is applied.
            deploy("0x204", "firmware", [G2]); desire(G2, 1, {"firmware": "0x204"})
            settle()
            assert "0x204" not in outcomes() and state()["managed"]["firmware"] == "0x202"
            desire(G2, 2, {"firmware": "0x202"})  # Agreement again: already applied.
            settle()
            assert runs() == 2
            print("PASS: cross-group conflict leaves the component as installed until the groups agree")

            # 5. One revision changes three components; the failing first step
            #    stops the plan, and the same desired state is not retried.
            deploy("0x205", "alpha", [GROUP], bad_hash=True)
            deploy("0x206", "zeta", [GROUP])
            desire(GROUP, 3, {"alpha": "0x205", "firmware": "0x202", "zeta": "0x206"})
            wait_for(lambda: outcomes().get("0x205") == 2, "failing step did not fail", process)
            settle()
            assert "0x206" not in outcomes(), "plan continued past a failed step"
            assert state()["step"]["deployment"] == "0x205" and runs() == 2
            deploy("0x207", "alpha", [GROUP])
            desire(GROUP, 4, {"alpha": "0x207", "firmware": "0x202", "zeta": "0x206"})
            wait_for(lambda: outcomes().get("0x207") == 1 and outcomes().get("0x206") == 1, "revised plan did not complete", process)
            settle()  # The last outcome is saved just before its step is cleared.
            assert "step" not in state() and runs() == 4, (runs(), state())
            print("PASS: atomic three-component revision; a failed step stops the plan until a new revision, which then completes")

            # 6. Crash during the first step of a revision that has a later,
            #    unexecuted step: the interrupted updater is never re-run, and
            #    the later step stays stopped after restart.
            hang.touch()
            deploy("0x208", "firmware", [GROUP])
            deploy("0x20b", "zeta", [GROUP])
            # G2 first: until GROUP agrees, firmware is only a conflict.
            desire(G2, 3, {"firmware": "0x208"})
            desire(GROUP, 5, {"alpha": "0x207", "firmware": "0x208", "zeta": "0x20b"})
            pid = root / "updater.pid"; pid.unlink(missing_ok=True)
            wait_for(lambda: "0x208" in state().get("deployments", {}) and pid.exists(), "updater did not start", process)
            assert state()["step"]["deployment"] == "0x208"
            # Power loss mid-update: the daemon and its updater (own process group) die.
            process.send_signal(signal.SIGKILL); process.wait()
            os.killpg(int(pid.read_text()), signal.SIGKILL)
            hang.unlink()
            process = start()
            wait_for(lambda: state().get("deployments", {}).get("0x208", {}).get("indeterminate"), "interrupted step was not preserved as unknown", process)
            assert outcomes().get("0x208") is None and not state()["deployments"]["0x208"]["reported"]
            settle()
            assert runs() == 4, "interrupted updater was executed again"
            assert "interrupted" in state()["deployments"]["0x208"]["error"]
            assert "0x20b" not in outcomes(), "plan continued past an interrupted step after restart"
            assert state()["step"]["deployment"] == "0x208" and state()["current"]["firmware"] == "0x208"
            print("PASS: crash mid-step stays unknown and unreported on restart; the updater is never re-executed and later steps stay stopped")

            # 7. A -> B -> A: firmware 0x202 ran, then 0x208 was started; a
            #    revision selecting 0x202 again is not taken as converged (a
            #    deployment never runs twice). The new state lifts the stop, so
            #    the other step proceeds.
            desire(GROUP, 6, {"alpha": "0x207", "firmware": "0x202", "zeta": "0x20b"})
            desire(G2, 4, {"firmware": "0x202"})
            wait_for(lambda: outcomes().get("0x20b") == 1, "new desired state did not lift the stop", process)
            settle()
            assert runs() == 5 and state()["current"]["firmware"] == "0x208", ("reselection recorded as convergence", state())
            assert "already ran here" in (root / "daemon.log").read_text()
            print("PASS: reselecting an already-run deployment (A -> B -> A) is reported, not recorded as installed")

            # 8. A one-shot update of a managed component is what it now runs;
            #    the next desired version converges it again.
            deploy("0x20c", "zeta", [GROUP], managed=False)
            wait_for(lambda: outcomes().get("0x20c") == 1, "one-shot update of a managed component not applied", process)
            settle()
            assert runs() == 6 and state()["current"]["zeta"] == "0x20c"
            deploy("0x20d", "zeta", [GROUP])
            desire(GROUP, 7, {"alpha": "0x207", "firmware": "0x202", "zeta": "0x20d"})
            wait_for(lambda: outcomes().get("0x20d") == 1, "next desired version did not converge", process)
            assert state()["current"]["zeta"] == "0x20d" and state()["managed"]["zeta"] == "0x20d"
            print("PASS: one-shot update of a managed component is tracked; the next desired version converges it")

            # 9. One-shot jobs still work; a managed record outside any desired
            #    state is left alone by the event path.
            deploy("0x209", "tool", [GROUP], managed=False)
            deploy("0x20a", "orphan", [GROUP])
            wait_for(lambda: outcomes().get("0x209") == 1, "unmanaged one-shot deployment not applied", process)
            settle()
            assert "0x20a" not in outcomes() and runs() == 8
            print("PASS: unmanaged deployments stay one-shot jobs; managed records apply only through desired state")
            assert not sim.errors, sim.errors
            process.send_signal(signal.SIGINT); process.wait(timeout=10)
            assert process.returncode == 0
            process = None
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait()
        shim.kill(); shim.wait()
        server.shutdown(); server.server_close()


def run_helper(binary):
    """A step in flight in the privileged helper, then a newer desired state:
    it waits for the step, and is planned once the step fails, although no
    further change arrives and the daemon restarts in between."""
    sim = Simulator()
    sim.truncate_next_artifact = False
    sim.allow_reports = True
    sim.fail_execution = False
    sim.memberships.append((GROUP, DEVICE))
    server = serve(sim)
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    process, helper = None, None
    stop_helper, pause_helper = threading.Event(), threading.Event()
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-desired-helper-") as temporary:
            root = Path(temporary)
            key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
            state_dir = root / "state"
            spool = state_dir / "firmware"
            requests = spool / "requests"
            applied, hold, fail = root / "applied", root / "hold", root / "fail"
            url = f"http://127.0.0.1:{server.server_port}"
            (root / "deployment.json").write_text(json.dumps(manifest(sim, url, rpc_url)))
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nmode = "privileged-helper"\n'
                f'firmware_spool = {json.dumps(str(spool))}\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\n')
            # The firmware updater: records each run, blocks while `hold` exists
            # (the helper holds its claim meanwhile), fails while `fail` exists.
            script = f"echo ran >> {applied}; while [ -e {hold} ]; do sleep 0.1; done; [ ! -e {fail} ]"
            helper_config = root / "firmware.toml"
            helper_config.write_text(f'command = {json.dumps("/bin/sh -c " + chr(39) + script + chr(39))}\n'
                f'timeout_seconds = 60\nspool_dir = {json.dumps(str(spool))}\nwork_dir = {json.dumps(str(root / "helper-work"))}\n')
            log = (root / "daemon.log").open("w")

            def helper_loop():
                # Like suifleet-firmware.path: run the helper when a request appears.
                while not stop_helper.is_set():
                    if not pause_helper.is_set() and requests.is_dir() and any(requests.glob("*.json")):
                        subprocess.run([str(binary), "--firmware-helper", str(helper_config)], stdout=log, stderr=log)
                    time.sleep(0.1)

            def state():
                path = state_dir / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcomes():
                return {id: o["status"] for id, o in state().get("deployments", {}).items()}

            def runs():
                return len(applied.read_text().splitlines()) if applied.exists() else 0

            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            def settle():
                for _ in range(2):
                    checks = sim.chain_checks
                    wait_for(lambda: sim.chain_checks > checks, "daemon stopped polling", process)
                time.sleep(0.3)

            def deploy(id, component):
                sim.add(id, groups=[GROUP], direct=False, event=dict(component_name=component))
                sim.managed.add(id)

            helper = threading.Thread(target=helper_loop, daemon=True)
            helper.start()
            hold.touch(); fail.touch()
            # Revision 1: alpha (A) then beta. A's update is held in the helper.
            deploy("0x401", "alpha"); deploy("0x402", "beta")
            sim.desired[GROUP] = (1, {"alpha": "0x401", "beta": "0x402"})
            process = start()
            wait_for(lambda: runs() == 1 and "0x401" in outcomes(), "first step was not handed to the helper", process)
            # The daemon dies while the helper is still applying A.
            process.send_signal(signal.SIGKILL); process.wait()
            process = start()
            settle()
            assert outcomes() == {"0x401": None}, ("in-flight step settled or later step ran", outcomes())
            first = state()["step"]
            assert first["deployment"] == "0x401"
            # Revision 2 replaces A while it is still in flight: it waits.
            deploy("0x403", "alpha")
            sim.desired[GROUP] = (2, {"alpha": "0x403", "beta": "0x402"})
            settle()
            assert outcomes() == {"0x401": None} and state()["step"] == first, ("newer state did not wait", state())
            # A restart between seeing revision 2 and A settling.
            process.send_signal(signal.SIGINT); process.wait(timeout=10)
            assert process.returncode == 0
            process = start()
            settle()
            assert outcomes() == {"0x401": None} and state()["step"] == first
            # A fails. Nothing changes on chain; revision 2 is still planned.
            pause_helper.set()
            hold.unlink()
            wait_for(lambda: outcomes().get("0x401") == 2, "failed helper result was not collected", process)
            fail.unlink()
            pause_helper.clear()
            wait_for(lambda: outcomes().get("0x403") == 1 and outcomes().get("0x402") == 1,
                     "newer desired state was not planned after the in-flight step settled", process)
            settle()
            assert runs() == 3 and "step" not in state(), (runs(), state())
            assert state()["current"] == {"alpha": "0x403", "beta": "0x402"}
            wait_for(lambda: dict(sim.reports).get("0x401") == 2, "failed step not reported", process)
            assert not sim.errors, sim.errors
            process.send_signal(signal.SIGINT); process.wait(timeout=10)
            assert process.returncode == 0
            process = None
            print("PASS: a newer desired state waits for an in-flight helper step and is planned once it fails, across restarts")
    finally:
        stop_helper.set()
        if helper:
            helper.join(timeout=70)
        if process and process.poll() is None:
            process.kill(); process.wait()
        shim.kill(); shim.wait()
        server.shutdown(); server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    binary = parser.parse_args().binary.resolve()
    run(binary)
    run_helper(binary)
