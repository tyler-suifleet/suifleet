#!/usr/bin/env python3
"""CloudWatch Logs export (SF-060): the real adapter process
(`suifleet --cloudwatch-logs`) against the request-checking local CloudWatch
Logs emulator (scripts/cloudwatch_logs_emulator.py), without AWS.

  real daemon, supervised svc with      -> with [runtime] log_group_read the logs and
  confidential overlay value, api key,     retained values are 0640; the adapter uploads
  levels and a 300 KiB line                svc and System lines in order under the
                                           LogManager 2.3.14 group/stream names; the
                                           overlay value and api key never reach the spool
                                           or the emulator; DEBUG is filtered; the long
                                           line is split at the event limit
  ingress outage (503)                  -> lines spooled, then delivered in order
  response lost                         -> the batch is resent: at least once, none lost
  adapter SIGKILL with lines spooled    -> nothing lost after the restart
  rotation into .1                      -> every line once, in order
  symlinked/hard-linked component log   -> refused; the sibling's lines never leave
  small spool while the ingress is down -> oldest events dropped and counted; the journal
                                           stays bounded on disk
  missing credentials / bad config      -> the adapter refuses to start
Every request's SigV4 signature, shape and PutLogEvents limits are checked by
the emulator; the run fails on any violation.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ota  # noqa: E402
from cloudwatch_logs_emulator import CREDENTIAL_TOKEN, Emulator  # noqa: E402
from test_logs import wait_for  # noqa: E402
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGION = "eu-west-1"
THING = "fleet:pi-1"
OVERLAY_VALUE = "Ov3rlayValueQ9-brokerlogin"
API_KEY = "k3y-abcdef0123"
LONG = 300_000
# Prints its lines once (a marker in the persistent work directory).
RUN = ("if [ ! -e printed ]; then echo 'connecting to broker with " + OVERLAY_VALUE + "'; echo 'api_key=" + API_KEY + "'; "
       "echo 'WARN low disk'; echo 'DEBUG detail hidden'; "
       f"head -c {LONG} /dev/zero | tr '\\0' 'x'; echo; "
       "i=0; while [ $i -lt 50 ]; do echo tick $i; i=$((i+1)); done; touch printed; fi; "
       "touch ready; exec sleep 600")


def group(name, kind="UserComponent"):
    return f"/aws/greengrass/{kind}/{REGION}/{name}"


def adapter_config(path, state_dir, spool_dir, endpoint, components, system=True, **extra):
    config = {
        "region": REGION, "thingName": THING, "stateDir": str(state_dir), "spoolDir": str(spool_dir), "endpoint": endpoint,
        "periodicUploadIntervalSec": 1, "readIntervalSec": 1,
        "logsUploaderConfiguration": {
            "systemLogsConfiguration": {"uploadToCloudWatch": system},
            "componentLogsConfigurationMap": {name: {} for name in components},
        },
        **extra,
    }
    path.write_text(json.dumps(config))
    return path


class Adapter:
    def __init__(self, binary, config, emulator, log_path):
        self.args = [str(binary), "--cloudwatch-logs", str(config)]
        self.env = {**os.environ, "AWS_CONTAINER_CREDENTIALS_FULL_URI": emulator.credentials_uri,
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN": CREDENTIAL_TOKEN, "RUST_LOG": "suifleet=info"}
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
            self.env.pop(name, None)
        self.log_path = log_path
        self.process = None

    def start(self):
        self.log = open(self.log_path, "a")
        self.process = subprocess.Popen(self.args, env=self.env, stdout=self.log, stderr=self.log)
        return self

    def stop(self, sig=signal.SIGTERM):
        if self.process and self.process.poll() is None:
            self.process.send_signal(sig)
            self.process.wait(timeout=30)
        if self.process:
            self.log.close()
        self.process = None


def messages(emulator, name, kind="UserComponent"):
    return [m for _, m in emulator.stored(group(name, kind))]


def spool_text(spool_dir):
    text = []
    try:
        for path in Path(spool_dir).iterdir():
            text.append(path.read_text(errors="replace"))
    except (FileNotFoundError, IsADirectoryError):
        pass  # not created yet, or replaced by a compaction meanwhile
    return "".join(text)


def check_clean(emulator):
    state = emulator.state()
    assert not state["violations"], state["violations"]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def daemon_part(binary, emulator, root):
    """The real daemon writes the logs; the adapter exports them."""
    sim = Simulator()
    sim.allow_reports = True
    sim.fail_execution = False
    sim.truncate_next_artifact = False
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def respond(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                response = {"jsonrpc": "2.0", "id": body["id"], "result": sim.rpc(body["method"], body["params"])}
            except Exception as error:
                sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": str(error)}}
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
    daemon = None
    adapter = None
    root.mkdir()
    try:
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        overlays = root / "overlays"
        overlays.mkdir(mode=0o700)
        (overlays / "svc.json").write_text(json.dumps({"/broker_login": OVERLAY_VALUE}))
        (overlays / "svc.json").chmod(0o600)
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
            '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
            f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\ncomponent_startup_seconds = 1\nlog_group_read = true\n'
            f'[components]\nconfiguration_overlay_dir = {json.dumps(str(overlays))}\n')

        def journal():
            try:
                return json.loads((state_dir / "state.json").read_text())
            except (OSError, ValueError):
                return {}

        with (root / "daemon.out").open("w") as out:
            daemon = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=out, stderr=out)
            wait_for(lambda: journal().get("initialized"), "daemon did not start", daemon)
            sim.add("0x501")
            event = sim.events[-1]["parsedJson"]
            event["component_name"] = "svc"
            sim.recipes[event["recipe_blob_id"]].update(componentName="svc", lifecycle={"run": RUN, "readiness": "test -e ready"})
            wait_for(lambda: journal().get("deployments", {}).get("0x501", {}).get("status") == 1, "svc was not applied", daemon)

            component_log = state_dir / "supervised/svc/component.log"
            for path, expected in ((component_log, 0o640), (state_dir / "logs/confidential.json", 0o640), (state_dir / "logs/daemon.log", 0o640),
                                   (state_dir / "supervised/svc", 0o750), (state_dir / "supervised", 0o750), (state_dir / "logs", 0o750)):
                assert mode(path) == expected, f"{path} is {oct(mode(path))}, not {oct(expected)}"
            print("PASS: [runtime] log_group_read: component and daemon logs and retained values 0640, their directories 0750")

            spool = root / "cw-spool"
            cfg = adapter_config(root / "cw.json", state_dir, spool, emulator.url, ["svc"])
            adapter = Adapter(binary, cfg, emulator, root / "adapter.out").start()
            wait_for(lambda: "tick 49" in messages(emulator, "svc"), "svc lines did not arrive", adapter.process, timeout=60)
            wait_for(lambda: messages(emulator, "System", "GreengrassSystemComponent"), "daemon lines did not arrive", adapter.process, timeout=30)
            svc = messages(emulator, "svc")
            assert [m for m in svc if m.startswith("tick ")] == [f"tick {i}" for i in range(50)], svc[:6]
            assert "connecting to broker with [REDACTED]" in svc and "api_key=[REDACTED]" in svc, svc[:4]
            assert "WARN low disk" in svc and not any("DEBUG" in m for m in svc)
            long_parts = [m for m in svc if set(m) == {"x"}]
            assert [len(p) for p in long_parts] == [262118, LONG - 262118], [len(p) for p in long_parts]
            streams = {s for (g, s) in [k.split("|") for k in emulator.state()["streams"]] if g == group("svc")}
            today = time.strftime("%Y/%m/%d", time.gmtime())
            assert streams <= {f"/{today}/thing/fleet+pi-1", f"/{time.strftime('%Y/%m/%d', time.gmtime(time.time() - 120))}/thing/fleet+pi-1"}, streams
            system = messages(emulator, "System", "GreengrassSystemComponent")
            for secret in (OVERLAY_VALUE, API_KEY):
                assert all(secret not in m for m in svc + system), f"{secret[:6]}… reached CloudWatch"
                journal_text = (spool / "spool.journal").read_text()
                events = [e["message"] for line in journal_text.splitlines() for record in json.loads(line).values() for e in record.get("events", [])]
                assert all(secret not in m for m in events), f"{secret[:6]}… reached a spooled event"
            assert mode(spool) == 0o700 and mode(spool / "spool.journal") == 0o600
            check_clean(emulator)
            print(f"PASS: real daemon: {len(svc)} svc and {len(system)} System events in CloudWatch order under "
                  f"{group('svc')} and stream /{today}/thing/fleet+pi-1; overlay value and api key redacted before the spool; "
                  "DEBUG filtered; a 300,000-byte line split into 262,118 + 37,882 bytes")
            adapter.stop()
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=30)
            daemon = None
        assert not sim.errors, sim.errors
    finally:
        if adapter:
            adapter.stop(signal.SIGKILL)
        if daemon and daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        shim.kill()
        server.shutdown()


def file_part(binary, emulator, root):
    """Failure paths against a state directory written by the test."""
    state = root / "state"
    (state / "logs").mkdir(parents=True)
    (state / "logs/confidential.json").write_text(json.dumps({"values": {"Pl8inPIN": {}}}))
    app = state / "supervised/app/component.log"
    app.parent.mkdir(parents=True)
    app.touch()

    def write(path, lines):
        with open(path, "a") as f:
            f.writelines(line + "\n" for line in lines)

    spool = root / "spool"
    cfg = adapter_config(root / "cw.json", state, spool, emulator.url, ["app", "evil", "linked"], system=False)
    adapter = Adapter(binary, cfg, emulator, root / "adapter.out").start()
    try:
        # Outage: lines wait in the spool, then arrive in order.
        with emulator.lock:
            emulator.down = True
        write(app, [f"outage {i} Pl8inPIN" for i in range(30)])
        wait_for(lambda: "outage 29" in spool_text(spool), "lines were not spooled during the outage", adapter.process)
        time.sleep(2.5)
        assert not messages(emulator, "app")
        with emulator.lock:
            emulator.down = False
        wait_for(lambda: len(messages(emulator, "app")) == 30, "the spool did not drain after the outage", adapter.process)
        assert messages(emulator, "app") == [f"outage {i} [REDACTED]" for i in range(30)]
        print("PASS: outage: 30 lines spooled (redacted) while CloudWatch answered 503, then delivered once, in order")

        # A lost response: the batch is sent again, nothing lost.
        emulator.queue(["response-lost"])
        write(app, ["lost a", "lost b"])
        wait_for(lambda: messages(emulator, "app")[-2:] == ["lost a", "lost b"] and messages(emulator, "app").count("lost a") == 2,
                 "a batch whose response was lost was not resent", adapter.process)
        print("PASS: response lost: the same batch was resent unchanged (at least once)")

        # SIGKILL with lines spooled but not yet sent; the restart sends them.
        with emulator.lock:
            emulator.down = True
        write(app, [f"crash {i}" for i in range(5)])
        wait_for(lambda: "crash 4" in spool_text(spool), "lines were not spooled before the kill", adapter.process)
        adapter.stop(signal.SIGKILL)
        with emulator.lock:
            emulator.down = False
        adapter.start()
        wait_for(lambda: "crash 4" in messages(emulator, "app"), "spooled lines were lost across SIGKILL", adapter.process)
        assert [m for m in messages(emulator, "app") if m.startswith("crash")] == [f"crash {i}" for i in range(5)]
        print("PASS: adapter SIGKILL with 5 lines spooled: all delivered once after the restart")

        # Rotation into .1 between reads.
        adapter.stop()
        write(app, ["before rotation"])
        os.rename(app, str(app) + ".1")
        write(app, ["after rotation"])
        adapter.start()
        wait_for(lambda: "after rotation" in messages(emulator, "app"), "lines after a rotation did not arrive", adapter.process)
        tail = messages(emulator, "app")[-2:]
        assert tail == ["before rotation", "after rotation"], tail
        print("PASS: rotation: the rest of component.log.1, then the new file, each line once")

        # Sibling access: links out of a component's log are refused.
        sibling = state / "supervised/sibling/component.log"
        sibling.parent.mkdir()
        write(sibling, ["sibling private line"])
        (state / "supervised/evil").mkdir()
        os.symlink(sibling, state / "supervised/evil/component.log")
        (state / "supervised/linked").mkdir()
        os.link(sibling, state / "supervised/linked/component.log")
        write(app, ["after links"])
        wait_for(lambda: "after links" in messages(emulator, "app"), "the adapter stopped after refusing links", adapter.process)
        time.sleep(2)
        everything = json.dumps(emulator.state()["streams"]) + spool_text(spool)
        assert "sibling private line" not in everything, "a sibling's log line left through a link"
        assert "Cannot read this source" in Path(adapter.log_path).read_text()
        print("PASS: sibling access: a symlinked and a hard-linked component log were refused; the sibling's lines never left")
        adapter.stop()

        # Disk limit: a small spool while CloudWatch is down drops the oldest, counted.
        small_spool = root / "small-spool"
        small = adapter_config(root / "small.json", state, small_spool, emulator.url, ["app"], system=False, spoolLimitBytes=2000)
        limited = Adapter(binary, small, emulator, root / "small.out")
        # Start after what is already there: only new lines.
        limited.start()
        wait_for(lambda: (small_spool / "spool.journal").exists() and spool_text(small_spool).strip(), "the small spool was not created", limited.process)
        with emulator.lock:
            emulator.down = True
        before = len(messages(emulator, "app"))
        write(app, [f"bulk {i:04d} " + "z" * 40 for i in range(400)])
        wait_for(lambda: "bulk 0399" in spool_text(small_spool), "the bulk lines were not read", limited.process)
        size = (small_spool / "spool.journal").stat().st_size
        assert size < 2 * 1024 * 1024, f"spool journal grew to {size} bytes"
        with emulator.lock:
            emulator.down = False
        wait_for(lambda: any(m.startswith("bulk 0399") for m in messages(emulator, "app")), "the newest bulk lines were not delivered", limited.process)
        bulk = [m for m in messages(emulator, "app")[before:] if m.startswith("bulk")]
        kept = len(bulk)
        assert 0 < kept < 400 and bulk == [f"bulk {i:04d} " + "z" * 40 for i in range(400 - kept, 400)], (kept, bulk[:2])
        assert kept * (len(bulk[0]) + 26) <= 2000
        limited.stop()
        records = [json.loads(line) for line in (small_spool / "spool.journal").read_text().splitlines()]
        dropped = max(body.get("dropped", 0) for record in records for body in record.values())
        assert dropped >= 400 - kept, (dropped, kept)
        print(f"PASS: disk limit: a 2,000-byte spool kept the newest {kept} of 400 lines and counted {dropped} dropped "
              f"(oldest first); the journal stayed at {size} bytes")
    finally:
        adapter.stop(signal.SIGKILL)

    # Refusals: no credentials, unsupported LogManager option.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    result = subprocess.run([str(binary), "--cloudwatch-logs", str(cfg)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "AWS_CONTAINER_CREDENTIALS_FULL_URI" in result.stderr, result.stderr[-400:]
    bad = json.loads(cfg.read_text())
    bad["logsUploaderConfiguration"]["componentLogsConfigurationMap"]["app"]["logFileRegex"] = "app.*"
    (root / "bad.json").write_text(json.dumps(bad))
    result = subprocess.run([str(binary), "--cloudwatch-logs", str(root / "bad.json")], env=adapter.env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "logFileRegex" in result.stderr, result.stderr[-400:]
    check_clean(emulator)
    print("PASS: refused to start without container credentials and with an unsupported LogManager option (logFileRegex)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=ROOT / "suifleet-edge/target/debug/suifleet")
    binary = parser.parse_args().binary.resolve()
    emulator = Emulator()
    emulator.start()
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-cwlogs-") as temporary:
            daemon_part(binary, emulator, Path(temporary) / "daemon")
        emulator.reset()
        with tempfile.TemporaryDirectory(prefix="suifleet-cwlogs-") as temporary:
            file_part(binary, emulator, Path(temporary))
    finally:
        emulator.stop()


if __name__ == "__main__":
    main()
