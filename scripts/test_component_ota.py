#!/usr/bin/env python3
"""Run the real daemon with the real SF-030 test updater against the local
chain/Walrus simulator: v1, v2, an injected install failure and an injected
health failure, each with exact generated artifact and recipe bytes.

This proves the one-shot apply path end to end on this machine. It does not
replace the Raspberry Pi acceptance run (SF-031).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_component as component
from test_ota import (CAP, DEVICE, OPERATOR, PACKAGE, RECIPE_ORIGIN, REGISTRY, TYPE_ORIGINS,
                      UPGRADE_CAP, Simulator, start_grpc, wait_for)


class ComponentSimulator(Simulator):
    """Serves generated recipe/artifact bytes and their on-chain digests."""

    def __init__(self):
        super().__init__()
        self.allow_reports = True
        self.fail_execution = False
        self.blobs = {}

    def deploy(self, id, artifact):
        artifact_blob, recipe_blob = f"artifact-{id}", f"recipe-{id}"
        recipe = component.recipe(artifact, artifact_blob)
        self.blobs[artifact_blob], self.blobs[recipe_blob] = artifact, recipe
        fields = component.header(artifact)
        event = dict(deployment_id=id, component_name=fields["component"], version=fields["version"],
                     recipe_blob_id=recipe_blob, artifact_sha256=hashlib.sha256(artifact).hexdigest(),
                     target_groups=[], target_devices=[DEVICE], creator=OPERATOR)
        self.events.append(dict(id={"txDigest": f"event-{len(self.events)}", "eventSeq": "0"}, parsedJson=event))

    def rpc(self, method, params):
        if method == "suix_getDynamicFieldObject" and params[1]["type"].endswith("RecipeDigestKey"):
            assert params[1]["type"] == f"{RECIPE_ORIGIN}::deployment_manager::RecipeDigestKey"
            event = next(e["parsedJson"] for e in self.events if e["parsedJson"]["deployment_id"] == params[0])
            return {"data": {"content": {"fields": {"value": hashlib.sha256(self.blobs[event["recipe_blob_id"]]).hexdigest()}}}}
        return super().rpc(method, params)


def run(binary):
    sim = ComponentSimulator()

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
            blob = sim.blobs.get(self.path.removeprefix("/v1/blobs/"))
            self.respond(blob, 200) if blob is not None else self.respond(b"not found", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-component-ota-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
                shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.2.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            components = root / "state" / "test-components"
            command = f"/bin/sh {component.UPDATER} --root {components}"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = {json.dumps(command)}\ntimeout_seconds = 10\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n')

            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            run_id = "sim-" + uuid.uuid4().hex[:12]
            plan = {  # deployment -> (version, injected failure, expected on-chain status)
                "0x201": ("1.0.0", "none", 1),
                "0x202": ("2.0.0", "none", 1),
                "0x203": ("3.0.0", "install", 2),
                "0x204": ("4.0.0", "health", 2),
            }
            artifacts = {id: component.build(version, run_id, fail) for id, (version, fail, _) in plan.items()}
            sim.deploy("0x200", component.build("0.9.0", run_id))  # Before enrollment: must never run.
            with (root / "daemon.log").open("w") as log:
                process = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)
                wait_for(lambda: state().get("initialized"), "enrollment did not establish cursor", process)
                for id, artifact in artifacts.items():
                    sim.deploy(id, artifact)
                wait_for(lambda: len(state().get("deployments", {})) == len(plan)
                         and all(o["reported"] for o in state()["deployments"].values()),
                         "test component deployments were not applied and reported", process)
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
                assert process.returncode == 0
                process = None
            outcomes = state()["deployments"]
            assert {id: o["status"] for id, o in outcomes.items()} == {id: p[2] for id, p in plan.items()}, outcomes
            assert sorted(sim.reports) == sorted((id, p[2]) for id, p in plan.items()), sim.reports
            base = components / component.DEFAULT_COMPONENT
            history = [json.loads(line) for line in (base / "history.jsonl").read_text().splitlines()]
            expected = [(p[0], run_id, hashlib.sha256(artifacts[id]).hexdigest()) for id, p in plan.items()]
            assert [(r["version"], r["runId"], r["artifactSha256"]) for r in history] == expected, history
            assert [(r["install"], r["health"]) for r in history] == [
                ("succeeded", "passed"), ("succeeded", "passed"), ("failed", "not-run"), ("succeeded", "failed")]
            assert all(r["run"]["arch"] == os.uname().machine for r in history if r["run"])
            assert (base / "current").read_text().strip() == "4.0.0"
            assert not (base / "versions" / "0.9.0").exists() and not (base / "versions" / "3.0.0").exists()
            assert not sim.errors, sim.errors
            print("PASS: real daemon applied test component v1/v2 via suifleet-test-updater; receipts match version, run ID and SHA-256")
            print("PASS: injected install failure (previous version kept) and health failure reported Failed with distinct receipts")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        server.shutdown()
        shim.kill()
        shim.wait()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
