#!/usr/bin/env python3
"""Device-key isolation (SF-053) under the shipped units on real systemd and
cgroup v2, against the local simulator (whose gRPC shim verifies every user
signature with Mysten's sui-crypto): no keys of value, gas, public network or
hardware. Uses the disposable harness of scripts/test_supervisor_systemd.py
(host side: needs Docker).

  legacy             -> the updated package with an unchanged legacy
                        config.toml (keypair_path, no signer.toml): provisioning
                        leaves the key with the daemon user, which reports
  migration          -> after signer.toml is installed and config.toml switched
                        to the service signer, a daemon restart (root
                        ExecStartPre=) hands the same key to suifleet-signer; the
                        address and journal are unchanged
  0x901 probe        -> a supervised component running in suifleet.service's
                        sandbox, as the daemon user, cannot: read device.key or
                        signer.toml; see the signer's processes in /proc; read
                        the daemon's environ or take its descriptors; find
                        key material in its environment or inherited fds; get a
                        signature from the signing socket (refused: not the
                        unit's main process), neither a report nor telemetry
                        (SF-019). It can read the public address.
  reports            -> the daemon's status reports are signed by the service
                        and accepted by the shim's signature verification,
                        including after the signer and the daemon restart
  0x904 orphan       -> a component SIGKILLs the daemon, is reparented to PID 1
                        inside suifleet.service's cgroup (what a parent-PID
                        rule would accept) and asks for signatures: every
                        request is refused as not the recorded main process;
                        the restarted daemon reports again. The shipped
                        KillMode=mixed removes such orphans within milliseconds;
                        a test-only drop-in (KillMode=process, Restart=no) holds
                        the orphan alive so the refusal is observed every run
  hijack             -> a component-written daemon-update launcher state (its own
                        executable as current/pending) is ignored: the shipped
                        config cannot enable [daemon_update] with the service
                        signer (the real binary refuses it), and the restarted
                        main process is the base binary. A main process running
                        any other executable (what an obeyed launcher state
                        would start) is never recorded: the root recorder
                        removes the record, so the signing service refuses
                        that process's otherwise valid report requests
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_supervisor_systemd import UNIT_CGROUP, alive, cgroup, host, sh, show  # noqa: E402

CONTAINER = "suifleet-key-isolation-systemd"
KEY_HEX = "07" * 32  # The simulator's test identity, not a secret.

# Runs as the component: every attempt is recorded, none may succeed.
PROBE = r'''
import json, os, socket, base64
out = {}
def attempt(name, fn):
    try:
        out[name] = {"ok": True, "value": fn()}
    except Exception as e:
        out[name] = {"ok": False, "error": type(e).__name__ + ": " + str(e)}
attempt("read_key", lambda: open("/etc/suifleet/device.key").read())
attempt("read_signer_config", lambda: open("/etc/suifleet/signer.toml").read())
def signer_processes():
    seen = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            uid = int(open(f"/proc/{pid}/status").read().split("Uid:")[1].split()[0])
        except Exception:
            continue
        seen.append(uid)
    return sorted(set(seen))
attempt("visible_uids", signer_processes)
# The daemon (our parent) is non-dumpable: no /proc access, no descriptor theft.
attempt("daemon_environ", lambda: open(f"/proc/{os.getppid()}/environ", "rb").read().decode(errors="replace"))
def steal_fds():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = libc.syscall(434, os.getppid(), 0)  # pidfd_open
    if pidfd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    try:
        stolen = [fd for fd in range(64) if libc.syscall(438, pidfd, fd, 0) >= 0]  # pidfd_getfd
    finally:
        os.close(pidfd)
    if not stolen:
        raise OSError(ctypes.get_errno(), "pidfd_getfd: " + os.strerror(ctypes.get_errno()))
    return stolen
attempt("daemon_fds", steal_fds)
env = dict(os.environ)
out["env_has_key"] = any(KEY in v for v in env.values())
out["env_names"] = sorted(env)
out["fds"] = sorted(int(f) for f in os.listdir("/proc/self/fd"))
fd_targets = {}
for f in os.listdir("/proc/self/fd"):
    try:
        fd_targets[f] = os.readlink(f"/proc/self/fd/{f}")
    except OSError:
        pass
out["fd_targets"] = fd_targets
def ask(body):
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/suifleet-signer/sign.sock")
    s.sendall((json.dumps(body) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    return json.loads(data)
attempt("address", lambda: ask({"op": "address"}))
attempt("sign", lambda: ask({"op": "sign", "tx": base64.b64encode(b"\0" * 64).decode()}))
attempt("sign_telemetry", lambda: ask({"op": "sign_telemetry", "message": base64.b64encode(b"{}").decode()}))
attempt("export", lambda: ask({"op": "export"}))
json.dump(out, open("probe.json.tmp", "w"))
os.rename("probe.json.tmp", "probe.json")
import time
while True:
    time.sleep(1)
'''.replace("KEY", repr(KEY_HEX))


# Runs once as a component: kills the daemon, then asks for signatures as an
# orphan in the unit's cgroup until systemd kills it.
ORPHAN = r'''
import json, os, signal, socket, base64
if os.path.exists("attacked"):
    signal.pause()
open("attacked", "w").close()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
log = open("orphan.jsonl", "a", buffering=1)
log.write(json.dumps({"daemon": os.getppid(), "pid": os.getpid()}) + "\n")
sockets = []
for _ in range(8):
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/suifleet-signer/sign.sock")
    sockets.append(s)
os.kill(os.getppid(), signal.SIGKILL)
while os.getppid() != 1:
    pass
tx = base64.b64encode(b"\0" * 64).decode()
for s in sockets:
    entry = {"ppid": os.getppid(), "cgroup": open("/proc/self/cgroup").read().strip()}
    try:
        s.sendall((json.dumps({"op": "sign", "tx": tx}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        entry["response"] = json.loads(data)
    except Exception as e:
        entry["exception"] = type(e).__name__ + ": " + str(e)
    log.write(json.dumps(entry) + "\n")
signal.pause()
'''


def inside(args):
    sys.path.insert(0, "/workspace/scripts")
    import test_ota
    from test_ota import ADDRESS, CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc

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

    def deploy(id, name, lifecycle):
        sim.add(id, event=dict(component_name=name), recipe=dict(lifecycle=lifecycle))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test_ota.SHIM = Path("/workspace/suifleet-edge/target/debug/examples/grpc_sim_shim")
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    url = f"http://127.0.0.1:{server.server_port}"
    files = Path("/workspace/suifleet-edge/meta-suifleet/recipes-edge/suifleet/files")

    # Users and files as the recipe installs them.
    sh("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "--user-group", "suifleet")
    sh("useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "--user-group", "suifleet-signer")
    sh("install", "-m", "0755", "/workspace/suifleet-edge/target/debug/suifleet", "/usr/bin/suifleet")
    sh("install", "-D", "-m", "0755", str(files / "suifleet-provision"), "/usr/libexec/suifleet-provision")
    units = {}
    for unit in ("suifleet.service", "suifleet-provision.service", "suifleet-signer.service", "suifleet-signer.socket"):
        units[unit] = hashlib.sha256((files / unit).read_bytes()).hexdigest()
        sh("install", "-m", "0644", str(files / unit), f"/etc/systemd/system/{unit}")
    etc = Path("/etc/suifleet")
    etc.mkdir(mode=0o750, exist_ok=True)
    sh("chown", "root:suifleet", str(etc))
    # Old layout: the daemon user owned the key.
    key = etc / "device.key"
    key.write_text(KEY_HEX)
    sh("chown", "suifleet:suifleet", str(key))
    key.chmod(0o600)
    manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
        rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
        shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
        supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
        walrus={"publisher_url": url, "aggregator_url": url})
    (etc / "deployment.json").write_text(json.dumps(manifest))
    def write_config(signer, extra=""):
        path = etc / "config.toml"
        path.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n' + signer +
            f'[walrus]\naggregator_url = "{url}"\n'
            '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
            '[runtime]\nstate_dir = "/var/lib/suifleet"\npoll_seconds = 1\nevent_discovery_seconds = 1\ncomponent_startup_seconds = 1\n' + extra)
        sh("chown", "root:suifleet", str(path))
        path.chmod(0o640)
        return path

    # Old layout: the daemon loads the key itself and there is no signer.toml.
    write_config(f'keypair_path = "{key}"\n')
    sh("chown", "root:suifleet", str(etc / "deployment.json"))
    # signer.toml names the manifest relative to itself; the signer reads it too.
    (etc / "deployment.json").chmod(0o644)
    sh("systemctl", "daemon-reload")
    state = Path("/var/lib/suifleet")

    def journal():
        try:
            return json.loads((state / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def outcome(id):
        return journal().get("deployments", {}).get(id, {})

    def wait(condition, message, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.2)
        raise AssertionError(message)

    def reported(id):
        return any(i == id for i, _ in sim.reports)

    import pwd

    def key_owner():
        stat = os.stat(key)
        assert stat.st_mode & 0o777 == 0o600, oct(stat.st_mode)
        return pwd.getpwuid(stat.st_uid).pw_name

    try:
        # A package update reaching a device whose configuration is unchanged.
        sh("systemctl", "start", "suifleet-signer.socket", "suifleet")
        wait(lambda: journal().get("initialized"), "the legacy daemon did not start")
        assert key_owner() == "suifleet", key_owner()
        deploy("0x900", "legacy", {})
        wait(lambda: reported("0x900"), "the legacy daemon could not report after the package update")
        legacy = outcome("0x900")
        assert legacy.get("status") in (1, 2), legacy
        check("legacy: with an unchanged config.toml (keypair_path) and no signer.toml, provisioning leaves the key with "
              "the daemon user (0600), which keeps reporting")

        (etc / "signer.toml").write_text(f'manifest_path = "deployment.json"\ndevice_cap_id = "{CAP}"\n'
            f'keypair_path = "{key}"\ncaller_unit = "suifleet.service"\n')
        sh("chown", "root:suifleet-signer", str(etc / "signer.toml"))
        (etc / "signer.toml").chmod(0o640)
        write_config('[device.signer]\nkind = "service"\nsocket = "/run/suifleet-signer/sign.sock"\n')
        sh("systemctl", "restart", "suifleet")
        wait(lambda: show("ActiveState") == "active", "the daemon did not restart on the service signer")
        assert key_owner() == "suifleet-signer", key_owner()
        assert (etc / "device.address").read_text().strip() == ADDRESS
        assert outcome("0x900") == legacy, "the journal was not preserved"
        record = Path("/run/suifleet-signer/daemon.main")
        wait(lambda: record.exists() and record.read_text().split()[0] == show("MainPID"), "the main process was not recorded")
        record_stat = os.stat(record)
        assert record_stat.st_uid == 0 and record_stat.st_mode & 0o777 == 0o644, (record_stat.st_uid, oct(record_stat.st_mode))
        check(f"migration: after switching config.toml to the service signer, the daemon restart handed the same key to "
              f"suifleet-signer (0600) without a reboot; address ({ADDRESS[:12]}…) and journal unchanged; the unit "
              "recorded its MainPID as root")

        deploy("0x901", "probe", {"run": "exec python3 -c " + "'" + PROBE.replace("'", "'\\''") + "'"})
        work = state / "supervised" / "probe" / "work"
        wait(lambda: (work / "probe.json").exists(), "the probe component did not run")
        probe = json.loads((work / "probe.json").read_text())
        for name in ("read_key", "read_signer_config"):
            assert not probe[name]["ok"] and "PermissionError" in probe[name]["error"], (name, probe[name])
        for name in ("daemon_environ", "daemon_fds"):
            assert not probe[name]["ok"], (name, probe[name])
        signer_uid = pwd.getpwnam("suifleet-signer").pw_uid
        assert probe["visible_uids"]["ok"] and signer_uid not in probe["visible_uids"]["value"], probe["visible_uids"]
        assert not probe["env_has_key"], "key material in the component environment"
        assert not any(re.search("KEY|PIN|SECRET|SIGNER|CREDENTIAL", n) for n in probe["env_names"]), probe["env_names"]
        leaked = {f: t for f, t in probe["fd_targets"].items() if int(f) > 2 and not t.startswith("/proc/")}
        assert not leaked, f"inherited descriptors: {leaked}"
        assert probe["address"]["ok"] and probe["address"]["value"].get("address") == ADDRESS, probe["address"]
        sign = probe["sign"]["value"] if probe["sign"]["ok"] else {}
        assert "may not request signatures" in sign.get("error", ""), probe["sign"]
        telemetry = probe["sign_telemetry"]["value"] if probe["sign_telemetry"]["ok"] else {}
        assert "may not request telemetry signatures" in telemetry.get("error", ""), probe["sign_telemetry"]
        assert "error" in (probe["export"]["value"] if probe["export"]["ok"] else {"error": 1}), probe["export"]
        check("component process (daemon user, shipped sandbox): device.key and signer.toml unreadable (EACCES); signer "
              "processes invisible; no key material in env or inherited fds; the daemon's environ and descriptors "
              f"unavailable ({probe['daemon_environ']['error']}; {probe['daemon_fds']['error']}); report and "
              "telemetry signing refused (not the main process); only the public address served")

        wait(lambda: reported("0x901"), "the daemon's status report was not signed and accepted")
        assert outcome("0x901").get("status") == 1
        check("the daemon's status report was signed by the service and accepted by the shim's signature verification")

        sh("systemctl", "restart", "suifleet-signer.service")
        deploy("0x902", "plain", {})
        wait(lambda: reported("0x902"), "no report after the signing service restarted")
        sh("systemctl", "restart", "suifleet")
        deploy("0x903", "plain2", {})
        wait(lambda: reported("0x903"), "no report after the daemon restarted")
        log = re.sub(r"\x1b\[[0-9;]*m", "", sh("journalctl", "-u", "suifleet-signer", "--no-pager", "-o", "cat"))
        assert "Signing request refused" in log
        assert not sim.errors, sim.errors
        check("reports keep being signed after the signer and the daemon restart; the refused component request is logged")

        before = show("MainPID")
        dropin = Path("/etc/systemd/system/suifleet.service.d/hold-orphans.conf")
        dropin.parent.mkdir(exist_ok=True)
        dropin.write_text("[Service]\nKillMode=process\nRestart=no\n")
        sh("systemctl", "daemon-reload")
        deploy("0x904", "orphan", {"run": "exec python3 -c " + "'" + ORPHAN.replace("'", "'\\''") + "'"})
        attempts_log = state / "supervised" / "orphan" / "work" / "orphan.jsonl"

        def attempts():
            try:
                return [json.loads(line) for line in attempts_log.read_text().splitlines() if line.endswith("}")]
            except OSError:
                return []
        wait(lambda: len(attempts()) == 9, "the orphan did not finish its signing requests")
        header, tries = attempts()[0], attempts()[1:]
        assert str(header["daemon"]) == before, (header, before)
        assert show("MainPID") == "0", "the daemon was not killed"
        orphan = header["pid"]
        stat = Path(f"/proc/{orphan}/stat").read_text().rsplit(")", 1)[1].split()
        assert stat[1] == "1" and cgroup(orphan) == UNIT_CGROUP, (stat[:2], cgroup(orphan))
        for attempt in tries:
            assert attempt["ppid"] == 1 and attempt["cgroup"] == "0::" + UNIT_CGROUP, attempt
            assert "may not request signatures" in attempt.get("response", {}).get("error", ""), attempt
        sh("kill", "-9", str(orphan))
        wait(lambda: not alive(orphan), "the orphan survived SIGKILL")
        dropin.unlink()
        sh("systemctl", "daemon-reload")
        sh("systemctl", "start", "suifleet")
        wait(lambda: show("MainPID") not in ("0", before) and record.read_text().split()[0] == show("MainPID"),
             "the daemon did not restart and record its main process")
        deploy("0x905", "plain3", {})
        wait(lambda: reported("0x905"), "no report after the daemon was killed by a component")
        check(f"a component killed the daemon and, reparented to PID 1 in suifleet.service's cgroup, made {len(tries)} "
              "signing requests (ppid 1, unit cgroup verified as root): all refused as not the recorded main process; the restarted daemon reports again")

        # A component (daemon user) writes launcher state naming its own executable.
        install = state / "daemon"
        evil = install / "versions" / "9.9.9-ee" / "suifleet"
        evil.parent.mkdir(parents=True, exist_ok=True)
        sh("install", "-m", "0755", "/usr/bin/suifleet", str(evil))
        (install / "current").write_text(str(evil))
        (install / "handoff.json").write_text(json.dumps({"deployment": "0x9ee", "version": "9.9.9", "candidate": str(evil),
            "previous": str(evil), "attempts": 0, "phase": "pending", "error": None}))
        sh("chown", "-R", "suifleet:suifleet", str(install))
        updating = write_config('[device.signer]\nkind = "service"\nsocket = "/run/suifleet-signer/sign.sock"\n',
                                '[daemon_update]\nenabled = true\ncomponent = "suifleet"\n')
        refused = subprocess.run(["/usr/bin/suifleet", "--check-config", str(updating)], capture_output=True, text=True)
        assert refused.returncode != 0 and "daemon_update cannot be enabled with the service signer" in refused.stderr, refused
        write_config('[device.signer]\nkind = "service"\nsocket = "/run/suifleet-signer/sign.sock"\n')
        before = show("MainPID")
        sh("systemctl", "restart", "suifleet")
        wait(lambda: show("MainPID") not in ("0", before) and record.read_text().split()[0] == show("MainPID"),
             "the daemon did not restart and record its main process")
        assert os.readlink(f"/proc/{show('MainPID')}/exe") == "/usr/bin/suifleet", os.readlink(f"/proc/{show('MainPID')}/exe")
        deploy("0x906", "plain4", {})
        wait(lambda: reported("0x906"), "no report after the launcher state was tampered with")

        # What an obeyed launcher state would start: the unit's main process runs
        # the component's executable (a full daemon, with this valid config and
        # pending deployments). The root recorder must not record it.
        hijack = Path("/etc/systemd/system/suifleet.service.d/hijack.conf")
        hijack.write_text(f"[Service]\nExecStart=\nExecStart={evil}\n")
        sh("systemctl", "daemon-reload")
        def refusals():
            text = re.sub(r"\x1b\[[0-9;]*m", "", sh("journalctl", "-u", "suifleet-signer", "--no-pager", "-o", "cat"))
            return text.count("Signing request refused")
        refused_before = refusals()
        sh("systemctl", "restart", "suifleet")
        hijacked = show("MainPID")
        assert os.readlink(f"/proc/{hijacked}/exe") == str(evil), os.readlink(f"/proc/{hijacked}/exe")
        log = re.sub(r"\x1b\[[0-9;]*m", "", sh("journalctl", "-u", "suifleet", "--no-pager", "-o", "cat"))
        assert f"main process {hijacked} is not running the base binary" in log, log[-3000:]
        assert not record.exists(), "a main process running another executable was recorded"
        deploy("0x9ef", "hijacked", {})
        wait(lambda: outcome("0x9ef").get("status") in (1, 2), "the hijacked daemon did not try to report")
        wait(lambda: refusals() > refused_before, "the hijacked daemon's report request was not refused")
        time.sleep(3)
        assert not reported("0x9ef"), "a report from a main process running another executable was signed"
        assert show("ActiveState") == "active" and show("MainPID") == hijacked
        hijack.unlink()
        sh("systemctl", "daemon-reload")
        sh("systemctl", "restart", "suifleet")
        wait(lambda: show("MainPID") != "0" and record.read_text().split()[0] == show("MainPID"),
             "the daemon did not restart after the hijack attempt")
        wait(lambda: reported("0x9ef"), "the queued report was not signed once the base daemon ran again")
        deploy("0x907", "plain5", {})
        wait(lambda: reported("0x907"), "no report after the hijack attempt")
        assert not sim.errors, sim.errors
        check("hijack: component-written launcher state naming its own executable is ignored (the real binary refuses "
              "[daemon_update] with the service signer; the restarted main process is /usr/bin/suifleet and reports); a "
              "main process running that executable (a full daemon) is not recorded by the root recorder ('not running the "
              "base binary'), and its otherwise valid report request is refused; the queued report is signed only "
              "after the base daemon runs again")
    finally:
        sh("systemctl", "stop", "suifleet", "suifleet-signer.service", "suifleet-signer.socket", check=False)
        shim.kill()
        server.shutdown()

    print("EVIDENCE " + json.dumps({
        "ticket": "SF-053", "head": args.head, "checks": checks,
        "systemd": sh("systemctl", "--version").splitlines()[0],
        "container_cgroup": sh("stat", "-fc", "%T", "/sys/fs/cgroup"),
        "os": Path("/etc/os-release").read_text().split('PRETTY_NAME="', 1)[1].split('"', 1)[0],
        "units_sha256": units,
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
        host(arguments, script="test_key_isolation_systemd.py", container=CONTAINER)
