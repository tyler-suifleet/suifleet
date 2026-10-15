#!/usr/bin/env python3
"""Supervised components under the shipped systemd units, on real systemd and
cgroup v2 (SF-014), against the local chain/Walrus simulator: no keys, gas,
public network or hardware.

On the host (needs Docker): builds scripts/systemd/Dockerfile, boots it with
systemd as PID 1 in a disposable privileged container with a private cgroup
namespace, and reruns this script inside it with --inside. The daemon runs as
suifleet.service exactly as shipped (User=suifleet, ProtectControlGroups,
KillMode=mixed, Restart=on-failure, Type=notify with watchdog), after the
shipped suifleet-provision.service.

  0x501 run + readiness     -> Applied once ready; the run and its grouped,
                               setsid, env-cleared and reparented env-cleared
                               setsid descendants all live in the unit's cgroup
  component crash (SIGKILL) -> restarted by the supervisor; every one of those
                               descendants is gone
  daemon SIGKILL            -> systemd kills the rest of the cgroup and restarts
                               the unit; the successor accepts the sealed, empty
                               cgroup instead of refusing, bootstraps the
                               component, and no earlier process survives: one
                               instance runs
  systemctl stop            -> the daemon runs the component's own shutdown
                               command (systemd signals only the daemon); status
                               stopped; the unit's cgroup is empty
Evidence (versions, head, unit digests, results) goes to --evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "suifleet-edge/meta-suifleet/recipes-edge/suifleet/files"
IMAGE = "suifleet-systemd:test"
CONTAINER = "suifleet-supervisor-systemd"
UNIT_CGROUP = "/system.slice/suifleet.service"

# The run records itself and keeps four kinds of descendants: one in its
# group, one escaped with setsid, one with a cleared environment, and an
# env-cleared setsid one under a middle process (reparented when that dies).
RUN = ("rm -f ready middle detached; echo run >> runs; echo $$ > pid; "
       "sleep 300 & echo $! > grouped; setsid sleep 300 & echo $! > escaped; "
       "sh -c 'env -i setsid sleep 300 & echo $! > detached; echo $$ > middle; exec sleep 300' & "
       "env -i sleep 300 & echo $! > cleared; "
       "while [ ! -s middle ] || [ ! -s detached ]; do sleep 0.05; done; "
       "trap 'echo graceful > stopped; exit 0' TERM; echo $$ > ready; while true; do sleep 0.2; done")
# Ready only once this run (not a previous one, whose marker may persist in
# the shared work directory) has installed its TERM trap.
READY = 'test "$(cat ready 2>/dev/null)" = {pid}'
RECORDED = ("pid", "grouped", "escaped", "middle", "detached", "cleared")


def sh(*command, check=True, capture=True):
    result = subprocess.run(command, text=True, capture_output=capture)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)} failed ({result.returncode}): {result.stdout}{result.stderr}")
    return result.stdout.strip() if capture else ""


def host(args):
    for binary in ("suifleet", "examples/grpc_sim_shim"):
        assert (ROOT / "suifleet-edge/target/debug" / binary).exists(), f"build {binary} first (make test-supervisor-systemd)"
    sh("docker", "build", "-q", "-t", IMAGE, str(ROOT / "scripts/systemd"))
    sh("docker", "rm", "-f", CONTAINER, check=False)
    sh("docker", "run", "-d", "--name", CONTAINER, "--privileged", "--cgroupns=private",
       "--tmpfs", "/run", "--tmpfs", "/run/lock", "-v", f"{ROOT}:/workspace:ro", IMAGE)
    try:
        for _ in range(60):
            state = sh("docker", "exec", CONTAINER, "systemctl", "is-system-running", check=False)
            if state in ("running", "degraded"):
                break
            time.sleep(1)
        else:
            raise AssertionError(f"systemd did not boot: {state}")
        head = sh("git", "-C", str(ROOT), "rev-parse", "HEAD")
        dirty = bool(sh("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"))
        result = subprocess.run(["docker", "exec", "-e", "PYTHONDONTWRITEBYTECODE=1", CONTAINER, "python3",
                                 "/workspace/scripts/test_supervisor_systemd.py", "--inside",
                                 "--head", head + ("+dirty" if dirty else "")], text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        if result.returncode:
            sys.stdout.write(result.stderr[-6000:])
            journal = sh("docker", "exec", CONTAINER, "journalctl", "-u", "suifleet", "--no-pager", "-n", "80", check=False)
            sys.stdout.write("--- journal ---\n" + journal + "\n")
            raise SystemExit(result.returncode)
        evidence = json.loads(result.stdout.strip().splitlines()[-1].removeprefix("EVIDENCE "))
        evidence["host"] = {"kernel": platform.release(), "docker": sh("docker", "version", "--format", "{{.Server.Version}}"),
                            "cgroup": sh("stat", "-fc", "%T", "/sys/fs/cgroup")}
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        if not args.keep:
            sh("docker", "rm", "-f", CONTAINER, check=False)


# --- inside the systemd container (root) ------------------------------------

def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def cgroup(pid):
    try:
        return Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None


def unit_procs():
    try:
        return sorted(int(p) for p in Path(f"/sys/fs/cgroup{UNIT_CGROUP}/cgroup.procs").read_text().split())
    except OSError:
        return []


def show(prop):
    return sh("systemctl", "show", "-p", prop, "--value", "suifleet")


def inside(args):
    sys.path.insert(0, "/workspace/scripts")
    import test_ota
    from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

    checks = []

    def check(label):
        checks.append(label)
        print("PASS:", label, flush=True)

    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False

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
            if blob == "artifact":
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test_ota.SHIM = Path("/workspace/suifleet-edge/target/debug/examples/grpc_sim_shim")
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    url = f"http://127.0.0.1:{server.server_port}"

    # The shipped units, provisioning script and daemon, installed as on a device.
    sh("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "suifleet")
    sh("install", "-m", "0755", "/workspace/suifleet-edge/target/debug/suifleet", "/usr/bin/suifleet")
    sh("install", "-D", "-m", "0755", str(FILES / "suifleet-provision").replace(str(ROOT), "/workspace"), "/usr/libexec/suifleet-provision")
    units = {}
    for unit in ("suifleet.service", "suifleet-provision.service"):
        source = Path("/workspace/suifleet-edge/meta-suifleet/recipes-edge/suifleet/files") / unit
        units[unit] = hashlib.sha256(source.read_bytes()).hexdigest()
        sh("install", "-m", "0644", str(source), f"/etc/systemd/system/{unit}")
    etc = Path("/etc/suifleet")
    etc.mkdir(mode=0o755, exist_ok=True)
    key = etc / "device.key"
    key.write_text("07" * 32)  # The simulator's device identity (test key, not a secret).
    manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
        rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})
    (etc / "deployment.json").write_text(json.dumps(manifest))
    (etc / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
        f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
        f'[walrus]\naggregator_url = "{url}"\n'
        '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
        '[runtime]\nstate_dir = "/var/lib/suifleet"\npoll_seconds = 1\n'
        'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 5\n'
        'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 3\n')
    for path in (etc / "deployment.json", etc / "config.toml"):
        path.chmod(0o644)
    sh("systemctl", "daemon-reload")

    state = Path("/var/lib/suifleet")
    supervised = state / "supervised"
    work = supervised / "svc" / "work"

    def journal():
        try:
            return json.loads((state / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def outcome(id):
        return journal().get("deployments", {}).get(id, {})

    def status():
        try:
            return json.loads((supervised / "status.json").read_text()).get("svc", {})
        except (OSError, ValueError):
            return {}

    def read(name):
        try:
            return (work / name).read_text().strip()
        except OSError:
            return ""

    def recorded():
        return {name: int(read(name)) for name in RECORDED}

    def wait(condition, message, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.1)
        raise AssertionError(message)

    def runs():
        """Live run instances of svc (shells executing the run script)."""
        found = []
        for pid in unit_procs():
            try:
                if Path(f"/proc/{pid}/cwd").resolve() == work and b"echo run >> runs" in Path(f"/proc/{pid}/cmdline").read_bytes():
                    found.append(pid)
            except OSError:
                pass
        return found

    try:
        sh("systemctl", "start", "suifleet")
        assert show("ActiveState") == "active", show("ActiveState")
        assert Path("/etc/suifleet/device.address").exists(), "the shipped provisioning unit did not run"
        wait(lambda: journal().get("initialized"), "daemon did not establish its cursor")
        main = int(show("MainPID"))
        assert cgroup(main) == UNIT_CGROUP, cgroup(main)
        check(f"suifleet.service active as shipped (User={show('User')}, Type=notify, MainPID {main} in {UNIT_CGROUP})")

        sim.add("0x501")
        event = sim.events[-1]["parsedJson"]
        event["component_name"], event["version"] = "svc", "1.0.0"
        sim.recipes[event["recipe_blob_id"]].update(componentName="svc", componentVersion="1.0.0",
            lifecycle={"run": RUN, "readiness": READY, "shutdown": "echo $$ > requested; kill -TERM {pid}"})
        wait(lambda: outcome("0x501").get("status") == 1, "supervised component was not applied")
        wait(lambda: all(read(n) for n in RECORDED), "run did not record its descendants")
        first = recorded()
        assert all(alive(p) and cgroup(p) == UNIT_CGROUP for p in first.values()), {n: (alive(p), cgroup(p)) for n, p in first.items()}
        check("component Applied once ready; run plus grouped, setsid, env-cleared and reparented env-cleared setsid descendants all in the unit cgroup")

        os.kill(first["pid"], signal.SIGKILL)
        wait(lambda: status().get("phase") == "running" and status().get("restarts") == 1 and status().get("pid") != first["pid"],
             "crashed run was not restarted")
        wait(lambda: not any(alive(p) for p in first.values()),
             f"descendants survived a component crash: {[n for n, p in first.items() if alive(p)]}")
        assert show("MainPID") == str(main), "the daemon itself restarted on a component crash"
        check("component crash: restarted by the supervisor; all of its descendants killed; daemon unaffected")

        wait(lambda: all(read(n) for n in RECORDED) and int(read("pid")) == status().get("pid"), "restarted run did not record itself")
        second = recorded()
        restarts_before = int(show("NRestarts"))
        sh("systemctl", "kill", "--kill-who=main", "--signal=SIGKILL", "suifleet")
        wait(lambda: show("MainPID") not in ("0", str(main)) and show("ActiveState") == "active", "systemd did not restart the daemon", timeout=60)
        successor = int(show("MainPID"))
        assert int(show("NRestarts")) == restarts_before + 1
        wait(lambda: status().get("phase") == "running" and status().get("pid") not in (None, second["pid"]),
             "successor did not bootstrap the component")
        survivors = [n for n, p in second.items() if alive(p)]
        assert not survivors, f"processes of the killed daemon's component survived: {survivors}"
        assert len(runs()) == 1, f"expected one run instance, found {runs()}"
        log = re.sub(r"\x1b\[[0-9;]*m", "", sh("journalctl", "-u", "suifleet", "--no-pager", "-o", "cat"))
        assert "refusing to start components" not in log, "successor refused to start under the sealed cgroup"
        accepted = [line for line in log.splitlines() if "none of its processes can remain" in line]
        assert len(accepted) == 1 and f"previous={main}" in accepted[0] and "rebooted=false" in accepted[0], accepted
        assert cgroup(successor) == UNIT_CGROUP
        check(f"daemon SIGKILL: systemd emptied the cgroup and restarted the unit (NRestarts {restarts_before}->{restarts_before + 1}); "
              "successor accepted the sealed cgroup, bootstrapped the component; no earlier process survived; one instance")

        wait(lambda: outcome("0x501").get("reported"), "the deployment's report was not sent")
        third = recorded() if all(read(n) for n in RECORDED) else {}
        (work / "stopped").unlink(missing_ok=True)
        (work / "requested").unlink(missing_ok=True)
        sh("systemctl", "stop", "suifleet")
        assert show("ActiveState") == "inactive", show("ActiveState")
        assert read("requested"), "the daemon did not run the component's shutdown command on systemctl stop"
        assert read("stopped") == "graceful", "the run did not stop gracefully on systemctl stop"
        assert not unit_procs(), f"processes left in the unit cgroup: {unit_procs()}"
        assert not any(alive(p) for p in third.values()), "component processes outlived systemctl stop"
        assert status().get("phase") == "stopped", status()
        assert not sim.errors, sim.errors
        check("systemctl stop: graceful shutdown command ran; unit cgroup empty; status stopped; report sent")
    finally:
        sh("systemctl", "stop", "suifleet", check=False)
        shim.kill()
        server.shutdown()

    evidence = {
        "ticket": "SF-014", "head": args.head, "checks": checks,
        "systemd": sh("systemctl", "--version").splitlines()[0],
        "container_cgroup": sh("stat", "-fc", "%T", "/sys/fs/cgroup"),
        "os": Path("/etc/os-release").read_text().split('PRETTY_NAME="', 1)[1].split('"', 1)[0],
        "units_sha256": units,
        "daemon_sha256": hashlib.sha256(Path("/usr/bin/suifleet").read_bytes()).hexdigest(),
    }
    print("EVIDENCE " + json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--head", help=argparse.SUPPRESS)
    parser.add_argument("--evidence", help="write evidence JSON here")
    parser.add_argument("--keep", action="store_true", help="keep the container for inspection")
    arguments = parser.parse_args()
    (inside if arguments.inside else host)(arguments)
