#!/usr/bin/env python3
"""AWS Greengrass recipe translation (SF-037) executed on the real daemon
against the local chain/Walrus simulator, without keys or gas.

An AWS YAML recipe (install, run and shutdown with {artifacts:path} and
{work:path}, DefaultConfiguration) is translated by the real translator CLI
(scripts/greengrass_translate.cjs) and deployed unchanged:
  0x601 sensor 1.0.0 translated      -> Applied; run supervised in the work
                                        directory with {work:path} = that
                                        directory, the artifact at
                                        {artifacts:path}/<file>, the AWS
                                        default configuration as its
                                        configuration file; no nucleus
                                        environment leaks in; install ran in
                                        the version's artifact directory
                                        (the reported install difference)
  0x602 sensor 1.0.0 + merge         -> the operator's per-deployment merge
                                        (the AWS MERGE update); shutdown ran
                                        before the restart
  0x603 sensor 1.1.0 translated      -> device value kept, new default keys
                                        added (native SF-015 rule)
The translator rejects an artifact-only AWS manifest (no install or run). The
same native recipe deployed directly shows why: the daemon hands it to the
platform updater:
  0x604 native recipe, no commands   -> the configured apply command ran
"""
import argparse
import base64
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
URI = "s3://example-bucket/sensor/1.0.0/sensor.sh"
BLOB = "gg-sensor"
RECIPE = """---
RecipeFormatVersion: '2020-01-25'
ComponentName: com.example.Sensor
ComponentVersion: '{version}'
ComponentDescription: Translated sensor
ComponentPublisher: Example
ComponentConfiguration:
  DefaultConfiguration:
{defaults}
Manifests:
  - Platform:
      os: windows
    Lifecycle:
      run: sensor.exe
    Artifacts:
      - Uri: s3://example-bucket/sensor/1.0.0/sensor.exe
  - Platform:
      os: linux
    Lifecycle:
      install: sh {{artifacts:path}}/sensor.sh install
      run:
        Script: exec sh {{artifacts:path}}/sensor.sh run {{work:path}}
        RequiresPrivilege: false
      shutdown: sh {{artifacts:path}}/sensor.sh shutdown {{work:path}}
    Artifacts:
      - Uri: {uri}
        Algorithm: SHA-256
        Digest: {digest}
"""
DEFAULTS_V1 = "    interval: 1\n    labels:\n      site: lab\n"
DEFAULTS_V2 = "    interval: 10\n    labels:\n      site: lab\n    mode: fast\n"
# Stands in for an AWS component; records where and with what it ran.
SENSOR = r"""#!/bin/sh
probe=PROBE
case "$1" in
install) pwd > "$probe/install-cwd" ;;
run)
  printf '{"cwd": "%s", "work": "%s", "self": "%s", "thing": "%s", "svcuid": "%s"}\n' \
    "$(pwd)" "$2" "$0" "${AWS_IOT_THING_NAME-unset}" "${SVCUID-unset}" > "$probe/run-$SUIFLEET_COMPONENT_VERSION.json"
  cp "$SUIFLEET_CONFIGURATION" "$probe/config.json"
  while [ ! -e "$2/stop" ]; do sleep 0.1; done
  rm -f "$2/stop"; echo "stopped $SUIFLEET_COMPONENT_VERSION" >> "$probe/events" ;;
shutdown) echo "shutdown $SUIFLEET_COMPONENT_VERSION" >> "$probe/events"; touch "$2/stop" ;;
esac
"""


