#!/usr/bin/env python3
"""Supervised components (SF-014) with the real daemon against the local
chain/Walrus simulator, without keys or gas.

One component, `svc`, across daemon lifetimes:
  0x401 v1  run + readiness            -> Applied only once ready; status Running
            run crashes (SIGKILL)      -> restarted by the on-failure policy
  0x402 v2  never becomes ready        -> Failed; rolled back to v1, still Running
  0x403 v3  replaces v1                -> Applied; v1's run stopped
  daemon SIGTERM                       -> shutdown command ran; run, its grouped
                                          child and a setsid-escaped child all gone
  daemon start (reboot stage)          -> v3 started again from active.json
  daemon SIGKILL, then start           -> the orphaned run and every descendant
                                          (setsid, reparented, env-cleared) killed;
                                          one instance
  0x404 v4  no run (one-shot install)  -> Applied; v3 stopped and uncommitted,
                                          not started again by a daemon restart
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

# The run records itself, keeps a child in its group, one that escapes it with
# setsid, one under a middle process (so it can be reparented away from the
# run's tree) and one that clears its environment.
RUN = ("rm -f ready middle detached; echo $SUIFLEET_COMPONENT_VERSION >> runs; echo $$ > pid; "
       "sleep 300 & echo $! > grouped; setsid sleep 300 & echo $! > escaped; "
       "sh -c 'setsid sleep 300 & echo $! > detached; echo $$ > middle; exec sleep 300' & "
       "env -i sleep 300 & echo $! > cleared; "
       "while [ ! -s middle ] || [ ! -s detached ]; do sleep 0.05; done; "
       "trap 'echo graceful > stopped; exit 0' TERM; touch ready; while true; do sleep 0.2; done")
RECORDED = ("pid", "grouped", "escaped", "middle", "detached", "cleared")


def parent(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError):
        return None


def alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False

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
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def deploy(id, version, **lifecycle):
        sim.add(id)
        event = sim.events[-1]["parsedJson"]
        event["component_name"], event["version"] = "svc", version
        recipe = sim.recipes[event["recipe_blob_id"]]
        recipe.update(componentName="svc", componentVersion=version, lifecycle=lifecycle)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    seen = set()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-supervisor-") as temporary:
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
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\n'
                'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 3\n')
            components = state_dir / "supervised"
            work = components / "svc" / "work"

            def journal():
                path = state_dir / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcome(id):
                return journal().get("deployments", {}).get(id, {})

            def status():
                try:
                    return json.loads((components / "status.json").read_text()).get("svc", {})
                except (OSError, ValueError):
                    return {}

            def read(name):
                try:
                    return (work / name).read_text().strip()
                except OSError:
                    return ""

            def recorded():
                pids = [int(read(name)) for name in RECORDED]
                seen.update(pids)
                return pids

            def running(version):
                current = status()
                return current.get("phase") == "running" and current.get("version") == version

            with (root / "daemon.log").open("w") as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

                process = start()
                wait_for(lambda: journal().get("initialized"), "daemon did not establish its cursor", process)

                # Start and readiness.
                deploy("0x401", "1.0.0", run=RUN, readiness="test -e ready", shutdown="kill -TERM {pid}")
                wait_for(lambda: outcome("0x401").get("status") == 1, "v1 was not applied", process)
                assert running("1.0.0"), status()
                first = recorded()
                assert status()["pid"] == first[0] and all(map(alive, first)), (status(), first)
                print("PASS: supervised v1 applied once ready; status.json Running with its real pid")

                # Crash: the on-failure policy restarts the run.
                os.kill(first[0], signal.SIGKILL)
                wait_for(lambda: running("1.0.0") and status().get("restarts") == 1, "crashed run was not restarted", process)
                assert read("runs").splitlines() == ["1.0.0", "1.0.0"], read("runs")
                assert not alive(first[1]), "the crashed run's grouped child survived"
                wait_for(lambda: not any(map(alive, first)), "a descendant of the crashed run survived", process)
                print("PASS: crashed run restarted (on-failure); all of its descendants were killed")

                # Replacement that never becomes ready: Failed, rolled back to v1.
                deploy("0x402", "2.0.0", run="exit 1")
                wait_for(lambda: outcome("0x402").get("status") == 2, "unready v2 was not reported Failed", process)
                assert "rolled back to 1.0.0 (ready)" in outcome("0x402")["error"], outcome("0x402")
                assert running("1.0.0"), status()
                print("PASS: v2 never became ready: Failed, rolled back to v1 (Running)")

                # Successful replacement.
                before = recorded()
                deploy("0x403", "3.0.0", run=RUN, readiness="test -e ready", shutdown="kill -TERM {pid}")
                wait_for(lambda: outcome("0x403").get("status") == 1, "v3 was not applied", process)
                assert running("3.0.0"), status()
                assert not any(map(alive, before)), "v1's processes survived its replacement"
                assert read("stopped") == "graceful", "v1 was not stopped through its shutdown command"
                active = json.loads((components / "active.json").read_text())
                assert active["svc"]["version"] == "3.0.0", active
                print("PASS: v3 replaced v1 (graceful shutdown) and was committed")

                # Daemon stop: the component goes with it, descendants included.
                (work / "stopped").unlink()
                third = recorded()
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None
                assert read("stopped") == "graceful"
                assert not any(map(alive, third)), [p for p in third if alive(p)]
                assert status()["phase"] == "stopped", status()
                print("PASS: daemon SIGTERM stopped v3 gracefully; no descendant outlived the daemon")

                # Reboot stage: the committed version starts again.
                process = start()
                wait_for(lambda: running("3.0.0") and status().get("pid") not in third, "v3 did not start on daemon start", process)
                wait_for(lambda: recorded()[0] == status()["pid"], "the restarted run did not record itself", process)
                print("PASS: daemon start (reboot stage) restarted the committed v3")

                # Daemon killed outright: its successor kills the orphaned run first.
                orphan = recorded()
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=10)
                assert all(map(alive, orphan)), "the run did not outlive the killed daemon (nothing to recover)"
                # Break one descendant away from the run's tree as well.
                middle, detached = orphan[3], orphan[4]
                os.kill(middle, signal.SIGKILL)
                wait_for(lambda: parent(detached) != middle, "the detached descendant was not reparented")
                assert alive(detached)
                process = start()
                wait_for(lambda: running("3.0.0") and status().get("pid") != orphan[0], "v3 did not restart after a daemon kill", process)
                assert not any(map(alive, orphan)), ("a pre-crash descendant survived daemon recovery",
                                                     dict(zip(RECORDED, map(alive, orphan))))
                instances = [pid for pid in map(int, filter(str.isdigit, os.listdir("/proc")))
                             if alive(pid) and (Path(f"/proc/{pid}/cwd").exists() and os.readlink(f"/proc/{pid}/cwd") == str(work))
                             and Path(f"/proc/{pid}/cmdline").read_bytes().startswith(b"/bin/sh")]
                assert len(instances) == 1, f"expected one running instance, found {instances}"
                print("PASS: after a daemon SIGKILL every pre-crash process (setsid, reparented, env-cleared) "
                      "was killed before the restart; exactly one instance runs")

                # A same-name version without a run command ends supervision.
                wait_for(lambda: recorded()[0] == status().get("pid"), "the recovered run did not record itself", process)
                fourth = recorded()
                runs = len(read("runs").splitlines())
                deploy("0x404", "4.0.0", install="true")
                wait_for(lambda: outcome("0x404").get("status") == 1, "one-shot v4 was not applied", process)
                wait_for(lambda: not any(map(alive, fourth)), "v3 kept running after one-shot v4 was applied", process)
                assert "svc" not in json.loads((components / "active.json").read_text()), "v3 stayed committed"
                assert status() == {}, status()
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = start()
                wait_for(lambda: outcome("0x404").get("reported"), "v4's report was not sent", process)
                assert status() == {} and len(read("runs").splitlines()) == runs, "a daemon restart resurrected v3"
                print("PASS: one-shot v4 replaced supervised v3: v3 stopped, uncommitted, not restarted")

                # Reports queued across the stops and the kill go out on the next poll.
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None
                assert not sim.errors, sim.errors
                reported = sorted(id for id, _ in sim.reports)
                assert reported == ["0x401", "0x402", "0x403", "0x404"], reported
                print("PASS: all four outcomes reported on chain")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        # Leave nothing behind when an assertion failed mid-run (only processes
        # that still look like the component's, not a reused PID).
        for pid in seen:
            try:
                if Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0] in (b"sleep", b"/bin/sh", b"sh"):
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    args = parser.parse_args()
    run(args.binary.resolve())
