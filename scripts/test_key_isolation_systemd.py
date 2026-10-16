#!/usr/bin/env python3
"""Device-key isolation (SF-053) under the shipped units on real systemd and
cgroup v2, against the local simulator (whose gRPC shim verifies every user
signature with Mysten's sui-crypto): no keys of value, gas, public network or
hardware. Uses the disposable harness of scripts/test_supervisor_systemd.py
(host side: needs Docker).

  migration          -> an existing key owned by the daemon user (old layout)
                        is handed to suifleet-signer by the shipped provisioning
                        unit; the device address is unchanged
  0x901 probe        -> a supervised component running in suifleet.service's
                        sandbox, as the daemon user, cannot: read device.key or
                        signer.toml; see the signer's processes in /proc; find
                        key material in its environment or inherited fds; get a
                        signature from the signing socket (refused: not the
                        unit's main process). It can read the public address.
  reports            -> the daemon's status reports are signed by the service
                        and accepted by the shim's signature verification,
                        including after the signer and the daemon restart
"""
import argparse
import base64
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
from test_supervisor_systemd import host, sh, show  # noqa: E402

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
attempt("export", lambda: ask({"op": "export"}))
json.dump(out, open("probe.json.tmp", "w"))
os.rename("probe.json.tmp", "probe.json")
import time
while True:
    time.sleep(1)
'''.replace("KEY", repr(KEY_HEX))


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
        sim.add(id)
        event = sim.events[-1]["parsedJson"]
        event["component_name"] = name
        sim.recipes[event["recipe_blob_id"]].update(componentName=name, lifecycle=lifecycle)

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
    (etc / "signer.toml").write_text(f'manifest_path = "deployment.json"\ndevice_cap_id = "{CAP}"\n'
        f'keypair_path = "{key}"\ncaller_unit = "suifleet.service"\n')
    sh("chown", "root:suifleet-signer", str(etc / "signer.toml"))
    (etc / "signer.toml").chmod(0o640)
    (etc / "config.toml").write_text('[sui]\nmanifest_path = "deployment.json"\n'
        f'[device]\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
        '[device.signer]\nkind = "service"\nsocket = "/run/suifleet-signer/sign.sock"\n'
        f'[walrus]\naggregator_url = "{url}"\n'
        '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
        '[runtime]\nstate_dir = "/var/lib/suifleet"\npoll_seconds = 1\ncomponent_startup_seconds = 1\n')
    for name in ("deployment.json", "config.toml"):
        sh("chown", "root:suifleet", str(etc / name))
        (etc / name).chmod(0o640)
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

    try:
        sh("systemctl", "start", "suifleet-signer.socket", "suifleet")
        wait(lambda: journal().get("initialized"), "daemon did not start through the signing service")
        stat = os.stat(key)
        import pwd
        assert pwd.getpwuid(stat.st_uid).pw_name == "suifleet-signer" and stat.st_mode & 0o777 == 0o600, (stat.st_uid, oct(stat.st_mode))
        assert (etc / "device.address").read_text().strip() == ADDRESS
        assert show("ActiveState") == "active"
        check(f"migration: the existing key now belongs to suifleet-signer (0600); address unchanged ({ADDRESS[:12]}…)")

        deploy("0x901", "probe", {"run": "exec python3 -c " + "'" + PROBE.replace("'", "'\\''") + "'"})
        work = state / "supervised" / "probe" / "work"
        wait(lambda: (work / "probe.json").exists(), "the probe component did not run")
        probe = json.loads((work / "probe.json").read_text())
        for name in ("read_key", "read_signer_config"):
            assert not probe[name]["ok"] and "PermissionError" in probe[name]["error"], (name, probe[name])
        signer_uid = pwd.getpwnam("suifleet-signer").pw_uid
        assert probe["visible_uids"]["ok"] and signer_uid not in probe["visible_uids"]["value"], probe["visible_uids"]
        assert not probe["env_has_key"], "key material in the component environment"
        assert not any(re.search("KEY|PIN|SECRET|SIGNER|CREDENTIAL", n) for n in probe["env_names"]), probe["env_names"]
        leaked = {f: t for f, t in probe["fd_targets"].items() if int(f) > 2 and not t.startswith("/proc/")}
        assert not leaked, f"inherited descriptors: {leaked}"
        assert probe["address"]["ok"] and probe["address"]["value"].get("address") == ADDRESS, probe["address"]
        sign = probe["sign"]["value"] if probe["sign"]["ok"] else {}
        assert "may not request signatures" in sign.get("error", ""), probe["sign"]
        assert "error" in (probe["export"]["value"] if probe["export"]["ok"] else {"error": 1}), probe["export"]
        check("component process (daemon user, shipped sandbox): device.key and signer.toml unreadable (EACCES); signer "
              "processes invisible; no key material in env or inherited fds; signing refused (not the main process); "
              "only the public address served")

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
