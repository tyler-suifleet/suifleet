#!/usr/bin/env python3
"""Exercise the real daemon against a local chain/Walrus simulator, without keys or gas.
This verifies integration behavior, not public-network compatibility or hardware OTA.

The daemon speaks Sui gRPC v2. The simulator below keeps scenario state and
assertions in Python; suifleet-edge/examples/grpc_sim_shim.rs serves gRPC in
front of it (see start_grpc). Real-chain behavior is covered separately by
scripts/test_daemon_localnet.py.
"""
from deployment_manifest import REQUIRED_TYPES, object_id
from pathlib import Path
import json
_PROTOCOL_RECIPE_VERSION = json.loads((Path(__file__).resolve().parents[1] / "interfaces/recipe.schema.json").read_text())["properties"]["schemaVersion"]["enum"][0]
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PACKAGE = object_id("0x3")
ORIGIN = object_id("0x1")
RECIPE_ORIGIN = object_id("0x2")
REGISTRY = "0xbe"
UPGRADE_CAP = "0xc0"
TYPE_ORIGINS = {name: ORIGIN for name in REQUIRED_TYPES}
TYPE_ORIGINS["deployment_manager::RecipeDigestKey"] = RECIPE_ORIGIN
TYPE_ORIGINS["device_cap::DeviceAddressKey"] = PACKAGE
for _name in ("device_registry::IdentityKey", "device_registry::Identity", "device_registry::DeviceIdentityChanged",
              "device_registry::CustodyKey", "device_registry::DeviceOwnerCap"):
    TYPE_ORIGINS[_name] = PACKAGE  # Introduced by a later upgrade.
DEVICE = "0xde"
CAP = "0xca"
OWNER_CAP = "0xc1"
CUSTODY_FIELD = "0xcf"
OPERATOR = "0xad"
GROUP = "0x10"
ADDRESS = "0xa0ccc8bcc83f6c628340134f8546a21e0618fd1aaa02432bba454c4a2c2233da"
SHIM = Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/examples/grpc_sim_shim"
ARTIFACT = b"test OTA artifact, not a recipe"
HASH = hashlib.sha256(ARTIFACT).hexdigest()


def start_grpc(simulator_url):
    """Start the gRPC shim in front of a simulator; returns (process, gRPC URL)."""
    process = subprocess.Popen([str(SHIM), simulator_url], stdout=subprocess.PIPE, text=True)
    line = process.stdout.readline()
    if not line.startswith("listening "):
        process.kill()
        raise AssertionError(f"gRPC shim did not start: {line!r}")
    return process, f"http://127.0.0.1:{int(line.split()[1])}"


def scanned(count):
    """The journal cursor after the daemon has read the first `count` events."""
    return {"after": base64.b64encode(count.to_bytes(8, "little")).decode()}


def wait_for(condition, message, process=None, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.05)
    raise AssertionError(message)


def completed_polls(log_path):
    """Completed daemon cycles; transport reads can occur many times per cycle."""
    return Path(log_path).read_text().count("Chain reads this poll")


