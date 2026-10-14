#!/usr/bin/env python3
"""Exercise the real daemon against a local chain/Walrus simulator, without keys or gas.
This verifies integration behavior, not public-network compatibility or hardware OTA.
"""
from deployment_manifest import REQUIRED_TYPES, object_id
import argparse
import base64
import hashlib
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

PACKAGE = object_id("0x3")
ORIGIN = object_id("0x1")
RECIPE_ORIGIN = object_id("0x2")
REGISTRY = "0xbe"
UPGRADE_CAP = "0xc0"
TYPE_ORIGINS = {name: ORIGIN for name in REQUIRED_TYPES}
TYPE_ORIGINS["deployment_manager::RecipeDigestKey"] = RECIPE_ORIGIN
TYPE_ORIGINS["device_cap::DeviceAddressKey"] = PACKAGE
DEVICE = "0xde"
CAP = "0xca"
OPERATOR = "0xad"
GROUP = "0x10"
ADDRESS = "0xa0ccc8bcc83f6c628340134f8546a21e0618fd1aaa02432bba454c4a2c2233da"
SIGNATURE = "AGe7jkxf/S/LN1HBGv3Lbbhb+4FzdWYtt+XlzfsikT+N6RHAAxUZBeipd/iHLQofYPVZqPaSLt4WQTkmA1YnEgXqSmxj4pxSCr71UHsTLsX5lUd2rr6+e5JCHuppFEbSLA=="
ARTIFACT = b"test OTA artifact, not a recipe"
HASH = hashlib.sha256(ARTIFACT).hexdigest()


def wait_for(condition, message, process=None):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.05)
    raise AssertionError(message)


class Simulator:
    def __init__(self):
        self.latest_package = PACKAGE
        self.chain = "1234abcd"
        self.events = []
        self.recipes = {}
        self.reports = []
        self.allow_reports = False
        self.fail_execution = True
        self.deleted = set()
        self.downloads = []
        self.built = []
        self.errors = []
        self.truncate_next_artifact = True  # First artifact transfer drops mid-body.
        self.ranges = []

    def add(self, id, *, creator=OPERATOR, groups=None, direct=True, bad_hash=False, run=False, expired=False):
        digest = "f" * 64 if bad_hash else HASH
        event = dict(deployment_id=id, component_name=f"app-{id}", version="1.0.0",
            recipe_blob_id=f"recipe-{id}", artifact_sha256=digest,
            target_groups=groups or [], target_devices=[DEVICE] if direct else [], creator=creator)
        recipe = dict(schemaVersion="1.0", componentName=event["component_name"], componentVersion="1.0.0",
            artifactBlobId="expired" if expired else "artifact", artifactSha256=digest, lifecycle={"run": "sleep 10"} if run else {}, configuration={})
        self.recipes[event["recipe_blob_id"]] = recipe
        self.events.append(dict(id={"txDigest":f"event-{len(self.events)}", "eventSeq":"0"}, parsedJson=event))

    def obj(self, id):
        if id in self.deleted:
            return {"error":{"code":"deleted","object_id":id}}
        if id == UPGRADE_CAP:
            return {"data":{"content":{"type":"0x2::package::UpgradeCap","fields":{"package":self.latest_package,"version":"3"}}}}
        if id == REGISTRY:
            type, fields, owner = "device_registry::DeviceRegistry", {}, None
        elif id == DEVICE:
            type, fields, owner = "device_registry::Device", {"device_address":ADDRESS}, OPERATOR
        elif id == CAP:
            type, fields, owner = "device_cap::DeviceCap", {"device_id":DEVICE}, ADDRESS
        elif id == GROUP:
            type, fields, owner = "device_group::DeviceGroup", {"admin":OPERATOR,"device_ids":[DEVICE]}, None
        else:
            event = next(e["parsedJson"] for e in self.events if e["parsedJson"]["deployment_id"] == id)
            type, fields, owner = "deployment_manager::DeploymentRecord", {"creator":event["creator"]}, None
        return {"data":{"objectId":id,"owner":{"AddressOwner":owner} if owner else {"Shared":{"initial_shared_version":"1"}},
            "content":{"type":f"{TYPE_ORIGINS[type]}::{type}","fields":fields}}}

    def rpc(self, method, params):
        if method == "sui_getChainIdentifier":
            return self.chain
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
            if params[0] == CAP:
                assert params[1]["type"] == f"{PACKAGE}::device_cap::DeviceAddressKey"
                return {"data":{"content":{"fields":{"value":ADDRESS}}}}
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
            assert args[1] == CAP
            assert (params[3] == "report_status" and len(args) == 3) or (params[3] == "report_status_for_group" and args[2] == GROUP and len(args) == 4)
            if not self.allow_reports:
                raise ConnectionError("simulated unavailable transaction service")
            self.built.append((args[0], int(args[-1])))
            return {"txBytes":base64.b64encode(bytes([1,2,3,4])).decode()}
        if method == "sui_executeTransactionBlock":
            assert params[1] == [SIGNATURE], "signature differs from independent Mysten SDK vector"
            if self.fail_execution:
                self.fail_execution = False
                return {"digest":"failed-digest", "effects":{"status":{"status":"failure","error":"simulated abort"}}}
            self.reports.append(self.built[-1])
            return {"digest":"success-digest", "effects":{"status":{"status":"success"}}}
        raise AssertionError(f"unexpected RPC method {method}")


