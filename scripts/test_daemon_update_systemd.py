#!/usr/bin/env python3
"""Daemon self-update (SF-026) under the shipped suifleet.service on real
systemd and cgroup v2, against the local simulator: no keys, gas, public
network or hardware. Uses the disposable harness of
scripts/test_supervisor_systemd.py (host side: needs Docker).

  start                         -> ExecStart=/usr/bin/suifleet --launch runs the
                                   base binary (nothing staged)
  0x801 v2  working new daemon  -> staged and preflighted; exit 75; systemd
                                   restarts the unit; the launcher execs v2 as
                                   the unit's main process, it confirms and
                                   reports Applied with the same identity/journal
  0x802 v3  hangs before ready  -> systemd's start timeout kills it twice; the
                                   launcher then restarts v2 (the new daemon is
                                   never asked to recover itself), which reports
                                   Failed; v2 stays current
  0x803 v4  exits at startup    -> two failed starts, then the same rollback
  systemctl stop                -> unit inactive, cgroup empty
The only test-specific unit change is a drop-in shortening TimeoutStartSec to
15 s (default 90 s) so the hang case runs quickly.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_supervisor_systemd import ROOT, alive, cgroup, host, sh, show, unit_procs, UNIT_CGROUP  # noqa: E402

CONTAINER = "suifleet-daemon-update-systemd"
COMPONENT = "suifleet-daemon"
HANGS = b'#!/bin/sh\n[ "$1" = --self-check ] && exit 0\nexec sleep 1000\n'
EXITS = b'#!/bin/sh\n[ "$1" = --self-check ] && exit 0\necho "broken daemon" >&2\nexit 3\n'


def inside(args):
    sys.path.insert(0, "/workspace/scripts")
    import test_ota
    from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

    checks = []

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    blobs = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
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
            if blob in blobs:
                self.respond(blobs[blob])
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def deploy(id, version, payload):
        digest = hashlib.sha256(payload).hexdigest()
        blobs[f"artifact-{id}"] = payload
        sim.add(id, event=dict(component_name=COMPONENT, version=version, artifact_sha256=digest),
                recipe=dict(artifactBlobId=f"artifact-{id}"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test_ota.SHIM = Path("/workspace/suifleet-edge/target/debug/examples/grpc_sim_shim")
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    url = f"http://127.0.0.1:{server.server_port}"
    files = Path("/workspace/suifleet-edge/meta-suifleet/recipes-edge/suifleet/files")

    sh("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "suifleet")
    sh("install", "-m", "0755", "/workspace/suifleet-edge/target/debug/suifleet", "/usr/bin/suifleet")
    sh("install", "-D", "-m", "0755", str(files / "suifleet-provision"), "/usr/libexec/suifleet-provision")
    units = {}
    for unit in ("suifleet.service", "suifleet-provision.service"):
        units[unit] = hashlib.sha256((files / unit).read_bytes()).hexdigest()
        sh("install", "-m", "0644", str(files / unit), f"/etc/systemd/system/{unit}")
    dropin = Path("/etc/systemd/system/suifleet.service.d")
    dropin.mkdir(parents=True)
    (dropin / "test-start-timeout.conf").write_text("[Service]\nTimeoutStartSec=15\n")
    etc = Path("/etc/suifleet")
    etc.mkdir(mode=0o755, exist_ok=True)
    key = etc / "device.key"
    key.write_text("07" * 32)  # The simulator's test identity, not a secret.
    manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
        rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})
    (etc / "deployment.json").write_text(json.dumps(manifest))
    (etc / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
        f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
        f'[walrus]\naggregator_url = "{url}"\n'
        '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\n'
        '[runtime]\nstate_dir = "/var/lib/suifleet"\npoll_seconds = 1\n'
        f'[daemon_update]\nenabled = true\ncomponent = "{COMPONENT}"\nmax_start_attempts = 2\n')
    for path in (etc / "deployment.json", etc / "config.toml"):
        path.chmod(0o644)
    sh("systemctl", "daemon-reload")
    state = Path("/var/lib/suifleet")
    daemon_dir = state / "daemon"

    def journal():
        try:
            return json.loads((state / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def outcome(id):
        return journal().get("deployments", {}).get(id, {})

    def current():
        try:
            return (daemon_dir / "current").read_text().strip()
        except OSError:
            return None

    def main_exe():
        pid = show("MainPID")
        try:
            return os.readlink(f"/proc/{pid}/exe") if pid != "0" else None
        except OSError:
            return None

    def wait(condition, message, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.2)
        raise AssertionError(message)

    def log():
        return re.sub(r"\x1b\[[0-9;]*m", "", sh("journalctl", "-u", "suifleet", "--no-pager", "-o", "cat"))

    try:
        sh("systemctl", "start", "suifleet")
        wait(lambda: journal().get("initialized"), "daemon did not establish its cursor")
        binding = journal()["binding"]
        assert show("ExecStart").count("--launch") and main_exe() == "/usr/bin/suifleet", (show("ExecStart"), main_exe())
        check("ExecStart=/usr/bin/suifleet --launch runs the base binary when nothing is staged")

        restarts = int(show("NRestarts"))
        deploy("0x801", "2.0.0", Path("/usr/bin/suifleet").read_bytes())
        wait(lambda: outcome("0x801").get("status") == 1, "the new daemon did not confirm and apply")
        v2 = current()
        assert v2 and "/versions/2.0.0-" in v2, v2
        wait(lambda: main_exe() == v2 and show("ActiveState") == "active", "v2 is not the unit's main process")
        assert int(show("NRestarts")) == restarts + 1, "the handoff did not go through a service-manager restart"
        assert cgroup(int(show("MainPID"))) == UNIT_CGROUP
        assert journal()["binding"] == binding
        assert "Daemon update confirmed" in log()
        check(f"working update: staged, exit 75, systemd restarted the unit, the launcher exec'd {Path(v2).parent.name}/suifleet "
              "as its main process; confirmed and Applied with the same identity and journal")

        restarts = int(show("NRestarts"))
        deploy("0x802", "3.0.0", HANGS)
        wait(lambda: outcome("0x802").get("status") == 2, "the hanging update was not rolled back", timeout=180)
        assert "did not complete startup in 2 attempt(s)" in outcome("0x802")["error"], outcome("0x802")
        assert current() == v2, current()
        wait(lambda: main_exe() == v2 and show("ActiveState") == "active", "v2 is not running after the rollback")
        timeouts = log().count("start operation timed out")
        assert timeouts >= 2, f"expected two start timeouts, saw {timeouts}"
        assert int(show("NRestarts")) >= restarts + 3
        check(f"update that never becomes ready: systemd's start timeout killed it {timeouts} times; the launcher then "
              "restarted v2 without the new daemon's help; Failed reported; v2 stays current")

        deploy("0x803", "4.0.0", EXITS)
        wait(lambda: outcome("0x803").get("status") == 2, "the exiting update was not rolled back", timeout=120)
        assert "did not complete startup in 2 attempt(s)" in outcome("0x803")["error"], outcome("0x803")
        wait(lambda: main_exe() == v2 and show("ActiveState") == "active", "v2 is not running after the second rollback")
        check("update that exits at startup: two failed starts under systemd, then rollback to v2; Failed reported")

        wait(lambda: all(outcome(i).get("reported") for i in ("0x801", "0x802", "0x803")), "outcomes were not all reported")
        statuses = {id: status for id, status in sim.reports}
        assert (statuses["0x801"], statuses["0x802"], statuses["0x803"]) == (1, 2, 2), statuses
        sh("systemctl", "stop", "suifleet")
        assert show("ActiveState") == "inactive" and not unit_procs(), unit_procs()
        assert not sim.errors, sim.errors
        check("outcomes reported (Applied, Failed, Failed); systemctl stop leaves the unit inactive with an empty cgroup")
    finally:
        sh("systemctl", "stop", "suifleet", check=False)
        shim.kill()
        server.shutdown()

    print("EVIDENCE " + json.dumps({
        "ticket": "SF-026", "head": args.head, "checks": checks,
        "systemd": sh("systemctl", "--version").splitlines()[0],
        "container_cgroup": sh("stat", "-fc", "%T", "/sys/fs/cgroup"),
        "os": Path("/etc/os-release").read_text().split('PRETTY_NAME="', 1)[1].split('"', 1)[0],
        "units_sha256": units, "test_dropin": "TimeoutStartSec=15",
        "daemon_sha256": hashlib.sha256(Path("/usr/bin/suifleet").read_bytes()).hexdigest(),
    }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--head", help=argparse.SUPPRESS)
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--keep", action="store_true", help="keep the container for inspection")
    arguments = parser.parse_args()
    if arguments.inside:
        inside(arguments)
    else:
        host(arguments, script="test_daemon_update_systemd.py", container=CONTAINER)