class Simulator:
    def __init__(self, device=DEVICE, cap=CAP):
        self.device, self.cap = device, cap
        self.latest_package = PACKAGE
        self.chain = "1234abcd"
        self.events = []
        self.recipes = {}
        self.reports = []
        self.allow_reports = False
        self.fail_execution = True
        self.deleted = set()
        self.schema_fields = {}
        self.downloads = []
        self.built = []
        self.errors = []
        self.identity = {"signer": ADDRESS, "revoked": False, "owner_cap": OWNER_CAP}
        self.chain_checks = 0
        self.reads = {}
        self.read_bytes = {}
        self.discovery_log = []
        self.discovery_seen = set()
        self.truncate_next_artifact = True  # First artifact transfer drops mid-body.
        self.ranges = []
        # Desired state (SF-013): group objects, DeviceAddedToGroup events,
        # per-group desired components and records marked managed.
        self.groups = {GROUP: {"admin": OPERATOR, "device_ids": [self.device]}}
        self.memberships = []
        self.desired = {}
        self.managed = set()
        self.controls = {}

    def add(self, id, *, creator=OPERATOR, groups=None, direct=True, bad_hash=False, run=False, expired=False, event=None, recipe=None):
        """Publish deployment `id`. `event`/`recipe` override fields before
        publication: a polling daemon must never see a half-built deployment."""
        digest = "f" * 64 if bad_hash else HASH
        fields = dict(deployment_id=id, component_name=f"app-{id}", version="1.0.0",
            recipe_blob_id=f"recipe-{id}", artifact_sha256=digest,
            target_groups=groups or [], target_devices=[self.device] if direct else [], creator=creator) | (event or {})
        self.recipes[fields["recipe_blob_id"]] = dict(schemaVersion=_PROTOCOL_RECIPE_VERSION, componentName=fields["component_name"],
            componentVersion=fields["version"], artifactBlobId="expired" if expired else "artifact", artifactSha256=fields["artifact_sha256"],
            lifecycle={"run": "sleep 10"} if run else {}, configuration={}) | (recipe or {})
        self.events.append(dict(id={"txDigest":f"event-{len(self.events)}", "eventSeq":"0"}, parsedJson=fields))

    def obj(self, id):
        if id in self.deleted:
            return {"error":{"code":"deleted","object_id":id}}
        if id == UPGRADE_CAP:
            return {"data":{"content":{"type":"0x2::package::UpgradeCap","fields":{"package":self.latest_package,"version":"3"}}}}
        if id == REGISTRY:
            type, fields, owner = "device_registry::DeviceRegistry", {}, None
        elif id == self.device:  # Activated: held by the registry, operated through OWNER_CAP.
            type, fields, owner = "device_registry::Device", {"device_address":ADDRESS}, {"ObjectOwner":CUSTODY_FIELD}
        elif id == OWNER_CAP:
            type, fields, owner = "device_registry::DeviceOwnerCap", {"device_id":self.device}, OPERATOR
        elif id == self.cap:
            type, fields, owner = "device_cap::DeviceCap", {"device_id":self.device}, ADDRESS
        elif id in self.groups:
            type, fields, owner = "device_group::DeviceGroup", dict(self.groups[id]), None
        else:
            event = next(e["parsedJson"] for e in self.events if e["parsedJson"]["deployment_id"] == id)
            fields = {key: event[key] for key in ("creator", "component_name", "version", "recipe_blob_id", "artifact_sha256")}
            type, owner = "deployment_manager::DeploymentRecord", None
            fields["schema_version"] = "1"
        fields.update(self.schema_fields.get(id, {}))
        if fields.get("schema_version") is None:
            fields.pop("schema_version", None)
        owner = {"AddressOwner":owner} if isinstance(owner, str) else owner or {"Shared":{"initial_shared_version":"1"}}
        return {"data":{"objectId":id,"owner":owner,
            "content":{"type":f"{TYPE_ORIGINS[type]}::{type}","fields":fields}}}

    def rpc(self, method, params):
        if method == "shim_read":
            self.reads[params[0]] = self.reads.get(params[0], 0) + 1
            self.read_bytes[params[0]] = self.read_bytes.get(params[0], 0) + params[1]
            return True
        if method == "shim_version_salt":
            return self.chain
        if method == "shim_discovery_events":
            candidates = [(f"{ORIGIN}::deployment_manager::DeploymentCreated", e["parsedJson"]) for e in self.events]
            candidates += [(f"{ORIGIN}::device_group::DeviceAddedToGroup", dict(group_id=g, device_id=d)) for g,d in self.memberships]
            for kind, event in candidates:
                key = (kind, json.dumps(event, sort_keys=True))
                if key not in self.discovery_seen:
                    self.discovery_seen.add(key)
                    self.discovery_log.append(dict(event_type=kind, json=event))
            return list(self.discovery_log)
        if method == "sui_getChainIdentifier":
            self.chain_checks += 1
            return self.chain
        # Simulator views used by the gRPC shim.
        if method == "shim_height":
            return len(self.events)
        if method == "shim_fields":
            key = lambda name: f"{TYPE_ORIGINS[name]}::{name}"
            empty = "00"  # An empty key struct is one `false` dummy byte.
            fields = [[self.cap, key("device_cap::DeviceAddressKey"), empty],
                      [REGISTRY, key("device_registry::IdentityKey"), object_id(self.device)[2:]]]
            fields += [[group, key("desired_state::DesiredKey"), empty] for group in self.groups]
            for event in self.events:
                parent = event["parsedJson"]["deployment_id"]
                fields += [[parent, key(name), empty] for name in (
                    "deployment_manager::RecipeDigestKey", "deployment_manager::TargetDevicesKey",
                    "deployment_manager::TargetGroupsKey", "deployment_manager::ManagedKey", "deployment_manager::ControlKey")]
            return fields
        if method == "shim_events":
            if params[0] == f"{ORIGIN}::device_group::DeviceAddedToGroup":
                return [dict(group_id=group, device_id=device) for group, device in self.memberships]
            assert params[0] == f"{ORIGIN}::deployment_manager::DeploymentCreated"
            return [e["parsedJson"] for e in self.events]
        if method == "sui_getObject" and params[0] == PACKAGE:
            return {"data": {"version": "3", "bcs": {"dataType": "package", "typeOriginTable": [
                {"module_name": name.split("::")[0], "datatype_name": name.split("::")[1], "package": origin}
                for name, origin in TYPE_ORIGINS.items()]}}}
        if method == "suix_queryEvents":
            assert params[0]["MoveEventType"] == f"{ORIGIN}::deployment_manager::DeploymentCreated"
            cursor, limit, descending = params[1:]
            events = list(reversed(self.events)) if descending else list(self.events)
            if cursor is not None:
                position = next(i for i, e in enumerate(events) if e["id"] == cursor)
                events = events[position+1:]
            page = events[:min(limit, 2)]
            return {"data":page, "nextCursor":page[-1]["id"] if page else cursor,"hasNextPage":len(events)>len(page)}
        if method == "sui_getObject":
            return self.obj(params[0])
        if method == "suix_getDynamicFieldObject":
            if params[0] == REGISTRY:
                assert params[1]["type"] == f"{PACKAGE}::device_registry::IdentityKey"
                return {"data":{"content":{"fields":{"value":self.identity}}}}
            if params[0] == self.cap:
                assert params[1]["type"] == f"{PACKAGE}::device_cap::DeviceAddressKey"
                return {"data":{"content":{"fields":{"value":ADDRESS}}}}
            if params[1]["type"].endswith("::desired_state::DesiredKey"):
                state = self.desired.get(params[0])
                value = state and {"revision": str(state[0]), "updated_at": "0",
                    "components": {"contents": [{"key": k, "value": v} for k, v in state[1].items()]}}
                return {"data":{"content":{"fields":{"value":value}}}}
            if params[1]["type"].endswith("::deployment_manager::ControlKey"):
                return {"data":{"content":{"fields":{"value":self.controls.get(params[0])}}}}
            if params[1]["type"].endswith("::deployment_manager::ManagedKey"):
                return {"data":{"content":{"fields":{"value":True if params[0] in self.managed else None}}}}
            event = next(e["parsedJson"] for e in self.events if e["parsedJson"]["deployment_id"] == params[0])
            if params[1]["type"].endswith("RecipeDigestKey"):
                assert params[1]["type"] == f"{RECIPE_ORIGIN}::deployment_manager::RecipeDigestKey"
                recipe = self.recipes[event["recipe_blob_id"]]
                digest = hashlib.sha256(json.dumps(recipe).encode()).hexdigest()
                if event["deployment_id"] == "0x109":
                    digest = "0" * 64
                return {"data":{"content":{"fields":{"value":digest}}}}
            assert params[1]["type"] in (f"{ORIGIN}::deployment_manager::TargetDevicesKey", f"{ORIGIN}::deployment_manager::TargetGroupsKey")
            field = "target_devices" if params[1]["type"].endswith("TargetDevicesKey") else "target_groups"
            return {"data":{"content":{"fields":{"value":event[field]}}}}
        if method == "unsafe_moveCall":
            assert params[0] == ADDRESS and params[1] == PACKAGE
            assert params[2] == "deployment_manager"
            args = params[5]
            assert args[1] == REGISTRY and args[2] == self.cap
            assert (params[3] == "report_verified_status" and len(args) == 4) or (params[3] == "report_verified_status_for_group" and args[3] in self.groups and len(args) == 5)
            if not self.allow_reports:
                raise ConnectionError("simulated unavailable transaction service")
            self.built.append((args[0], int(args[-1])))
            return {"txBytes":base64.b64encode(bytes([1,2,3,4])).decode()}
        if method == "sui_executeTransactionBlock":
            # The shim verified the user signature with Mysten's sui-crypto.
            assert params[1] == [f"verified:{ADDRESS}"], params[1]
            if self.fail_execution:
                self.fail_execution = False
                return {"digest":"failed-digest", "effects":{"status":{"status":"failure","error":"simulated abort"}}}
            self.reports.append(self.built[-1])
            return {"digest":"success-digest", "effects":{"status":{"status":"success"}}}
        raise AssertionError(f"unexpected RPC method {method}")


