#!/usr/bin/env python3
"""Component configuration (SF-015) with the real daemon against the local
chain/Walrus simulator, without keys or gas.

Supervised component `cfg` (its run copies $SUIFLEET_CONFIGURATION), with
`/db/role` confidential and provisioned only in the device overlay:
  0x501 1.0.0 defaults                 -> runs with defaults + overlay value;
                                          its install prints the configuration
                                          into the private component log only;
                                          no overlay value in public state or logs
  0x502 1.0.0 merge                    -> merged over the persisted config
  0x503 2.0.0 new defaults, reset      -> existing values kept, missing defaults
                                          added, reset pointer takes the default
  0x504 reset through a scalar         -> Failed before any change
  0x505 validate command rejects       -> Failed before any change
  0x506 validate command defers        -> nothing changes until it accepts
  0x507 validate defers past the limit -> Failed
  0x508 unprovisioned confidential     -> Failed before any change
  daemon restart, overlay rotated      -> restarted with the persisted config
                                          and the new overlay value
  0x509 merge after the restart        -> builds on the persisted config
Install-only component `tool`:
  0x50a defaults + merge + confidential -> its install receives {configuration};
                                          its output reaches only its component log
  0x50b deferred, then deleted         -> dropped; its deferral pruned
"""
import argparse
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

RUN = 'cp "$SUIFLEET_CONFIGURATION" seen.json; exec sleep 300'
# An install that prints the configuration it receives, confidential values
# included, on stdout and stderr: that output must stay out of the daemon log.
PRINT = "cat {configuration}; cat {configuration} >&2"