def translate(root: Path, version: str, defaults: str, sha: str) -> dict:
    digest = base64.b64encode(bytes.fromhex(sha)).decode()
    path = root / f"recipe-{version}.yaml"
    path.write_text(RECIPE.format(version=version, defaults=defaults.rstrip("\n"), uri=URI, digest=digest))
    done = subprocess.run(["node", str(ROOT / "scripts/greengrass_translate.cjs"), str(path), "--bind", f"{URI}={BLOB}:{sha}"],
                          capture_output=True, text=True)
    result = json.loads(done.stdout)
    assert done.returncode == 0 and result["outcome"] == "translate", (done.returncode, done.stdout, done.stderr)
    assert result["omitted_manifests"] == [0], result
    assert result["informational"] == ["ComponentPublisher", "ComponentConfiguration.DefaultConfiguration",
                                       "Lifecycle.install", "Lifecycle.run", "Lifecycle.shutdown"], result
    assert all(result["guidance"][code] for code in result["informational"]), result
    [manifest] = result["manifests"]
    assert (manifest["os"], manifest["arch"]) == ("linux", "*"), manifest
    return manifest["recipe"]


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    artifact = {}

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
            if blob == BLOB:
                self.respond(artifact["bytes"])
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def deploy(id, recipe):
        sim.add(id, event=dict(component_name=recipe["componentName"], version=recipe["componentVersion"],
                               artifact_sha256=recipe["artifactSha256"]), recipe=recipe)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-greengrass-") as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            artifact["bytes"] = SENSOR.replace("PROBE", str(probe)).encode()
            sha = hashlib.sha256(artifact["bytes"]).hexdigest()
            v1 = translate(root, "1.0.0", DEFAULTS_V1, sha)
            v2 = translate(root, "1.1.0", DEFAULTS_V2, sha)
            assert v1["schemaVersion"] == "1.2" and v1["configuration"] == {"defaults": {"interval": 1, "labels": {"site": "lab"}}}, v1
            assert v1["lifecycle"] == {"install": "sh {artifact} install", "run": "exec sh {artifact} run {work}",
                                       "shutdown": "sh {artifact} shutdown {work}"}, v1

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
            updater = root / "updater.sh"
            updater.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {probe}/updater-args\n')
            updater.chmod(0o700)
            state_dir = root / "state"
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                f'[apply]\ncommand = {json.dumps(str(updater))}\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\n'
                'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 3\n')
            name = "com.example.Sensor"
            work = state_dir / "supervised" / name / "work"

            def journal():
                path = state_dir / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcome(id):
                return journal().get("deployments", {}).get(id, {})

            def running(version):
                try:
                    current = json.loads((state_dir / "supervised" / "status.json").read_text()).get(name, {})
                except (OSError, ValueError):
                    return False
                return current.get("phase") == "running" and current.get("version") == version

            def record(version):
                path = probe / f"run-{version}.json"
                return json.loads(path.read_text()) if path.exists() else None

            def configuration():
                return json.loads((probe / "config.json").read_text())

            def events():
                path = probe / "events"
                return path.read_text().split("\n")[:-1] if path.exists() else []

            with (root / "daemon.log").open("w") as log:
                # Nucleus-style variables in the daemon's environment must not reach components.
                environment = {**os.environ, "EDGE_CONFIG": str(config), "AWS_IOT_THING_NAME": "thing", "SVCUID": "secret"}
                process = subprocess.Popen([str(binary)], env=environment, stdout=log, stderr=log)
                wait_for(lambda: journal().get("initialized"), "daemon did not establish its cursor", process)

                deploy("0x601", v1)
                wait_for(lambda: outcome("0x601").get("status") == 1, f"0x601 was not applied: {outcome('0x601')}", process)
                wait_for(lambda: running("1.0.0") and record("1.0.0"), "0x601 did not run", process)
                seen = record("1.0.0")
                assert seen["cwd"] == str(work) and seen["work"] == str(work), seen
                assert Path(seen["self"]).read_bytes() == artifact["bytes"], seen
                assert (seen["thing"], seen["svcuid"]) == ("unset", "unset"), seen
                assert configuration() == {"interval": 1, "labels": {"site": "lab"}}, configuration()
                install_cwd = Path((probe / "install-cwd").read_text().strip())
                assert install_cwd == Path(seen["self"]).parent and install_cwd != work, install_cwd
                print("PASS: translated AWS YAML recipe applied: run supervised in {work:path}, artifact at "
                      "{artifacts:path}/<file>, AWS default configuration delivered, no nucleus environment; "
                      "install ran in the version's artifact directory as reported")

                deploy("0x602", dict(v1, configuration=dict(v1["configuration"], merge={"interval": 2})))
                wait_for(lambda: outcome("0x602").get("status") == 1, "0x602 was not applied", process)
                wait_for(lambda: configuration().get("interval") == 2, "0x602 configuration did not reach the component", process)
                assert configuration() == {"interval": 2, "labels": {"site": "lab"}}, configuration()
                assert events()[:2] == ["shutdown 1.0.0", "stopped 1.0.0"], events()
                print("PASS: per-deployment merge (AWS MERGE update) applied; the translated shutdown step ran first")

                deploy("0x603", v2)
                wait_for(lambda: outcome("0x603").get("status") == 1, "0x603 was not applied", process)
                wait_for(lambda: running("1.1.0") and record("1.1.0"), "0x603 did not run", process)
                assert configuration() == {"interval": 2, "labels": {"site": "lab"}, "mode": "fast"}, configuration()
                assert events()[-2:] == ["shutdown 1.0.0", "stopped 1.0.0"], events()
                print("PASS: translated upgrade kept the device value and added the new default key (native SF-015 rule)")

                # What the translator refuses to produce: no install or run command.
                bare = {k: v for k, v in v1.items() if k not in ("configuration", "lifecycle")}
                deploy("0x604", dict(bare, schemaVersion="1.0", componentName="artifact-only", lifecycle={}, configuration={}))
                wait_for(lambda: outcome("0x604").get("status") == 1, "0x604 was not applied", process)
                arguments = (probe / "updater-args").read_text().split("\n")[:-1]
                assert len(arguments) == 1 and Path(arguments[0]).name and running("1.1.0"), arguments
                print("PASS: a native recipe without install or run is handed to the platform updater; "
                      "the translator rejects that AWS shape (no-lifecycle-command)")

                wait_for(lambda: all(outcome(f"0x60{i}").get("reported") for i in "1234"), "reports pending", process)
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None
                assert not sim.errors, sim.errors
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=ROOT / "suifleet-edge/target/debug/suifleet")
    args = parser.parse_args()
    run(args.binary.resolve())