def run(binary, firmware_helper=False, daemon_user=None, platform_installer=False, poll_reads=False, baseline_profile=False):
    """With firmware_helper, updates go through `suifleet --firmware-helper`, run
    here as the path unit would. With daemon_user (requires root), the daemon
    runs as that user while the helper keeps root, as in the systemd units."""
    sim = Simulator()
    if poll_reads or baseline_profile:
        sim.memberships.append((GROUP, DEVICE))
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
                result = sim.rpc(request["method"], request["params"])
                response = {"jsonrpc":"2.0","id":request["id"],"result":result}
            except Exception as error:
                if not isinstance(error, ConnectionError):
                    sim.errors.append(repr(error))
                response = {"jsonrpc":"2.0","id":request["id"],"error":{"code":-32000,"message":str(error)}}
            self.respond(json.dumps(response).encode())
        def do_GET(self):
            sim.downloads.append(self.path)
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                requested = self.headers["Range"]
                start, _, end = requested.removeprefix("bytes=").partition("-") if requested else ("0", "-", "")
                offset = int(start)
                end = min(int(end) if end else len(ARTIFACT) - 1, len(ARTIFACT) - 1)
                sim.ranges.append(offset)
                if offset >= len(ARTIFACT):
                    self.respond(b"range outside blob", 416)
                    return
                body = ARTIFACT[offset:end + 1]
                self.send_response(206 if requested else 200)
                if requested:
                    self.send_header("Content-Range", f"bytes {offset}-{end}/{len(ARTIFACT)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if sim.truncate_next_artifact:
                    # Promise this range, send a prefix, drop the connection.
                    sim.truncate_next_artifact = False
                    self.wfile.write(body[:10])
                    self.close_connection = True
                else:
                    self.wfile.write(body)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-ota-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            assert subprocess.check_output([str(binary), "--derive-address", str(key)], text=True).strip() == ADDRESS
            provisioned = root / "provisioned.key"
            first = subprocess.check_output([str(binary), "--provision", str(provisioned)], text=True)
            original = provisioned.read_bytes()
            assert subprocess.check_output([str(binary), "--provision", str(provisioned)], text=True) == first
            assert provisioned.read_bytes() == original and provisioned.stat().st_mode & 0o777 == 0o600
            applied = root / "applied.jsonl"
            updater = root / "apply.py"
            updater.write_text("import json, pathlib, sys\nwith open(" + repr(str(applied)) + ", 'a') as output:\n    output.write(json.dumps(pathlib.Path(sys.argv[1]).read_bytes().decode()) + '\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            command = f"{sys.executable} {updater}"
            if platform_installer:
                installer = Path(__file__).resolve().parents[1] / "tests/components/platform-installer/install.sh"
                command = shlex.join(["/bin/sh", str(installer), str(root / "installed-artifact"), str(updater)])
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
                shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min":"0.0.1", "max_exclusive":"0.1.0"}, "edge": {"min":"0.1.0", "max_exclusive":"0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            config = root / "config.toml"
            spool = root / "state" / "firmware"
            mode = f'mode = "privileged-helper"\nfirmware_spool = {json.dumps(str(spool))}\n' if firmware_helper else ""
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\nrange_chunk_bytes = 16\n[apply]\ncommand = {json.dumps("/bin/false" if firmware_helper else command)}\n'
                f'timeout_seconds = 2\n{mode}'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\nevent_discovery_seconds = 1\n')
            if poll_reads:
                config.write_text(config.read_text().replace("event_discovery_seconds = 1", "event_discovery_seconds = 60"))
            helper_config = root / "firmware.toml"
            # Only the root-owned helper config names the real updater.
            helper_config.write_text(f'command = {json.dumps(command)}\ntimeout_seconds = 2\n'
                f'spool_dir = {json.dumps(str(spool))}\nwork_dir = {json.dumps(str(root / "helper-work"))}\n')
            account = None
            if daemon_user:
                import pwd
                account = pwd.getpwnam(daemon_user)
                for path in [root, *root.rglob("*")]:
                    os.chown(path, account.pw_uid, account.pw_gid)
                helper_config.chmod(0o600)
                os.chown(helper_config, 0, 0)
            helper_stop = threading.Event()
            def helper_loop():
                while not helper_stop.is_set():
                    subprocess.run([str(binary), "--firmware-helper", str(helper_config)], stdout=log, stderr=log, check=False)
                    helper_stop.wait(0.2)
            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}
            def count():
                return len(applied.read_text().splitlines()) if applied.exists() else 0
            def start():
                identity = dict(user=account.pw_uid, group=account.pw_gid, extra_groups=[]) if account else {}
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG":str(config)}, stdout=log, stderr=log, **identity)
            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
                assert process.returncode == 0
            # Enrollment must not execute historical updates by accident.
            sim.add("0x100")
            with (root / "daemon.log").open("w") as log:
                if firmware_helper:
                    threading.Thread(target=helper_loop, daemon=True).start()
                process = start()
                wait_for(lambda: state().get("initialized"), "enrollment did not establish cursor", process)
                assert count() == 0
                # Enrollment stores a checkpoint; the first scan rewrites the same
                # position as a resume cursor. Compare against the latter.
                wait_for(lambda: "after" in state().get("cursor", {}), "first event scan did not complete", process)
                baseline = state()["cursor"]
                if baseline_profile:
                    checks = sim.chain_checks
                    wait_for(lambda: sim.chain_checks >= checks + 2, "baseline did not poll", process)
                    before, sizes = dict(sim.reads), dict(sim.read_bytes)
                    checks = sim.chain_checks
                    wait_for(lambda: sim.chain_checks >= checks + 3, "baseline profile incomplete", process)
                    time.sleep(0.5)
                    delta = {kind: count - before.get(kind, 0) for kind, count in sim.reads.items()}
                    byte_delta = {kind: count - sizes.get(kind, 0) for kind, count in sim.read_bytes.items()}
                    import re
                    raw = re.sub(r"\x1b\[[0-9;]*m", "", (root / "daemon.log").read_text())
                    metrics = re.findall(r"read_requests=(\d+) response_bytes=(\d+)", raw)
                    print(f"BASELINE: simulator steady polls (requests, protobuf response bytes): {metrics[-3:]}")
                    print(f"BASELINE: simulator reads across approximately 3 polls: {delta}; protobuf response bytes: {byte_delta}")
                    stop(process)
                    return
                if poll_reads:
                    wait_for(lambda: state().get("discovery_cursor") is not None and GROUP in state().get("groups", []), "cursor merge/group discovery incomplete", process)
                    wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= 2, "version watch did not become idle", process)
                    before, sizes = dict(sim.reads), dict(sim.read_bytes)
                    wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= before.get("BatchGetObjects", 0) + 3, "three idle polls missing", process)
                    delta = {kind: count - before.get(kind, 0) for kind, count in sim.reads.items()}
                    assert delta["BatchGetObjects"] == 3 and sum(delta.values()) == 3, delta
                    assert "Sync failed" not in (root / "daemon.log").read_text(), (root / "daemon.log").read_text()[-2000:]
                    payload = sim.read_bytes["BatchGetObjects"] - sizes["BatchGetObjects"]
                    print(f"PASS: steady polls: {delta}, {payload} protobuf response bytes across 3 polls")
                    # Every authority change wakes full, uncached validation on
                    # the next poll, rather than waiting for event discovery.
                    for label, diagnostic, mutate, restore in [
                        ("revocation", "revoked", lambda: sim.identity.update(revoked=True), lambda: sim.identity.update(revoked=False)),
                        ("rotation", "rotated away", lambda: sim.identity.update(signer="0xbad"), lambda: sim.identity.update(signer=ADDRESS)),
                        ("upgrade", "UpgradeCap does not identify", lambda: setattr(sim, "latest_package", object_id("0x4")), lambda: setattr(sim, "latest_package", PACKAGE)),
                        ("reset", "chain identifier mismatch", lambda: setattr(sim, "chain", "deadbeef"), lambda: setattr(sim, "chain", manifest["chain_identifier"])),
                        ("NotFound", "deleted or absent", lambda: sim.deleted.add(CAP), lambda: sim.deleted.remove(CAP)),
                    ]:
                        checks = sim.chain_checks
                        errors_before = (root / "daemon.log").read_text().count(diagnostic)
                        mutate()
                        wait_for(lambda: sim.chain_checks > checks, f"{label} did not wake full validation", process, timeout=4)
                        wait_for(lambda: (root / "daemon.log").read_text().count(diagnostic) > errors_before, f"{label} did not fail closed", process, timeout=4)
                        assert not sim.downloads and count() == 0
                        checks = sim.chain_checks
                        restore()
                        wait_for(lambda: sim.chain_checks > checks, f"{label} did not recover", process, timeout=4)
                        batches = sim.reads["BatchGetObjects"]
                        wait_for(lambda: sim.reads["BatchGetObjects"] >= batches + 2, f"{label} did not return to batch polling", process, timeout=5)
                        print(f"PASS: {label} revalidated within one poll; no apply or download")
                    # Failed schema resolution leaves a historical notice;
                    # an already reported outcome must not keep admission due.
                    sim.schema_fields["0x200"] = {"schema_version": "99"}
                    sim.allow_reports = True
                    sim.fail_execution = False
                    sim.add("0x200")
                    sim.groups[GROUP]["device_ids"].append("0xbad")  # Wake discovery.
                    wait_for(lambda: state().get("deployments", {}).get("0x200", {}).get("reported"),
                             "rejected recipe did not report", process)
                    assert state()["deployments"]["0x200"]["status"] == 2 and count() == 0
                    batches = sim.reads["BatchGetObjects"]
                    wait_for(lambda: sim.reads["BatchGetObjects"] >= batches + 2,
                             "rejection did not settle", process)
                    before = dict(sim.reads)
                    wait_for(lambda: sim.reads["BatchGetObjects"] >= before["BatchGetObjects"] + 3,
                             "idle polls after rejection missing", process)
                    delta = {kind: n - before.get(kind, 0) for kind,n in sim.reads.items()}
                    assert delta["BatchGetObjects"] == 3 and sum(delta.values()) == 3, delta
                    print(f"PASS: reported rejection history returns to one batch per idle poll: {delta}")
                    # A permanently undeliverable receipt must not turn idle
                    # polls into authority/funding/report read amplification.
                    sim.allow_reports = False
                    sim.schema_fields["0x201"] = {"schema_version": "99"}
                    sim.add("0x201")
                    sim.groups[GROUP]["device_ids"].append("0xbeef")
                    wait_for(lambda: state().get("deployments", {}).get("0x201", {}).get("status") == 2,
                             "undeliverable rejection did not queue", process)
                    wait_for(lambda: "Status report remains queued" in (root / "daemon.log").read_text(),
                             "queued receipt was not attempted", process)
                    batches = sim.reads["BatchGetObjects"]
                    wait_for(lambda: sim.reads["BatchGetObjects"] >= batches + 1, "queue did not settle", process)
                    before = dict(sim.reads)
                    wait_for(lambda: sim.reads["BatchGetObjects"] >= before["BatchGetObjects"] + 3,
                             "idle queued-receipt polls missing", process)
                    delta = {kind: n - before.get(kind, 0) for kind,n in sim.reads.items()}
                    assert delta["BatchGetObjects"] == 3 and sum(delta.values()) == 3, delta
                    assert not state()["deployments"]["0x201"]["reported"]
                    print(f"PASS: undeliverable queued receipt backs off; three idle polls use only three batches: {delta}")
                    sim.allow_reports = True
                    wait_for(lambda: state()["deployments"]["0x201"]["reported"], "receipt retry did not recover", process)
                    stop(process)
                    assert not sim.errors, sim.errors
                    return
                # A chain reset after enrollment must stop all downloads and cursor changes.
                sim.chain = "deadbeef"
                # A poll that passed its chain check before the reset may still be
                # reading events; add the next one only after a post-reset check.
                checks = sim.chain_checks
                wait_for(lambda: sim.chain_checks > checks, "daemon did not recheck the chain", process)
                sim.add("0x101")
                time.sleep(2.2)
                assert count() == 0 and not sim.downloads
                assert state()["cursor"] == baseline
                # Stale the package before restoring the chain, so no poll sees a
                # valid chain with the current package in between.
                sim.latest_package = object_id("0x4")
                sim.chain = manifest["chain_identifier"]
                time.sleep(2.2)
                assert count() == 0 and not sim.downloads
                assert state()["cursor"] == baseline
                sim.latest_package = PACKAGE
                sim.add("0x102", bad_hash=True)
                sim.add("0x103", creator="0xbad")
                # Unsupported records must fail individually, with no downloads,
                # and must not poison discovery of the valid group deployment.
                for id, version in (("0x10b", "5"), ("0x10c", None), ("0x10d", "invalid")):
                    sim.schema_fields[id] = {"schema_version": version}
                    sim.add(id)
                sim.add("0x104", groups=[GROUP], direct=False)
                sim.add("0x105", run=True)
                sim.add("0x109") # Tampered recipe must never reach the updater.
                sim.add("0x10a", expired=True) # Artifact storage period ended (404).
                sim.deleted.add("0x106")
                sim.add("0x106")
                sim.deleted.add("0x999")
                sim.add("0x107", groups=["0x999"], direct=False)
                wait_for(lambda: len(state().get("deployments", {})) == 9 and state().get("cursor") == scanned(len(sim.events)), "paged events were not processed", process)
                assert count() == 2, "untrusted/failed/unsupported/deleted update invoked the updater"
                outcomes = state()["deployments"]
                assert {id: outcome["status"] for id, outcome in outcomes.items()} == {"0x101":1,"0x102":2,"0x104":1,"0x105":2,"0x109":2,"0x10a":2,"0x10b":2,"0x10c":2,"0x10d":2}
                assert "unavailable or expired" in outcomes["0x10a"]["error"], outcomes["0x10a"]
                for id in ("0x10b", "0x10c", "0x10d"):
                    assert "unsupported DeploymentRecord schema version" in outcomes[id]["error"], outcomes[id]
                    assert outcomes[id].get("firmware_request") is None
                    assert f"/v1/blobs/recipe-{id}" not in sim.downloads
                    assert not (root / "state" / "artifacts" / hashlib.sha256(id.encode()).hexdigest()).exists()
                assert sim.ranges[:2] == [0, 10], f"interrupted transfer did not resume: {sim.ranges}"
                assert not any(o["reported"] for o in outcomes.values())
                stop(process)
                process = None
                # A crash after journaling outcomes but before the cursor is saved
                # redelivers processed events; the journal alone must prevent reapply.
                journal = state()
                # Rewind with the JSON-RPC-era cursor form: migration must resume
                # exactly after event 0 (redelivering the rest, deduplicated).
                journal.pop("discovery_cursor", None)
                journal.pop("discovery_merge_checkpoint", None)
                journal["cursor"] = {"txDigest": "event-0", "eventSeq": "0"}
                (root / "state" / "state.json").write_text(json.dumps(journal))
                sim.add("0x108") # Deployment created while the device is offline.
                sim.allow_reports = True
                process = start()
                wait_for(lambda: len(state().get("deployments", {})) == 10 and all(o["reported"] for o in state()["deployments"].values()), "restart failed to catch up and retry reports", process)
                assert count() == 3, "restart duplicated an already-applied deployment"
                assert len(sim.reports) == 10
                assert not list((root / "state" / "artifacts").iterdir()), "artifact cache not cleaned"
                assert all(json.loads(line) == ARTIFACT.decode() for line in applied.read_text().splitlines())
                assert "/v1/blobs/recipe-0x101" in sim.downloads and "/v1/blobs/artifact" in sim.downloads
                assert not sim.errors, sim.errors
                # A revoked device stops: nothing new is applied or reported.
                # Service-info calls also occur during event discovery. Wait
                # for an actual failed identity validation, so a poll already
                # in flight before revocation cannot see the new event.
                diagnostic = "device identity is revoked"
                failures = (root / "daemon.log").read_text().count(diagnostic)
                sim.identity = dict(sim.identity, revoked=True)
                wait_for(lambda: (root / "daemon.log").read_text().count(diagnostic) > failures,
                         "daemon did not reject the revoked identity", process)
                sim.add("0x10e")
                polls = completed_polls(root / "daemon.log")
                wait_for(lambda: completed_polls(root / "daemon.log") >= polls + 4,
                         "daemon stopped polling after revocation", process)
                assert count() == 3 and "0x10e" not in state()["deployments"], "revoked device applied an update"
                assert len(sim.reports) == 10
                stop(process)
                process = None
                helper_stop.set()
                if firmware_helper:
                    assert not list(spool.glob("requests/*")), "firmware requests left behind"
            if firmware_helper:
                print("PASS: privileged firmware helper" + (f" (daemon as {daemon_user}, helper as root)" if daemon_user else ""))
            print("PASS: provisioning and independent signing/address vector")
            print("PASS: mixed per-type origins and runtime chain-reset/stale-call-package rejection")
            print("PASS: safe enrollment; paginated direct/group deployment and recipe-to-artifact flow")
            if platform_installer:
                assert (root / "installed-artifact").read_bytes() == ARTIFACT
                print("PASS: explicitly configured Linux installer script received verified bytes; command completion only")
            print("PASS: artifact/recipe hash mismatch, unsupported lifecycle, untrusted creator and deleted targets rejected")
            print("PASS: future/missing/malformed deployment schemas failed without downloads; later v1 applied and cursor advanced")
            print("PASS: offline catch-up, restart deduplication, failed execution and durable report retry")
            print("PASS: revoked registry identity stops applying and reporting")
            print("PASS: interrupted artifact transfer resumed; expired blob reported Failed; artifact cache emptied")
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
    parser.add_argument("--baseline-profile", action="store_true", help="measure an instrumented pre-cache daemon")
    parser.add_argument("--poll-reads", action="store_true", help="assert one batch per unchanged poll and authority invalidation")
    parser.add_argument("--firmware-helper", action="store_true", help="apply through the privileged firmware helper")
    parser.add_argument("--platform-installer", action="store_true", help="exercise the explicit Linux installer script fixture")
    parser.add_argument("--daemon-user", help="run the daemon as this user; requires root (helper stays root)")
    args = parser.parse_args()
    if args.daemon_user and os.geteuid() != 0:
        parser.error("--daemon-user requires root")
    run(args.binary.resolve(), args.firmware_helper or bool(args.daemon_user), args.daemon_user, args.platform_installer, args.poll_reads, args.baseline_profile)