def run(binary):
    sim = Simulator()
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
                offset = int(self.headers["Range"].removeprefix("bytes=").rstrip("-")) if self.headers["Range"] else 0
                sim.ranges.append(offset)
                if sim.truncate_next_artifact:
                    # Promise the whole artifact, send a prefix, drop the connection.
                    sim.truncate_next_artifact = False
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(ARTIFACT)))
                    self.end_headers()
                    self.wfile.write(ARTIFACT[:10])
                    self.close_connection = True
                elif offset:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {offset}-{len(ARTIFACT)-1}/{len(ARTIFACT)}")
                    self.send_header("Content-Length", str(len(ARTIFACT) - offset))
                    self.end_headers()
                    self.wfile.write(ARTIFACT[offset:])
                else:
                    self.respond(ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
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
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
                shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min":"0.0.1", "max_exclusive":"0.1.0"}, "edge": {"min":"0.1.0", "max_exclusive":"0.2.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = {json.dumps(command)}\ntimeout_seconds = 2\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n')
            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}
            def count():
                return len(applied.read_text().splitlines()) if applied.exists() else 0
            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG":str(config)}, stdout=log, stderr=log)
            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
                assert process.returncode == 0
            # Enrollment must not execute historical updates by accident.
            sim.add("0x100")
            with (root / "daemon.log").open("w") as log:
                process = start()
                wait_for(lambda: state().get("initialized"), "enrollment did not establish cursor", process)
                assert count() == 0
                # A chain reset after enrollment must stop all downloads and cursor changes.
                sim.chain = "deadbeef"
                sim.add("0x101")
                time.sleep(2.2)
                assert count() == 0 and not sim.downloads
                assert state()["cursor"] == sim.events[0]["id"]
                sim.chain = manifest["chain_identifier"]
                sim.latest_package = object_id("0x4")
                time.sleep(2.2)
                assert count() == 0 and not sim.downloads
                assert state()["cursor"] == sim.events[0]["id"]
                sim.latest_package = PACKAGE
                sim.add("0x102", bad_hash=True)
                sim.add("0x103", creator="0xbad")
                sim.add("0x104", groups=[GROUP], direct=False)
                sim.add("0x105", run=True)
                sim.add("0x109") # Tampered recipe must never reach the updater.
                sim.add("0x10a", expired=True) # Artifact storage period ended (404).
                sim.deleted.add("0x106")
                sim.add("0x106")
                sim.deleted.add("0x999")
                sim.add("0x107", groups=["0x999"], direct=False)
                wait_for(lambda: len(state().get("deployments", {})) == 6 and state().get("cursor") == sim.events[-1]["id"], "paged events were not processed", process)
                assert count() == 2, "untrusted/failed/unsupported/deleted update invoked the updater"
                outcomes = state()["deployments"]
                assert {id: outcome["status"] for id, outcome in outcomes.items()} == {"0x101":1,"0x102":2,"0x104":1,"0x105":2,"0x109":2,"0x10a":2}
                assert "unavailable or expired" in outcomes["0x10a"]["error"], outcomes["0x10a"]
                assert sim.ranges[:2] == [0, 10], f"interrupted transfer did not resume: {sim.ranges}"
                assert not any(o["reported"] for o in outcomes.values())
                stop(process)
                process = None
                # A crash after journaling outcomes but before the cursor is saved
                # redelivers processed events; the journal alone must prevent reapply.
                journal = state()
                journal["cursor"] = sim.events[0]["id"]
                (root / "state" / "state.json").write_text(json.dumps(journal))
                sim.add("0x108") # Deployment created while the device is offline.
                sim.allow_reports = True
                process = start()
                wait_for(lambda: len(state().get("deployments", {})) == 7 and all(o["reported"] for o in state()["deployments"].values()), "restart failed to catch up and retry reports", process)
                assert count() == 3, "restart duplicated an already-applied deployment"
                assert len(sim.reports) == 7
                assert not list((root / "state" / "artifacts").iterdir()), "artifact cache not cleaned"
                assert all(json.loads(line) == ARTIFACT.decode() for line in applied.read_text().splitlines())
                assert "/v1/blobs/recipe-0x101" in sim.downloads and "/v1/blobs/artifact" in sim.downloads
                assert not sim.errors, sim.errors
                stop(process)
                process = None
            print("PASS: provisioning and independent signing/address vector")
            print("PASS: mixed per-type origins and runtime chain-reset/stale-call-package rejection")
            print("PASS: safe enrollment; paginated direct/group deployment and recipe-to-artifact flow")
            print("PASS: artifact/recipe hash mismatch, unsupported lifecycle, untrusted creator and deleted targets rejected")
            print("PASS: offline catch-up, restart deduplication, failed execution and durable report retry")
            print("PASS: interrupted artifact transfer resumed; expired blob reported Failed; artifact cache emptied")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
