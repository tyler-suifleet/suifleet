#!/usr/bin/env python3
"""CloudWatch Logs export (SF-060): the real adapter process
(`suifleet --cloudwatch-logs`) against the request-checking local CloudWatch
Logs emulator (scripts/cloudwatch_logs_emulator.py), without AWS.

  real daemon, supervised svc with      -> an installation first run without, then with
  confidential overlay value, api key,     [runtime] log_group_read: the existing logs and
  levels and a 300 KiB line                retained values become 0640; the adapter uploads
                                           svc and System lines in order under the
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
  log_group_read turned off again       -> every existing log 0600, directories without group
                                           access, no daemon log written
With --distinct-uids (root, in a disposable container: make test-cloudwatch-logs-uids)
only the real-daemon part runs, with the daemon and the adapter under separate
non-root UIDs and UMask 0077, the adapter reading only through the daemon's group.
It then takes credentials from the actual SF-057 credential adapter through its
wrapper: an expired token (401) makes the adapter exit, and a new start through
the wrapper delivers what was pending.
Every request's SigV4 signature, shape and PutLogEvents limits are checked by
the emulator; the run fails on any violation.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
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
# --distinct-uids: the daemon's user/group and the adapter's user (in the daemon's group only).
DAEMON_UID = 61601
ADAPTER_UID = 61602
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


def as_user(uid, groups=()):
    """A preexec function dropping to `uid` (its own GID) under UMask 0077, as the documented units."""
    def drop():
        os.setgroups(list(groups))
        os.setgid(uid)
        os.setuid(uid)
        os.umask(0o077)
    return drop


class Adapter:
    def __init__(self, binary, config, emulator, log_path, preexec=None, wrapper=()):
        # `wrapper`: the SF-057 wrapper command, which replaces the credential variables.
        self.args = [*map(str, wrapper), str(binary), "--cloudwatch-logs", str(config)]
        self.env = {**os.environ, "AWS_CONTAINER_CREDENTIALS_FULL_URI": emulator.credentials_uri,
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN": CREDENTIAL_TOKEN, "RUST_LOG": "suifleet=info"}
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
            self.env.pop(name, None)
        self.log_path = log_path
        self.preexec = preexec
        self.process = None

    def start(self):
        self.log = open(self.log_path, "a")
        self.process = subprocess.Popen(self.args, env=self.env, stdout=self.log, stderr=self.log, preexec_fn=self.preexec)
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


def daemon_part(binary, emulator, root, distinct_uids=False):
    """The real daemon writes the logs; the adapter exports them. The
    installation first runs without `log_group_read`, as one configured
    before it existed, then with it."""
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
    if distinct_uids:
        # The service's state directory (StateDirectoryMode=0750) and a binary both users can run.
        root.parent.chmod(0o711)
        shutil.copy(binary, root / "suifleet")
        binary = root / "suifleet"
        binary.chmod(0o755)
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

        def configure(group_read):
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n'
                '[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\nallow_recipe_commands = true\n'
                f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 1\nevent_discovery_seconds = 1\ncomponent_startup_seconds = 1\n'
                f'log_group_read = {json.dumps(group_read)}\n'
                f'[components]\nconfiguration_overlay_dir = {json.dumps(str(overlays))}\n')

        daemon_user = None
        if distinct_uids:
            state_dir.mkdir(mode=0o750)
            for path in [root, *root.rglob("*")]:
                os.chown(path, DAEMON_UID, DAEMON_UID)
            root.chmod(0o750)
            daemon_user = as_user(DAEMON_UID)

        def journal():
            try:
                return json.loads((state_dir / "state.json").read_text())
            except (OSError, ValueError):
                return {}

        component_log = state_dir / "supervised/svc/component.log"
        retained = state_dir / "logs/confidential.json"
        shared = ((component_log, 0o640), (retained, 0o640), (state_dir / "logs/daemon.log", 0o640),
                  (state_dir / "supervised/svc", 0o750), (state_dir / "supervised", 0o750), (state_dir / "logs", 0o750))

        def start_daemon(out):
            return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=out, stderr=out,
                                    preexec_fn=daemon_user)

        # First without log_group_read: svc runs, prints everything and its overlay value is retained.
        configure(False)
        if distinct_uids:
            os.chown(config, DAEMON_UID, DAEMON_UID)
        with (root / "daemon.out").open("w") as out:
            daemon = start_daemon(out)
            wait_for(lambda: journal().get("initialized"), "daemon did not start", daemon)
            sim.add("0x501")
            event = sim.events[-1]["parsedJson"]
            event["component_name"] = "svc"
            sim.recipes[event["recipe_blob_id"]].update(componentName="svc", lifecycle={"run": RUN, "readiness": "test -e ready"})
            wait_for(lambda: journal().get("deployments", {}).get("0x501", {}).get("status") == 1, "svc was not applied", daemon)
            wait_for(lambda: "tick 49" in component_log.read_text(errors="replace"), "svc did not print", daemon)
            assert mode(retained) == 0o600 and mode(component_log) == 0o600, (oct(mode(retained)), oct(mode(component_log)))
            assert OVERLAY_VALUE in retained.read_text(), "the overlay value was retained by the first run"
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=30)
            daemon = None
        retained_before = retained.read_bytes()
        # A rotated daemon log and an inactive component's log, as an earlier run may leave.
        extra = (state_dir / "logs/daemon.log.1", state_dir / "supervised/old/component.log")
        extra[1].parent.mkdir(mode=0o700)
        for path in extra:
            path.write_text("earlier line\n")
            path.chmod(0o600)
            if distinct_uids:
                os.chown(path.parent, DAEMON_UID, DAEMON_UID)
                os.chown(path, DAEMON_UID, DAEMON_UID)
        shared += ((extra[0], 0o640), (extra[1], 0o640), (extra[1].parent, 0o750))

        # Then with it: an existing installation, nothing new to retain.
        configure(True)
        with (root / "daemon.out").open("a") as out:
            daemon = start_daemon(out)
            def shared_modes():
                return {str(path.relative_to(state_dir)): oct(mode(path)) if path.exists() else None for path, _ in shared}
            expected_modes = {str(path.relative_to(state_dir)): oct(expected) for path, expected in shared}
            try:
                wait_for(lambda: shared_modes() == expected_modes, "the existing logs and retained values did not become group-readable", daemon)
            finally:
                print(f"modes after enabling log_group_read: {shared_modes()}")
            assert json.loads(retained.read_bytes()) == json.loads(retained_before), "only the mode changed"
            print("PASS: [runtime] log_group_read turned on for an existing installation: component (active and inactive) and "
                  "daemon logs (with daemon.log.1) and retained values 0640, their directories 0750")

            spool = root / "cw-spool"
            cfg = adapter_config(root / "cw.json", state_dir, spool, emulator.url, ["svc"])
            adapter_user = None
            if distinct_uids:
                # Its own spool, and the daemon's files only through the daemon's group.
                spool.mkdir(mode=0o700)
                for path in (spool, cfg):
                    os.chown(path, ADAPTER_UID, ADAPTER_UID)
                adapter_user = as_user(ADAPTER_UID, groups=[DAEMON_UID])
                denied = subprocess.run(["cat", str(key)], preexec_fn=adapter_user, capture_output=True)
                assert denied.returncode != 0, "the adapter's user must not read the device key"
            adapter = Adapter(binary, cfg, emulator, root / "adapter.out", preexec=adapter_user).start()
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
            if distinct_uids:
                print(f"PASS: daemon UID {DAEMON_UID} and adapter UID {ADAPTER_UID} (supplementary group {DAEMON_UID} only, "
                      "UMask 0077): the adapter read the logs and retained values and was denied the device key")
            print(f"PASS: real daemon: {len(svc)} svc and {len(system)} System events in CloudWatch order under "
                  f"{group('svc')} and stream /{today}/thing/fleet+pi-1; overlay value and api key redacted before the spool; "
                  "DEBUG filtered; a 300,000-byte line split into 262,118 + 37,882 bytes")
            adapter.stop()
            adapter = None
            if distinct_uids:
                broker_part(binary, emulator, root, cfg, component_log, adapter_user)
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=30)
            daemon = None

        # And off again: everything private, without writing the daemon log.
        configure(False)
        logged = (state_dir / "logs/daemon.log").stat().st_size
        with (root / "daemon.out").open("a") as out:
            daemon = start_daemon(out)
            private = {str(path.relative_to(state_dir)): oct(0o600 if path.is_file() else 0o700) for path, _ in shared}
            def private_modes():
                return {str(path.relative_to(state_dir)): oct(mode(path)) for path, _ in shared}
            try:
                wait_for(lambda: private_modes() == private, "the logs and retained values did not become private again", daemon)
            finally:
                print(f"modes after disabling log_group_read: {private_modes()}")
            time.sleep(2)
            assert (state_dir / "logs/daemon.log").stat().st_size == logged, "the daemon log is written although nothing reads it"
            if distinct_uids:
                for path in (component_log, retained, extra[0]):
                    denied = subprocess.run(["cat", str(path)], preexec_fn=adapter_user, capture_output=True)
                    assert denied.returncode != 0, f"the adapter's user can still read {path.name}"
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=30)
            daemon = None
        print("PASS: [runtime] log_group_read turned off again: every existing log (rotated and inactive included) and the "
              "retained values 0600, their directories without group access, and no daemon log written"
              + (f"; the adapter's UID {ADAPTER_UID} is denied them" if distinct_uids else ""))
        assert not sim.errors, sim.errors
    finally:
        if adapter:
            adapter.stop(signal.SIGKILL)
        if daemon and daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        shim.kill()
        server.shutdown()


def broker_part(binary, emulator, root, cfg, component_log, adapter_user):
    """The adapter's credentials from the actual SF-057 credential adapter
    (scripts/aws_credentials.py) through its wrapper (sdk/python/suifleet_aws.py),
    with a short token TTL: once the token expires the endpoint answers 401,
    and the adapter exits, keeping its spool, so a restart through the wrapper
    (Restart=on-failure) obtains a new token and delivers what was pending."""
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import aws_credentials
    from cloudwatch_logs_emulator import ACCESS_KEY, SECRET_KEY, SESSION_TOKEN

    ttl = 4
    private = Path(tempfile.mkdtemp(prefix="sf060-broker-", dir="/run"))
    upstream = None
    service = None
    adapter = None
    try:
        def openssl(*args):
            subprocess.run(["openssl", *map(str, args)], cwd=private, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "1", "-subj", "/CN=fixture-ca")
        for name in ("client", "server"):
            openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}")
            (private / f"{name}.ext").write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=" + ("serverAuth" if name == "server" else "clientAuth") + "\n")
            openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.crt",
                    "-days", "1", "-extfile", f"{name}.ext")
        for file in private.iterdir():
            file.chmod(0o600)
        # Only the adapter's UID may reach the control socket.
        aws_credentials.allow_principals(private, [ADAPTER_UID], "--x")

        class Upstream(BaseHTTPRequestHandler):
            """A stand-in for the AWS IoT credentials provider (mutual TLS), issuing the
            emulator's credentials: valid past the test, but due for refresh (under 5
            minutes), so the adapter fetches them on every upload."""
            def log_message(self, *a):
                pass

            def do_GET(self):
                expiration = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 299))
                body = json.dumps({"credentials": {"accessKeyId": ACCESS_KEY, "secretAccessKey": SECRET_KEY,
                                                   "sessionToken": SESSION_TOKEN, "expiration": expiration}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = HTTPServer(("127.0.0.1", 0), Upstream)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(private / "server.crt", private / "server.key")
        context.load_verify_locations(private / "ca.crt")
        context.verify_mode = ssl.CERT_REQUIRED
        upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        socket_path = private / "credentials.sock"
        service = aws_credentials.Adapter(dict(
            socket=str(socket_path), endpoint=f"https://127.0.0.1:{upstream.server_port}/role-aliases/fixture/credentials",
            ca=str(private / "ca.crt"), certificate=str(private / "client.crt"), key=str(private / "client.key"),
            daemon_uid=DAEMON_UID, components={"cloudwatch-logs": ADAPTER_UID}, grants=["cloudwatch-logs"], token_ttl_seconds=ttl))
        service.start()

        wrapper_path = root / "suifleet_aws.py"
        shutil.copy(ROOT / "sdk/python/suifleet_aws.py", wrapper_path)
        wrapper_path.chmod(0o755)
        wrapper = [sys.executable, wrapper_path, "--socket", socket_path, "--component", "cloudwatch-logs", "--"]
        log_path = root / "adapter-broker.out"

        def start():
            # Credentials only from the wrapper: none from the emulator are inherited.
            return Adapter(binary, cfg, emulator, log_path, preexec=adapter_user, wrapper=wrapper).start()

        def append(line):
            with component_log.open("a") as log:
                log.write(line + "\n")

        adapter = start()
        append("granted line")
        wait_for(lambda: "granted line" in messages(emulator, "svc"), "the line exported with broker credentials did not arrive",
                 adapter.process, timeout=30)
        time.sleep(ttl + 1)
        append("after expiry")
        try:
            adapter.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("the adapter kept running with a refused credential token") from None
        returncode = adapter.process.returncode
        adapter.stop()
        output = log_path.read_text(errors="replace")
        assert returncode not in (0, None) and "refused the authorization token (401)" in output, (returncode, output[-600:])
        print(f"PASS: the actual SF-057 credential adapter answered 401 once the {ttl}-second token expired; the CloudWatch Logs "
              f"adapter exited ({returncode}) for its service manager to restart it")
        # The service manager's Restart=on-failure: a new start through the wrapper.
        adapter = start()
        wait_for(lambda: "after expiry" in messages(emulator, "svc"), "the line pending at the token's expiry did not arrive "
                 "after the restart", adapter.process, timeout=30)
        svc = messages(emulator, "svc")
        assert svc.count("granted line") == 1 and svc.index("granted line") < svc.index("after expiry"), svc[-4:]
        check_clean(emulator)
        adapter.stop()
        adapter = None
        print("PASS: restarted through the SF-057 wrapper with a new token, the adapter delivered the line pending at the "
              "expiry, in order, nothing lost; every request signed with the broker's credentials")
    finally:
        if adapter:
            adapter.stop(signal.SIGKILL)
        if service:
            service.close()
        if upstream:
            upstream.shutdown()
        shutil.rmtree(private, ignore_errors=True)


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
    parser.add_argument("--distinct-uids", action="store_true", help="root only: the daemon and adapter under separate UIDs")
    args = parser.parse_args()
    binary = args.binary.resolve()
    if args.distinct_uids:
        assert os.geteuid() == 0, "run in a disposable root container (make test-cloudwatch-logs-uids)"
    emulator = Emulator()
    emulator.start()
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-cwlogs-") as temporary:
            daemon_part(binary, emulator, Path(temporary) / "daemon", args.distinct_uids)
        if args.distinct_uids:
            return
        emulator.reset()
        with tempfile.TemporaryDirectory(prefix="suifleet-cwlogs-") as temporary:
            file_part(binary, emulator, Path(temporary))
    finally:
        emulator.stop()


if __name__ == "__main__":
    main()
