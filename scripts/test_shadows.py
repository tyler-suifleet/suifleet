#!/usr/bin/env python3
"""SF-033 device shadows and cloud synchronization with the real daemon. Run
as root in a disposable, network-isolated container (make test-shadows): root
only creates real, distinct component UIDs for SO_PEERCRED. Local chain
simulator and a local reference shadow service; no keys, gas, public network
or hardware.

The reference service is the cloud side of the sync protocol in
docs/SHADOWS.md: compare-and-swap on its own version. It verifies every
request independently of the daemon: the body's SHA-256 against the
manifest, the manifest's Sui personal-message signature (BLAKE2b intent
digest + RFC 8032 Ed25519, from test_streams.py) and that its signer is the
device address.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for
from test_streams import verify_personal_message

ROOT = Path(__file__).resolve().parents[1]
APP, OPS, OTHER = 63001, 63002, 63003
THING = "dev1"
DELTA = f"$aws/things/{THING}/shadow/name/config/update/delta"


class Cloud:
    """Reference shadow service. Modes: ok, down (503), race (one 409 after another writer)."""

    def __init__(self, device_address):
        self.mode, self.device = "down", device_address
        self.shadows = {}  # name -> [version, document or None]
        self.requests, self.puts, self.lock = [], [], threading.Lock()
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if cloud.mode == "down":
                    return self.reply(503, {})
                manifest_bytes = base64.b64decode(self.headers["x-suifleet-manifest"])
                signer = verify_personal_message(manifest_bytes, base64.b64decode(self.headers["x-suifleet-signature"]))
                manifest = json.loads(manifest_bytes)
                request = json.loads(body)
                assert signer == manifest["signer"] == cloud.device, (signer, manifest["signer"])
                assert manifest["kind"] == "shadow-sync" and hashlib.sha256(body).hexdigest() == manifest["body_sha256"]
                assert (request["op"], request["shadow"], request["device_id"]) == (manifest["op"], manifest["shadow"], manifest["device_id"]) \
                    and request["device_id"] == DEVICE
                assert self.headers["authorization"] == "Bearer shadow-secret"
                with cloud.lock:
                    cloud.requests.append(request["op"])
                    version, document = cloud.shadows.get(request["shadow"], [0, None])
                    if request["op"] == "get":
                        return self.reply(200, {"version": version, "document": document})
                    if cloud.mode == "race":
                        # Another writer stores first; this put must lose and be merged again.
                        cloud.mode = "ok"
                        cloud._edit(request["shadow"], lambda d: {**(d or {}), "desired": {**(d or {}).get("desired", {}), "racer": True}})
                        return self.reply(409, {"version": cloud.shadows[request["shadow"]][0]})
                    if request["expected_version"] != version:
                        return self.reply(409, {"version": version})
                    cloud.shadows[request["shadow"]] = [version + 1, request["document"]]
                    cloud.puts.append((request["shadow"], version + 1, request["document"]))
                    return self.reply(200, {"version": version + 1})

            def reply(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _edit(self, name, change):
        version, document = self.shadows.get(name, [0, None])
        self.shadows[name] = [version + 1, change(document)]

    def edit(self, name, change):
        """A cloud-side writer (an application or console), independent of the device."""
        with self.lock:
            self._edit(name, change)

    def get(self, name):
        with self.lock:
            version, document = self.shadows.get(name, [0, None])
            return version, json.loads(json.dumps(document))


def run(evidence=None):
    assert os.geteuid() == 0, "run in a disposable root container (make test-shadows)"
    passed = []
    import builtins
    real_print = builtins.print

    def print(*args):  # noqa: A001 - record PASS lines for the receipt
        text = " ".join(map(str, args))
        if text.startswith("PASS: "):
            passed.append(text[6:])
        real_print(*args, flush=True)
    binary = ROOT / "suifleet-edge/target/debug/suifleet"
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

    class Chain(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                result = dict(jsonrpc="2.0", id=request["id"], result=sim.rpc(request["method"], request["params"]))
            except Exception as error:
                result = dict(jsonrpc="2.0", id=request["id"], error=dict(code=-32000, message=str(error)))
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    chain = ThreadingHTTPServer(("127.0.0.1", 0), Chain)
    threading.Thread(target=chain.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{chain.server_port}")
    with tempfile.TemporaryDirectory(prefix="sf033-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        # The checkout may be private (0700). Give foreign IPC UIDs a
        # readable copy of the actual SDK without changing checkout modes.
        sdk = root / "sdk"
        sdk.mkdir(mode=0o755)
        (sdk / "suifleet_ipc.py").write_bytes((ROOT / "sdk/python/suifleet_ipc.py").read_bytes())
        (sdk / "suifleet_ipc.py").chmod(0o644)
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        address = subprocess.run([str(binary), "--derive-address", str(key)], text=True, capture_output=True, check=True).stdout.strip()
        cloud = Cloud(address)
        token = root / "shadow.token"
        token.write_text("shadow-secret\n")
        token.chmod(0o600)
        url = f"http://127.0.0.1:{chain.server_port}"
        (root / "deployment.json").write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
            chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
            type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": url, "aggregator_url": url})))
        state, socket_path = root / "state", root / "ipc.sock"
        config = root / "config.toml"
        config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            # The application reports its state and follows desired through delta notifications.
            f'[ipc.components.app]\nuid = {APP}\n'
            'operations = ["shadow_get", "shadow_update", "shadow_list", "subscribe", "receive", "ack"]\n'
            '[[ipc.components.app.shadow_policies]]\n'
            'operations = ["aws.greengrass#GetThingShadow", "aws.greengrass#UpdateThingShadow"]\n'
            f'resources = ["$aws/things/{THING}/shadow/name/config", "$aws/things/{THING}/shadow"]\nsections = ["reported"]\n'
            '[[ipc.components.app.shadow_policies]]\n'
            f'operations = ["aws.greengrass#ListNamedShadowsForThing"]\nresources = ["$aws/things/{THING}/shadow/name/config"]\n'
            '[[ipc.components.app.pubsub_policies]]\n'
            f'operations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["{DELTA}"]\n'
            # A local operator component may set desired state and delete.
            f'[ipc.components.ops]\nuid = {OPS}\noperations = ["shadow_get", "shadow_update", "shadow_delete", "shadow_list"]\n'
            '[[ipc.components.ops.shadow_policies]]\noperations = ["*"]\n'
            f'resources = ["$aws/things/{THING}/shadow/name/*", "$aws/things/{THING}/shadow"]\n'
            f'[ipc.components.other]\nuid = {OTHER}\noperations = ["shadow_get", "shadow_update"]\n'
            # A write-only reporter: may update reported on "secret" but never read it.
            '[[ipc.components.other.shadow_policies]]\noperations = ["aws.greengrass#UpdateThingShadow"]\n'
            f'resources = ["$aws/things/{THING}/shadow/name/secret"]\nsections = ["reported"]\n'
            '[pubsub]\n'
            f'[shadows]\nthing_name = "{THING}"\nmax_shadows = 4\n'
            f'[shadows.sync]\nurl = "http://127.0.0.1:{cloud.server.server_port}/v1/shadows"\nbearer_token_file = "{token}"\n'
            'shadows = ["", "config"]\ninterval_seconds = 1\nmax_backoff_seconds = 2\n')
        check = subprocess.run([str(binary), "--check-config", str(config)], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        log = (root / "daemon.log").open("a")
        daemon = {}

        def start():
            daemon["p"] = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)
            wait_for(lambda: socket_path.exists(), "IPC socket did not appear", daemon["p"])

        def stop(kill=False):
            (daemon["p"].kill if kill else daemon["p"].terminate)()
            daemon["p"].wait(timeout=30)
            if socket_path.exists():
                socket_path.unlink()

        def ipc(uid, operation, topic="", payload=None):
            component = {APP: "app", OPS: "ops", OTHER: "other", 0: ""}[uid]
            code = ("import os,sys,json; uid=int(sys.argv[1]); "
                    "uid and (os.setgid(uid), os.setuid(uid)); "
                    "sys.path.insert(0, sys.argv[7]); from suifleet_ipc import Client, IpcError\n"
                    "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                    "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
            out = subprocess.run([sys.executable, "-c", code, str(uid), str(socket_path), component, operation, topic, json.dumps(payload),
                                  str(sdk)], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        def get(name, uid=0):
            return ipc(uid, "shadow_get", name)

        def document(name):
            """The local document as the cloud stores it (no delta)."""
            r = get(name)
            if not r["ok"]:
                return None
            return {k: v for k, v in r["data"]["state"].items() if k != "delta"}

        def status(name):
            detail = ipc(0, "shadows", payload={"shadow": name})["data"]
            assert {k: v for k, v in detail["sync"].items() if k in ("pushes", "pulls", "conflicts", "failures")} == \
                next(s for s in ipc(0, "shadows")["data"]["shadows"] if s["name"] == name)["sync"]
            return detail

        def converged(name):
            return cloud.get(name)[1] == document(name)

        def update(uid, name, state, **extra):
            return ipc(uid, "shadow_update", name, {"state": state, **extra})

        try:
            start()
            # ACLs: operation, resource and section scope; deny by default.
            assert update(OTHER, "config", {"reported": {"x": 1}}) == {"ok": False, "error": "request_failed"}
            assert update(APP, "config", {"desired": {"x": 1}}) == {"ok": False, "error": "request_failed"}, "app may not write desired"
            assert update(APP, "secret", {"reported": {"x": 1}}) == {"ok": False, "error": "request_failed"}
            assert ipc(APP, "shadow_delete", "config") == {"ok": False, "error": "request_failed"}
            assert ipc(APP, "shadows") == {"ok": False, "error": "request_failed"}, "status is administrator only"
            assert update(OPS, "secret", {"desired": {"s": 1}})["ok"]
            assert get("secret", APP) == {"ok": False, "error": "request_failed"}
            assert get("secret", OTHER) == {"ok": False, "error": "request_failed"}
            r = update(OTHER, "secret", {"reported": {"o": 1}})
            assert r == {"ok": True, "data": {"state": {"reported": {"o": 1}}, "version": 2}}, "a write-only caller must not read desired/delta"
            assert update(OTHER, "secret", {})["data"] == {"state": {}, "version": 3}
            r = update(APP, "config", {"reported": {"mode": "idle"}}, clientToken="t-1")
            assert r["ok"] and r["data"]["version"] == 1 and r["data"]["clientToken"] == "t-1", r
            assert ipc(APP, "shadow_list")["data"]["shadows"] == ["config"]
            assert ipc(OPS, "shadow_list")["data"]["shadows"] == ["config", "secret"]
            print("PASS: ACLs: per-component operations, shadow resources and sections (reported-only writer); "
                  "a write-only updater gets its request echoed, not the stored state; deny by default")

            # Delta notifications through durable pub/sub, after cloud sync has run.
            assert ipc(APP, "subscribe", DELTA, {"qos": 1})["ok"]
            cloud.mode = "ok"
            wait_for(lambda: converged("config") and cloud.get("config")[0] >= 1, "initial sync did not push the local shadow")
            r = update(OPS, "config", {"desired": {"mode": "run", "rate": 5}})
            assert r["data"]["state"]["delta"] == {"mode": "run", "rate": 5}, r
            message = ipc(APP, "receive", DELTA)["data"]
            assert message["sender"] == "@shadows" and message["payload"]["state"] == {"mode": "run", "rate": 5}, message
            assert ipc(APP, "ack", DELTA, message["id"])["data"]["acknowledged"]
            print("PASS: delta notification: desired change published on the AWS-shaped delta topic and received with QoS 1")

            # Stale version rejection and other refused requests change nothing.
            version = get("config")["data"]["version"]
            assert update(APP, "config", {"reported": {"mode": "x"}}, version=version - 1) == {"ok": False, "error": "conflict"}
            assert update(APP, "config", {"reported": {"mode": "x"}}, version=True) == {"ok": False, "error": "invalid"}
            assert update(APP, "config", {"reported": {"pad": "z" * 9000}}) == {"ok": False, "error": "too-large"}
            assert update(APP, "config", {"reported": {"a": [1, None]}}) == {"ok": False, "error": "invalid"}
            assert update(APP, "config", {"reported": {"a": [{"b": None}]}}) == {"ok": False, "error": "invalid"}
            assert get("config")["data"]["version"] == version
            r = update(APP, "config", {"reported": {"mode": "run", "rate": 5}}, version=version)
            assert r["ok"] and r["data"]["version"] == version + 1 and "delta" not in r["data"]["state"], r
            print(f"PASS: stale version {version - 1} rejected with conflict; invalid/too-large/null-in-array (also nested in an array object) refused without a version change; "
                  f"version {version} accepted")

            # Cloud → device: a cloud-side desired change is pulled and announced.
            wait_for(lambda: converged("config"), "reported state was not pushed")
            cloud.edit("config", lambda d: {**d, "desired": {**d["desired"], "rate": 10}})
            wait_for(lambda: get("config")["data"]["state"].get("delta") == {"rate": 10}, "cloud desired change was not pulled")
            message = ipc(APP, "receive", DELTA)["data"]
            assert message["payload"]["state"] == {"rate": 10}, message
            ipc(APP, "ack", DELTA, message["id"])
            print("PASS: cloud desired change pulled, versioned locally and announced as a delta to the component")

            # Offline edits on both sides, then reconnect with conflicts.
            cloud.mode = "down"
            update(APP, "config", {"reported": {"rate": 10, "temp": 21}})
            update(OPS, "config", {"desired": {"mode": "eco", "local_only": 1}})
            update(APP, "", {"reported": {"boot": 1}})
            cloud.edit("config", lambda d: {**d, "desired": {**d["desired"], "mode": "turbo", "cloud_only": 2},
                                            "reported": {**d["reported"], "temp": -1}})
            time.sleep(3)
            assert status("config")["sync"]["failures"] >= 1, status("config")
            assert get("config")["data"]["state"]["desired"]["mode"] == "eco", "offline local edits stay local"
            before = status("config")["sync"]["conflicts"]
            cloud.mode = "ok"
            wait_for(lambda: converged("config") and converged(""), "offline edits did not reconcile")
            merged = document("config")
            assert merged["desired"] == {"mode": "turbo", "rate": 10, "local_only": 1, "cloud_only": 2}, merged
            assert merged["reported"] == {"mode": "run", "rate": 10, "temp": 21}, merged
            assert document("") == {"reported": {"boot": 1}}
            wait_for(lambda: status("config")["sync"]["conflicts"] == before + 2, "conflicts were not counted")
            print("PASS: offline edits on both sides reconciled after reconnect: one-sided keys kept from each side; the 2 conflicts "
                  "(desired.mode, reported.temp) settled by owner (cloud owns desired, device owns reported)")

            # A concurrent cloud writer wins the compare-and-swap; the device merges again.
            cloud.mode = "race"
            update(APP, "config", {"reported": {"temp": 22}})
            wait_for(lambda: cloud.mode == "ok" and converged("config"), "a lost compare-and-swap did not converge")
            assert document("config")["desired"]["racer"] is True and document("config")["reported"]["temp"] == 22
            print("PASS: concurrent cloud writer: the device's put lost with 409, it re-read, merged and converged without losing either change")

            # Restart persistence: clean stop and kill -9 keep documents, versions and the sync base.
            snapshot = {n: get(n)["data"] for n in ("", "config", "secret")}
            cloud_versions = {n: cloud.get(n)[0] for n in ("", "config")}
            stop()
            start()
            assert {n: get(n)["data"] for n in snapshot} == snapshot
            stop(kill=True)
            start()
            assert {n: get(n)["data"] for n in snapshot} == snapshot
            time.sleep(3)
            assert {n: cloud.get(n)[0] for n in ("", "config")} == cloud_versions, "an unchanged shadow was pushed again after restart"
            assert {n: get(n)["data"]["version"] for n in snapshot} == {n: s["version"] for n, s in snapshot.items()}
            print("PASS: restart persistence: clean stop and kill -9 keep every document and version; the sync base survives (no redundant pushes)")

            # Offline edit across a crash is pushed once the service returns.
            cloud.mode = "down"
            update(APP, "config", {"reported": {"temp": 23}})
            stop(kill=True)
            start()
            assert get("config")["data"]["state"]["reported"]["temp"] == 23
            cloud.mode = "ok"
            wait_for(lambda: converged("config"), "an offline edit made before a crash was not pushed")
            print("PASS: an offline edit made before kill -9 is kept and pushed after restart and reconnect")

            # Deletion: local deletion propagates; the version sequence continues on recreation.
            deleted = ipc(OPS, "shadow_delete", "config", {"version": get("config")["data"]["version"]})["data"]["version"]
            assert get("config") == {"ok": False, "error": "not-found"}
            wait_for(lambda: cloud.get("config")[1] is None, "local deletion was not propagated")
            assert update(OPS, "config", {"desired": {"mode": "new"}})["data"]["version"] == deleted + 1
            wait_for(lambda: converged("config"), "recreated shadow not pushed")
            # Cloud deletion of the classic shadow (unchanged locally) deletes it here.
            cloud.edit("", lambda d: None)
            wait_for(lambda: get("") == {"ok": False, "error": "not-found"}, "cloud deletion was not pulled")
            # Deleted in the cloud while changed locally: the change survives.
            cloud.mode = "down"
            update(APP, "config", {"reported": {"after": 1}})
            cloud.edit("config", lambda d: None)
            cloud.mode = "ok"
            wait_for(lambda: cloud.get("config")[1] is not None and converged("config"), "modify-versus-delete did not keep the change")
            assert document("config") == {"desired": {"mode": "new"}, "reported": {"after": 1}}
            stop()
            start()
            assert get("") == {"ok": False, "error": "not-found"} and get("config")["data"]["version"] > deleted + 1
            print(f"PASS: deletion: local delete propagated (version {deleted}), recreation continues at {deleted + 1}, cloud delete pulled, "
                  "modify-versus-delete keeps the change; tombstones persist across restart")

            # Every request was signed and verified; the device never sent an unknown op.
            assert set(cloud.requests) == {"get", "put"} and len(cloud.puts) >= 6
            log_text = (root / "daemon.log").read_text()
            assert "shadow-secret" not in log_text
            print(f"PASS: {len(cloud.requests)} sync requests verified independently (body SHA-256, personal-message Ed25519 signature, "
                  "device signer, bearer token); the token never appears in the log")
            if evidence:
                head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
                Path(evidence).write_text(json.dumps({"ticket": "SF-033", "head": head or None, "checks": passed,
                    "fixture": {"file": "interfaces/compat/shadow-cases.json", "sequences": 10, "steps": 55,
                                "executed_by": "make edge-test: shadows::tests::shared_shadow_cases (each step also after reopening the store)"},
                    "signature_verification": "independent: SHA-256 body binding, BLAKE2b personal-message intent digest, RFC 8032 Ed25519 (Python), signer address",
                    "cloud": "local reference shadow service (scripts/test_shadows.py); no public network"},
                    indent=2) + "\n")
        except Exception:
            print((root / "daemon.log").read_text()[-4000:])
            raise
        finally:
            if daemon.get("p") and daemon["p"].poll() is None:
                stop()
            shim.terminate()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