VALUES = ("private-one", "private-two")


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False

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
            if blob == "artifact":
                self.respond(test_ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def deploy(id, version, configuration, name="cfg", **lifecycle):
        sim.add(id, event=dict(component_name=name, version=version),
                recipe=dict(schemaVersion="1.2", lifecycle=lifecycle or {"run": RUN}, configuration=configuration))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-configuration-") as temporary:
            root = Path(temporary)
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
            state_dir = root / "state"
            overlay = root / "overlay"
            overlay.mkdir()
            overlay_file = overlay / "cfg.json"

            def provision(value):
                overlay_file.write_text(json.dumps({"/db/role": value}))
                overlay_file.chmod(0o600)

            provision(VALUES[0])
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\n'
                'component_startup_seconds = 1\ncomponent_ready_timeout_seconds = 3\n'
                'component_stop_timeout_seconds = 3\ncomponent_max_restarts = 3\n'
                f'[components]\nconfiguration_overlay_dir = {json.dumps(str(overlay))}\n'
                'configuration_defer_max_seconds = 4\n')
            components = state_dir / "supervised"
            work = components / "cfg" / "work"
            committed = state_dir / "configuration" / "cfg" / "committed.json"

            def journal():
                path = state_dir / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcome(id):
                return journal().get("deployments", {}).get(id, {})

            def status():
                try:
                    return json.loads((components / "status.json").read_text()).get("cfg", {})
                except (OSError, ValueError):
                    return {}

            def running(version):
                current = status()
                return current.get("phase") == "running" and current.get("version") == version

            def seen():
                try:
                    return json.loads((work / "seen.json").read_text())
                except (OSError, ValueError):
                    return None

            def effective():
                return json.loads(committed.read_text())["effective"]

            def unchanged(id, expected, reason):
                wait_for(lambda: outcome(id).get("status") == 2, f"{id} was not reported Failed", process)
                assert reason in outcome(id)["error"], outcome(id)
                public = dict(expected, db={k: v for k, v in expected["db"].items() if k != "role"})
                assert seen() == expected and effective() == public, (seen(), effective())
                assert not (committed.parent / "staged.json").exists() or json.loads(
                    (committed.parent / "staged.json").read_text())["deployment"] != id

            with (root / "daemon.log").open("w") as log:
                def start():
                    return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

                process = start()
                wait_for(lambda: journal().get("initialized"), "daemon did not establish its cursor", process)

                deploy("0x501", "1.0.0", {"defaults": {"port": 8080, "log": {"level": "info"}, "db": {"host": "db1"}},
                                          "confidential": ["/db/role"]}, run=RUN, install=PRINT)
                wait_for(lambda: outcome("0x501").get("status") == 1, "0x501 was not applied", process)
                wait_for(lambda: running("1.0.0") and seen(), "0x501 did not run", process)
                v1 = {"port": 8080, "log": {"level": "info"}, "db": {"host": "db1", "role": VALUES[0]}}
                assert seen() == v1, seen()
                assert effective() == {"port": 8080, "log": {"level": "info"}, "db": {"host": "db1"}}, effective()
                assert (components / "cfg" / "component.log").read_text().count(VALUES[0]) == 2
                print("PASS: defaults applied; the confidential value came from the device overlay only")

                (work / "seen.json").unlink()
                deploy("0x502", "1.0.0", {"merge": {"port": 9000, "log": {"level": "debug"}}, "confidential": ["/db/role"]})
                wait_for(lambda: outcome("0x502").get("status") == 1, "0x502 was not applied", process)
                wait_for(lambda: seen(), "0x502 did not run", process)
                v2 = {"port": 9000, "log": {"level": "debug"}, "db": {"host": "db1", "role": VALUES[0]}}
                assert seen() == v2, seen()
                print("PASS: merge applied over the persisted configuration; the component restarted with it")

                (work / "seen.json").unlink()
                deploy("0x503", "2.0.0", {"defaults": {"port": 8080, "log": {"level": "info", "format": "json"}, "db": {"host": "db2"}},
                                          "reset": ["/db/host"], "confidential": ["/db/role"]})
                wait_for(lambda: outcome("0x503").get("status") == 1, "0x503 was not applied", process)
                wait_for(lambda: running("2.0.0") and seen(), "0x503 did not run", process)
                v3 = {"port": 9000, "log": {"level": "debug", "format": "json"}, "db": {"host": "db2", "role": VALUES[0]}}
                assert seen() == v3, seen()
                print("PASS: new version: customized values kept, new defaults added, reset pointer took its default")

                deploy("0x504", "3.0.0", {"reset": ["/log/level/x"], "confidential": ["/db/role"]})
                unchanged("0x504", v3, "/log/level/x")
                deploy("0x505", "3.0.0", {"merge": {"port": 70000}, "confidential": ["/db/role"]},
                       run=RUN, validate="echo port out of range >&2; ! grep -q 70000 {configuration}")
                unchanged("0x505", v3, "rejected by the component's validate command")
                assert "port out of range" not in outcome("0x505")["error"]
                assert "port out of range" in (components / "cfg" / "component.log").read_text()
                deploy("0x508", "3.0.0", {"confidential": ["/db/role", "/api/token"]})
                unchanged("0x508", v3, "/api/token is not provisioned")
                assert running("2.0.0"), status()
                print("PASS: invalid reset, a rejecting validate command and an unprovisioned confidential "
                      "value each failed the deployment before anything changed")

                deploy("0x506", "3.0.0", {"merge": {"port": 9100}, "confidential": ["/db/role"]},
                       run=RUN, validate="test -e {work}/allow || exit 75; grep -q 9100 {configuration}")
                wait_for(lambda: "0x506" in journal().get("configuration_deferrals", {}), "0x506 was not deferred", process)
                assert not outcome("0x506") and running("2.0.0") and seen() == v3, (outcome("0x506"), status())
                (work / "seen.json").unlink()
                (work / "allow").touch()
                wait_for(lambda: outcome("0x506").get("status") == 1, "0x506 was not applied once allowed", process)
                wait_for(lambda: running("3.0.0") and seen(), "0x506 did not run", process)
                v6 = dict(v3, port=9100)
                assert seen() == v6 and "0x506" not in journal().get("configuration_deferrals", {}), seen()
                print("PASS: a deferring validate command held the update until it accepted it")

                deploy("0x507", "4.0.0", {"merge": {"port": 1}, "confidential": ["/db/role"]}, run=RUN, validate="exit 75")
                wait_for(lambda: "0x507" in journal().get("configuration_deferrals", {}), "0x507 was not deferred", process)
                unchanged("0x507", v6, "still deferred")
                print("PASS: an update deferred past configuration_defer_max_seconds failed")

                # Restart with a rotated overlay value.
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                (work / "seen.json").unlink()
                provision(VALUES[1])
                process = start()
                wait_for(lambda: running("3.0.0") and seen(), "the committed version did not restart", process)
                assert seen() == dict(v6, db={"host": "db2", "role": VALUES[1]}), seen()
                (work / "seen.json").unlink()
                deploy("0x509", "3.0.1", {"merge": {"extra": True}, "confidential": ["/db/role"]})
                wait_for(lambda: outcome("0x509").get("status") == 1, "0x509 was not applied", process)
                wait_for(lambda: running("3.0.1") and seen(), "0x509 did not run", process)
                assert seen() == dict(v6, extra=True, db={"host": "db2", "role": VALUES[1]}), seen()
                print("PASS: after a daemon restart the component ran with its persisted configuration and the "
                      "rotated overlay value; the next update built on the persisted configuration")

                received = root / "tool-config.json"
                tool_overlay = overlay / "tool.json"
                tool_overlay.write_text(json.dumps({"/key": VALUES[1]}))
                tool_overlay.chmod(0o600)
                deploy("0x50a", "1.0.0", {"defaults": {"mode": "a", "n": 1}, "merge": {"n": 2}, "confidential": ["/key"]},
                       name="tool", install=f"cp {{configuration}} {received}; {PRINT}")
                wait_for(lambda: outcome("0x50a").get("status") == 1, "0x50a was not applied", process)
                assert json.loads(received.read_text()) == {"mode": "a", "n": 2, "key": VALUES[1]}, received.read_text()
                received.unlink()
                tool = json.loads((state_dir / "configuration" / "tool" / "committed.json").read_text())
                assert tool["effective"] == {"mode": "a", "n": 2} and tool["deployment"] == "0x50a", tool
                tool_log = components / "tool" / "component.log"
                assert tool_log.read_text().count(VALUES[1]) == 2 and tool_log.stat().st_mode & 0o777 == 0o600
                print("PASS: an install-only component's install received its configuration; its output went to "
                      "its private component log")

                # A deferred deployment that is deleted leaves no deferral behind.
                deploy("0x50b", "4.0.0", {"merge": {"port": 2}, "confidential": ["/db/role"]}, run=RUN, validate="exit 75")
                wait_for(lambda: "0x50b" in journal().get("configuration_deferrals", {}), "0x50b was not deferred", process)
                sim.deleted.add("0x50b")
                wait_for(lambda: "0x50b" not in journal().get("configuration_deferrals", {}), "0x50b's deferral was kept", process)
                assert not outcome("0x50b") and running("3.0.1"), (outcome("0x50b"), status())
                print("PASS: a deferred deployment deleted on chain is dropped without applying; its deferral is pruned")

                wait_for(lambda: all(outcome(f"0x50{i}").get("reported") for i in "123456789a"), "reports pending", process)
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=20) == 0
                process = None

            # No confidential value in public state, the journal or diagnostics.
            leaks = []
            for path in [root / "daemon.log", *state_dir.rglob("*")]:
                if path.is_file() and path.name not in ("configuration.json", "component.log", "seen.json"):
                    text = path.read_bytes()
                    leaks += [str(path) for value in VALUES if value.encode() in text]
            assert not leaks, leaks
            for name in ("configuration.json", "component.log"):
                for path in state_dir.rglob(name):
                    assert path.stat().st_mode & 0o777 == 0o600, path
            assert not sim.errors, sim.errors
            print("PASS: confidential values only in 0600 materialized files; none in state, journal or logs")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    args = parser.parse_args()
    run(args.binary.resolve())
