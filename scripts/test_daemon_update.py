#!/usr/bin/env python3
"""Daemon self-update (SF-026) with the real daemon against the local
chain/Walrus simulator, without keys or gas.

A small loop stands in for the service manager: it starts `suifleet --launch`
and restarts it whenever it exits non-zero (systemd's Restart=on-failure).
scripts/test_daemon_update_systemd.py covers the same under real systemd,
including a new daemon that hangs before readiness.

  0x701 v2  a working new daemon      -> staged, preflighted, restart (75);
                                         the launcher execs it (same service
                                         start), it confirms and reports Applied
                                         with the same identity and journal
  restart                             -> the confirmed v2 starts again
  0x702 v3  preflight passes, startup -> two counted attempts, then the
            fails                        launcher restarts v2, which reports
                                         Failed; v2 stays current
  0x703 v4  cannot run at all         -> rejected by the preflight self-check;
                                         no restart, the running daemon is kept
  0x704 v5  daemon killed right after -> the next start still hands over and
            staging (interrupted)        v5 confirms
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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

COMPONENT = "suifleet-daemon"
# Passes the preflight self-check, then fails every real start.
FAILS_AT_STARTUP = b'#!/bin/sh\n[ "$1" = --self-check ] && exit 0\necho "broken daemon" >&2\nexit 3\n'
NOT_RUNNABLE = b"\x7fELF this is not an executable\n"


class ServiceManager:
    """Restart `suifleet --launch` on non-zero exit, like Restart=on-failure."""

    def __init__(self, binary, config, log):
        self.binary, self.config, self.log = binary, config, log
        self.process, self.starts, self.exits = None, 0, []
        self.stopping = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stopping:
            self.starts += 1
            self.process = subprocess.Popen([str(self.binary), "--launch"], env={**os.environ, "EDGE_CONFIG": str(self.config)},
                                            stdout=self.log, stderr=self.log)
            code = self.process.wait()
            self.exits.append(code)
            if self.stopping or code == 0:
                return
            time.sleep(0.5)

    def pid(self):
        return self.process.pid if self.process and self.process.poll() is None else None

    def exe(self):
        pid = self.pid()
        try:
            return os.readlink(f"/proc/{pid}/exe") if pid else None
        except OSError:
            return None

    def stop(self):
        self.stopping = True
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=30)
        self.thread.join(timeout=30)


def run(binary):
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    blobs = {}

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
            if blob in blobs:
                self.respond(blobs[blob])
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    def deploy(id, version, payload):
        sim.add(id)
        event = sim.events[-1]["parsedJson"]
        digest = hashlib.sha256(payload).hexdigest()
        blobs[f"artifact-{id}"] = payload
        event.update(component_name=COMPONENT, version=version, artifact_sha256=digest)
        sim.recipes[event["recipe_blob_id"]].update(componentName=COMPONENT, componentVersion=version,
                                                     artifactBlobId=f"artifact-{id}", artifactSha256=digest, lifecycle={})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    manager = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-daemon-update-") as temporary:
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
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\n'
                f'[daemon_update]\nenabled = true\ncomponent = "{COMPONENT}"\nmax_start_attempts = 2\npreflight_timeout_seconds = 30\n')
            daemon_dir = state_dir / "daemon"

            def journal():
                try:
                    return json.loads((state_dir / "state.json").read_text())
                except (OSError, ValueError):
                    return {}

            def outcome(id):
                return journal().get("deployments", {}).get(id, {})

            def current():
                try:
                    return (daemon_dir / "current").read_text().strip()
                except OSError:
                    return None

            def reported():
                return {id: status for id, status in sim.reports}

            log = (root / "daemon.log").open("w")
            manager = ServiceManager(binary, config, log)
            manager.start()
            wait_for(lambda: journal().get("initialized"), "daemon did not establish its cursor")
            binding = journal()["binding"]
            base = os.path.realpath(binary)
            assert manager.exe() == base, manager.exe()

            # A working new daemon (the same build, delivered as an update).
            deploy("0x701", "2.0.0", Path(binary).read_bytes())
            wait_for(lambda: outcome("0x701").get("status") == 1, "the new daemon did not confirm and apply")
            v2 = current()
            assert v2 and "/versions/2.0.0-" in v2, v2
            assert 75 in manager.exits, f"no handoff restart: {manager.exits}"
            wait_for(lambda: manager.exe() == v2, "the running daemon is not the confirmed update")
            assert journal()["binding"] == binding, "identity/journal binding changed across the handoff"
            assert not (daemon_dir / "handoff.json").exists()
            wait_for(lambda: outcome("0x701").get("reported"), "the update's outcome was not reported")
            print("PASS: new daemon staged, preflighted and started by the launcher; confirmed; Applied reported with the same identity and journal")

            # A restart keeps running the confirmed update.
            pid = manager.pid()
            os.kill(pid, signal.SIGKILL)
            wait_for(lambda: manager.pid() not in (None, pid) and manager.exe() == v2, "the confirmed update did not start again")
            print("PASS: after a crash the launcher starts the confirmed update again")

            # Passes the preflight, fails every start: rolled back after two attempts.
            starts = manager.starts
            deploy("0x702", "3.0.0", FAILS_AT_STARTUP)
            wait_for(lambda: outcome("0x702").get("status") == 2, "the broken update was not rolled back and failed")
            assert "did not complete startup in 2 attempt(s)" in outcome("0x702")["error"], outcome("0x702")
            assert current() == v2, current()
            assert manager.exits.count(3) >= 2, manager.exits
            wait_for(lambda: manager.exe() == v2, "the previous daemon is not running after rollback")
            assert not any("3.0.0-" in p.name for p in (daemon_dir / "versions").iterdir()), "the failed candidate was kept"
            assert manager.starts - starts >= 3
            print("PASS: a new daemon that fails at startup: two counted attempts, then the launcher restarted v2, which reported Failed")

            # Cannot run at all: rejected before any handoff.
            pid, starts = manager.pid(), manager.starts
            deploy("0x703", "4.0.0", NOT_RUNNABLE)
            wait_for(lambda: outcome("0x703").get("status") == 2, "the unrunnable update was not rejected")
            assert "preflight self-check" in outcome("0x703")["error"], outcome("0x703")
            assert manager.pid() == pid and manager.starts == starts, "an unrunnable update caused a restart"
            print("PASS: an unrunnable binary failed its preflight self-check; the running daemon was kept (no restart)")

            # Killed right after staging: the next start still hands over.
            deploy("0x704", "5.0.0", Path(binary).read_bytes())
            wait_for(lambda: (daemon_dir / "handoff.json").exists() or outcome("0x704").get("status"), "the update was not staged")
            pid = manager.pid()
            if pid and (daemon_dir / "handoff.json").exists():
                os.kill(pid, signal.SIGKILL)
            wait_for(lambda: outcome("0x704").get("status") == 1, "the interrupted handoff did not complete")
            assert "/versions/5.0.0-" in current(), current()
            assert journal()["binding"] == binding
            print("PASS: a daemon killed right after staging: the next start handed over and v5 confirmed")

            wait_for(lambda: {"0x701", "0x702", "0x703", "0x704"} <= set(reported()), f"reports missing: {reported()}")
            assert reported()["0x701"] == 1 and reported()["0x702"] == 2 and reported()["0x703"] == 2 and reported()["0x704"] == 1, reported()
            assert not sim.errors, sim.errors
            print("PASS: all outcomes reported on chain (Applied, Failed, Failed, Applied)")
            manager.stop()
            manager = None
            log.close()
    finally:
        if manager:
            manager.stop()
        shim.kill()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
