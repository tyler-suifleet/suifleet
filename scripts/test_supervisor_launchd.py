#!/usr/bin/env python3
"""The daemon as a real macOS LaunchDaemon (SF-075), installed with the shipped
packaging/macos/install.sh and plist, against the local chain/Walrus
simulator: no keys, gas, public network or hardware.

Run as root on a disposable macOS host (a hosted CI runner): it creates the
_suifleet account, writes /etc/suifleet, /var/lib/suifleet and
/Library/LaunchDaemons, and needs --disposable-host to proceed. Build the
debug daemon and examples/grpc_sim_shim first.

  bootstrap                 -> the job runs as _suifleet in a launchd coalition
                               it alone occupies, recorded as sealed
  0x501 run + readiness     -> Applied once ready; the run and its grouped,
                               setsid, env-cleared and reparented env-cleared
                               setsid descendants are all in that coalition;
                               the component cannot spawn into another one
  component crash (SIGKILL) -> restarted by the supervisor; every descendant,
                               including the detached env-cleared one, is gone
  daemon SIGKILL            -> launchd restarts the job; the successor reaps
                               the old coalition, accepts it as empty, and
                               bootstraps the component; nothing earlier remains
  daemon SIGKILL mid-install-> the interrupted install's processes are gone; its
                               outcome stays indeterminate and is never rerun;
                               the committed version runs again
  secrets                   -> key, state and component log unreadable by other
                               users; install.sh strips a preexisting key ACL; a
                               group/world-readable key, or a 0600 key with an
                               ACL granting read, stops the daemon
  launchctl bootout         -> the component's own shutdown command runs; status
                               stopped; the coalition is empty
Evidence (versions, head, file digests, results) goes to --evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PACKAGING = ROOT / "suifleet-edge/packaging/macos"
LABEL = "org.suifleet.daemon"
PLIST = Path(f"/Library/LaunchDaemons/{LABEL}.plist")
USER = "_suifleet"
ETC = Path("/etc/suifleet")
STATE = Path("/var/lib/suifleet")
LOG = Path("/var/log/suifleet/daemon.log")
# Outside any home directory, so the daemon user can run it.
PROBE = Path("/usr/local/libexec/suifleet-test/macos_proc.py")
PERL_SETSID = "/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV'"
# The same, for use inside a single-quoted sh -c script.
PERL_SETSID_QUOTED = '/usr/bin/perl -MPOSIX -e "POSIX::setsid(); exec @ARGV"'

# The run records itself and keeps four kinds of descendants: one in its
# group, one escaped with setsid, one with a cleared environment, and an
# env-cleared setsid one under a middle process (reparented when that dies).
# It also tries to spawn into another coalition once.
RUN = ("rm -f ready middle detached; echo run >> runs; echo $$ > pid; "
       "/bin/sleep 300 & echo $! > grouped; "
       f"{PERL_SETSID} /bin/sleep 300 & echo $! > escaped; "
       f"/bin/sh -c 'env -i {PERL_SETSID_QUOTED} /bin/sleep 300 & echo $! > detached; echo $$ > middle; exec /bin/sleep 300' & "
       "env -i /bin/sleep 300 & echo $! > cleared; "
       "[ -s escape ] || /usr/bin/python3 " + str(PROBE) + " escape {target} > escape 2>&1; "
       # Also asks the services that start jobs for a user to start one.
       "[ -s delegated ] || { echo '* * * * * /bin/sleep 302' | crontab -; echo crontab=$?; "
       "echo '/bin/sleep 303' | at now; echo at=$?; } > delegated 2>&1; "
       "while [ ! -s middle ] || [ ! -s detached ]; do sleep 0.05; done; "
       "trap 'echo graceful > stopped; exit 0' TERM; echo $$ > ready; while true; do sleep 0.2; done")
READY = 'test "$(cat ready 2>/dev/null)" = {pid}'
SHUTDOWN = "echo $$ > requested; kill -TERM {pid}"
RECORDED = ("pid", "grouped", "escaped", "middle", "detached", "cleared")


def sh(*command, check=True, user=None):
    if user:
        command = ("sudo", "-n", "-u", user, *command)
    result = subprocess.run(command, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)} failed ({result.returncode}): {result.stdout}{result.stderr}")
    return result


def job():
    """launchd's view of the job: pid, runs, state (empty when not loaded)."""
    result = sh("launchctl", "print", f"system/{LABEL}", check=False)
    if result.returncode:
        return {}
    fields = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\t(pid|runs|state|last exit code) = (.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def acl(path):
    """The extended ACL entries of a file, as ls -led lists them."""
    lines = sh("ls", "-led", str(path)).stdout.splitlines()[1:]
    return [line.strip() for line in lines if line.strip()]


def daemon_pid():
    return int(job().get("pid", 0))


def wait(condition, message, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    import macos_proc as proc
    import test_ota
    from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

    assert platform.system() == "Darwin", "macOS only"
    assert os.geteuid() == 0, "run as root (sudo) on a disposable macOS host"
    if not args.disposable_host:
        raise SystemExit("refusing: this installs a LaunchDaemon and a system account; pass --disposable-host on a throwaway runner")
    binary = ROOT / "suifleet-edge/target/debug/suifleet"
    for built in (binary, test_ota.SHIM):
        assert built.exists(), f"build {built} first"
    assert not PLIST.exists() and not job(), f"{LABEL} is already installed here"

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
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    url = f"http://127.0.0.1:{server.server_port}"

    supervised = STATE / "supervised"
    work = supervised / "svc" / "work"

    def journal():
        try:
            return json.loads((STATE / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def outcome(id):
        return journal().get("deployments", {}).get(id, {})

    def status_of():
        try:
            return json.loads((supervised / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def status():
        return status_of().get("svc", {})

    def instance():
        try:
            return json.loads((supervised / "instance.json").read_text())
        except (OSError, ValueError):
            return {}

    def read(name):
        try:
            return (work / name).read_text().strip()
        except OSError:
            return ""

    def recorded():
        return {name: int(read(name)) for name in RECORDED}

    def log():
        try:
            return re.sub(r"\x1b\[[0-9;]*m", "", LOG.read_text())
        except OSError:
            return ""

    try:
        # The simulator's device identity (a test key, not a secret), written
        # world-readable: the installer must keep it and make it private.
        ETC.mkdir(mode=0o755, parents=True, exist_ok=True)
        key = ETC / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o644)
        # ...and with an ACL entry granting another user read access (and an
        # inheritable one on its directory), which the installer must remove.
        sh("chmod", "+a", "user:nobody allow read", str(key))
        sh("chmod", "+a", "user:nobody allow read,file_inherit", str(ETC))
        installed = sh("sh", str(PACKAGING / "install.sh"), str(binary)).stdout
        account = pwd.getpwnam(USER)
        assert 200 <= account.pw_uid <= 400, account
        assert (key.stat().st_mode & 0o777, key.stat().st_uid) == (0o600, account.pw_uid)
        for path in (key, ETC, STATE):
            assert not acl(path), f"the installer left an ACL on {path}: {acl(path)}"
        assert sh("cat", str(key), user="nobody", check=False).returncode != 0, "nobody read the installed key"
        assert (STATE.stat().st_mode & 0o777, STATE.stat().st_uid) == (0o700, account.pw_uid)
        assert "SuiFleet device address: 0x" in installed, installed
        PROBE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "scripts/macos_proc.py", PROBE)
        PROBE.chmod(0o755)
        manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
            rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
            shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": url, "aggregator_url": url})
        (ETC / "deployment.json").write_text(json.dumps(manifest))
        (ETC / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n'
            '[apply]\ncommand = "/usr/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
            f'[runtime]\nstate_dir = "{STATE}"\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
            'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 10\n'
            'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 3\n')
        for path in (ETC / "deployment.json", ETC / "config.toml"):
            path.chmod(0o644)

        sh("launchctl", "bootstrap", "system", str(PLIST))
        wait(lambda: daemon_pid() > 0 and journal().get("initialized"), f"daemon did not start: {job()} {log()[-2000:]}")
        main = daemon_pid()
        coalition = proc.coalition(main)
        assert proc.bsdinfo(main)["uid"] == account.pw_uid, proc.bsdinfo(main)
        assert coalition and coalition != proc.coalition(os.getpid()), (coalition, proc.coalition(os.getpid()))
        wait(lambda: instance().get("coalition") == coalition, f"sealed coalition not recorded: {instance()}")
        assert proc.members(coalition) == [main], proc.members(coalition)
        assert "Not the only process" not in log(), log()
        check(f"{LABEL} bootstrapped from the shipped plist: runs as {USER} (uid {account.pw_uid}) alone in launchd "
              f"coalition {coalition}, recorded as sealed")

        target = proc.coalition(os.getpid())
        sim.add("0x501", event=dict(component_name="svc", version="1.0.0"),
                recipe=dict(lifecycle={"run": RUN.replace("{target}", str(target)), "readiness": READY, "shutdown": SHUTDOWN}))
        wait(lambda: outcome("0x501").get("status") == 1, f"supervised component was not applied: {outcome('0x501')} {log()[-3000:]}")
        wait(lambda: all(read(n) for n in RECORDED), "run did not record its descendants")
        first = recorded()
        placement = {name: (proc.alive(pid), proc.coalition(pid)) for name, pid in first.items()}
        assert all(placement[name] == (True, coalition) for name in RECORDED), placement
        assert proc.bsdinfo(first["escaped"])["pgid"] != proc.bsdinfo(first["pid"])["pgid"], "setsid did not leave the group"
        check("component Applied once ready; run plus grouped, setsid, env-cleared and reparented env-cleared setsid "
              "descendants all in the daemon's coalition")

        wait(lambda: read("escape").startswith("attribute="), f"escape attempt did not finish: {read('escape')!r}")
        attempt = dict(part.split("=", 1) for part in read("escape").split())
        assert attempt["own"] == str(coalition), attempt
        assert attempt["child"] in ("None", str(coalition)) and attempt["child"] != str(target), attempt
        assert attempt["spawn"] != "0" or attempt["child"] == str(coalition), attempt
        check(f"a component cannot spawn into another coalition (posix_spawnattr_setcoalition_np: {read('escape')})")
        wait(lambda: "at=" in read("delegated"), f"delegated launch attempts did not finish: {read('delegated')!r}")
        delegated = dict(line.split("=", 1) for line in read("delegated").splitlines() if re.match(r"^(crontab|at)=\d+$", line))
        assert delegated.get("crontab") not in (None, "0") and delegated.get("at") not in (None, "0"), read("delegated")
        delegated_at = time.monotonic()
        check(f"the installer's cron/at denial refuses a component's crontab and at jobs ({delegated})")

        os.kill(first["pid"], signal.SIGKILL)
        wait(lambda: status().get("phase") == "running" and status().get("restarts") == 1 and status().get("pid") != first["pid"],
             f"crashed run was not restarted: {status()}")
        wait(lambda: not any(proc.alive(p) for p in first.values()),
             f"descendants survived a component crash: {[n for n, p in first.items() if proc.alive(p)]}")
        assert daemon_pid() == main, "the daemon itself restarted on a component crash"
        check("component crash: restarted by the supervisor; all of its descendants killed, including the detached "
              "env-cleared one (coalition sweep); daemon unaffected")

        wait(lambda: all(read(n) for n in RECORDED) and int(read("pid")) == status().get("pid"), "restarted run did not record itself")
        second = recorded()
        runs = int(job()["runs"])
        os.kill(main, signal.SIGKILL)
        wait(lambda: daemon_pid() not in (0, main), f"launchd did not restart the daemon: {job()}", timeout=60)
        successor = daemon_pid()
        assert int(job()["runs"]) == runs + 1, job()
        wait(lambda: status().get("phase") == "running" and status().get("pid") not in (None, second["pid"]),
             f"successor did not bootstrap the component: {status()} {log()[-3000:]}")
        survivors = [n for n, p in second.items() if proc.alive(p)]
        assert not survivors, f"processes of the killed daemon's component survived: {survivors}"
        assert "refusing to start components" not in log(), log()[-3000:]
        accepted = [line for line in log().splitlines() if "none of its processes can remain" in line]
        assert len(accepted) == 1 and f"previous={main}" in accepted[0] and "rebooted=false" in accepted[0], accepted
        successor_coalition = proc.coalition(successor)
        wait(lambda: instance().get("coalition") == successor_coalition and instance().get("pid") == successor,
             f"successor recorded no sealed coalition: {instance()}")
        live = [pid for pid in proc.members(successor_coalition) if pid != successor]
        assert int(read("pid")) in live, (live, read("pid"))
        check(f"daemon SIGKILL: launchd restarted the job (runs {runs}->{runs + 1}); the successor reaped coalition "
              f"{coalition}, accepted it as empty, bootstrapped the component in coalition {successor_coalition}; "
              "no earlier process survived")

        # Killed during an install: nothing it started remains, the outcome is
        # indeterminate, and it never runs again.
        installs = work / "installs"
        sim.add("0x502", event=dict(component_name="svc", version="1.0.1"),
                recipe=dict(lifecycle={"install": f"echo $$ >> {installs}; {PERL_SETSID} /bin/sleep 300 & echo $! > {work}/install-escaped; exec /bin/sleep 300",
                                       "run": "exec /bin/sleep 300"}))
        wait(lambda: read("install-escaped") and installs.exists(), "install did not start")
        installer, escaped = int(installs.read_text().split()[0]), int(read("install-escaped"))
        assert proc.alive(installer) and proc.alive(escaped)
        before, running = daemon_pid(), status().get("pid")
        os.kill(before, signal.SIGKILL)
        wait(lambda: daemon_pid() not in (0, before), f"launchd did not restart the daemon: {job()}")
        # The journal settles before the supervisor reaps: wait for the
        # successor's own record and a new run of the committed version.
        wait(lambda: instance().get("pid") == daemon_pid() and outcome("0x502").get("indeterminate")
             and status().get("phase") == "running" and status().get("version") == "1.0.0"
             and status().get("pid") not in (None, running),
             f"interrupted install not settled: {outcome('0x502')} {status()} {instance()}")
        assert not proc.alive(installer) and not proc.alive(escaped), "the interrupted install's processes survived"
        time.sleep(3)
        assert installs.read_text().split() == [str(installer)], "the interrupted install ran again"
        assert outcome("0x502").get("status") is None and not outcome("0x502").get("reported"), outcome("0x502")
        check("daemon SIGKILL during an install: its processes reaped, outcome indeterminate and never replayed or "
              "reported; committed svc 1.0.0 running again")

        # Secrets and private state: other users cannot read them.
        log_file = supervised / "svc" / "component.log"
        for path, mode in ((key, 0o600), (STATE, 0o700), (log_file, 0o600)):
            assert path.stat().st_mode & 0o777 == mode and path.stat().st_uid == account.pw_uid, (path, oct(path.stat().st_mode))
        for path in (key, log_file, STATE / "state.json"):
            assert sh("cat", str(path), user="nobody", check=False).returncode != 0, f"nobody read {path}"
        # A graceful stop (exit 0) is not restarted; a start with a key its
        # group can read fails, and keeps failing, before any component runs.
        stopping = status().get("pid")
        sh("launchctl", "kill", "SIGTERM", f"system/{LABEL}")
        wait(lambda: daemon_pid() == 0 and status().get("phase") == "stopped", f"daemon did not stop: {job()} {status()}")
        assert not proc.alive(stopping), "the component outlived a graceful daemon stop"
        key.chmod(0o640)
        sh("launchctl", "kickstart", f"system/{LABEL}")
        wait(lambda: "accessible to its group or others" in log(), f"a group-readable key was accepted: {job()}")
        wait(lambda: daemon_pid() == 0 and job().get("last exit code", "0") not in ("0", "(never exited)"),
             f"the daemon did not exit on a group-readable key: {job()}")
        assert status().get("phase") == "stopped", status()
        # Mode 0600, but an ACL entry lets another user read it: also refused
        # before any component runs. The entry goes on first, so a throttled
        # launchd retry never sees a private key in between.
        refused = log().count("has an access control list")
        sh("chmod", "+a", "user:nobody allow read", str(key))
        key.chmod(0o600)
        assert sh("cat", str(key), user="nobody", check=False).returncode == 0, "the ACL entry did not grant read"
        sh("launchctl", "kickstart", f"system/{LABEL}")
        wait(lambda: log().count("has an access control list") > refused, f"a key with an ACL was accepted: {job()}")
        wait(lambda: daemon_pid() == 0, f"the daemon did not exit on a key with an ACL: {job()}")
        assert status().get("phase") == "stopped", status()
        sh("chmod", "-N", str(key))
        sh("launchctl", "kickstart", f"system/{LABEL}")
        wait(lambda: daemon_pid() > 0 and status().get("phase") == "running" and proc.alive(status().get("pid") or 0),
             f"daemon did not recover with a private key: {job()} {status()}")
        check("secrets: key 0600, state 0700 and component log 0600 owned by the daemon user; another user cannot "
              "read them; a group-readable key, or a 0600 key with an ACL granting another user read, stops the daemon "
              "until fixed")

        wait(lambda: all(read(n) for n in RECORDED) and int(read("pid")) == status().get("pid"), "run did not record itself")
        third = recorded()
        last = proc.coalition(daemon_pid())
        for name in ("stopped", "requested"):
            (work / name).unlink(missing_ok=True)
        sh("launchctl", "bootout", f"system/{LABEL}")
        wait(lambda: not job(), "job still loaded after bootout")
        assert read("requested"), "the daemon did not run the component's shutdown command on bootout"
        assert read("stopped") == "graceful", "the run did not stop gracefully on bootout"
        wait(lambda: not proc.members(last), f"processes left in the job's coalition: {proc.members(last)}", timeout=10)
        assert not any(proc.alive(p) for p in third.values()), "component processes outlived bootout"
        assert status().get("phase") == "stopped", status()
        assert not instance(), "a clean stop kept the instance record"
        assert not sim.errors, sim.errors
        check("launchctl bootout: graceful shutdown command ran; coalition empty; status stopped; instance record cleared")
        # Long enough for cron's minute tick: nothing it could have started runs.
        time.sleep(max(0, 65 - (time.monotonic() - delegated_at)))
        leftover = sh("pgrep", "-U", str(account.pw_uid), "-f", "sleep 30[23]", check=False).stdout.split()
        assert not leftover, f"a delegated job ran as {USER}: {leftover}"
        check(f"no cron/at job of {USER} ran at least 65 s after the attempts")
    finally:
        sh("launchctl", "bootout", f"system/{LABEL}", check=False)
        shim.kill()
        server.shutdown()
        if LOG.exists():
            sys.stdout.write("--- daemon log (tail) ---\n" + log()[-4000:] + "\n")

    head = sh("git", "-C", str(ROOT), "rev-parse", "HEAD", check=False).stdout.strip()
    evidence = {
        "ticket": "SF-075", "head": head, "checks": checks,
        "macos": sh("sw_vers", "-productVersion").stdout.strip(),
        "build": sh("sw_vers", "-buildVersion").stdout.strip(),
        "kernel": platform.version(),
        "machine": platform.machine(),
        "files_sha256": {"org.suifleet.daemon.plist": digest(PACKAGING / f"{LABEL}.plist"),
                         "install.sh": digest(PACKAGING / "install.sh")},
        "daemon_sha256": digest("/usr/local/bin/suifleet"),
    }
    print("EVIDENCE " + json.dumps(evidence))
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--disposable-host", action="store_true", help="confirm this host may be modified")
    parser.add_argument("--evidence", help="write evidence JSON here")
    run(parser.parse_args())
