#!/usr/bin/env python3
"""SF-070 collector -> real daemon durable stream -> signed export.

Run as root in a disposable, network-isolated container (make test-industrial):
root only creates the collector's distinct component UID for SO_PEERCRED. The
built collector zip application runs as that UID against local Modbus and OPC UA
simulators (Basic256Sha256 SignAndEncrypt with a UserName identity) and appends
to a declared File stream through the daemon's IPC socket. A local receiver
verifies every export batch independently (body SHA-256, Sui personal-message
Ed25519 signature, device signer), reusing the SF-034 verifier.

Checks: interoperability through the real component boundary; a full stream
('full') makes the collector skip polls and withhold Publish with a bounded
buffer, then report the gap; kill -9 of the daemon loses no acknowledged or
buffered record; a SIGTERM restart of the collector flushes; every record id of
every collector boot reaches the receiver exactly as appended, in signed batches.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for  # noqa: E402
from test_streams import Receiver  # noqa: E402
from industrial import build, modbus, opcua_codec as ua, opcua_secure as sec, opcua_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_UID = 62101


def certificates(directory):
    for name in ("server", "client"):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out",
                        f"{name}.pem", "-days", "2", "-subj", f"/CN=sf070-{name}", "-addext",
                        f"subjectAltName=URI:urn:suifleet:{name}"], check=True, capture_output=True, cwd=directory)
        subprocess.run(["openssl", "x509", "-in", f"{name}.pem", "-outform", "DER", "-out", f"{name}.der"],
                       check=True, capture_output=True, cwd=directory)


def run(runtime, evidence=None):
    assert os.geteuid() == 0, "run in a disposable root container (make test-industrial)"
    sys.path.insert(0, runtime)
    assert sec.crypto_available(), f"the pinned cryptography runtime is missing from {runtime}"
    passed = []

    def ok(text):
        passed.append(text)
        print(f"PASS: {text}", flush=True)

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
    with tempfile.TemporaryDirectory(prefix="sf070-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        address = subprocess.run([str(binary), "--derive-address", str(key)], text=True, capture_output=True,
                                 check=True).stdout.strip()
        receiver = Receiver(address)
        token = root / "export.token"
        token.write_text("export-secret\n")
        token.chmod(0o600)
        url = f"http://127.0.0.1:{chain.server_port}"
        (root / "deployment.json").write_text(json.dumps(dict(
            schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain, rpc_url=rpc_url,
            call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3", type_origins=TYPE_ORIGINS,
            shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": url, "aggregator_url": url})))
        state, socket_path = root / "state", root / "ipc.sock"
        config = root / "config.toml"
        config.write_text(
            '[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            f'[ipc.components.industrial]\nuid = {COLLECTOR_UID}\n'
            'operations = ["stream_append", "stream_describe", "publish"]\npublish = ["industrial/samples"]\n'
            'streams = ["industrial"]\n'
            '[streams]\nmax_streams = 2\nmax_total_bytes = 1048576\n'
            '[[streams.definitions]]\nname = "industrial"\nmax_size = 49152\nstrategy_on_full = "RejectNewData"\n'
            'discard_exported = true\n'
            f'[streams.definitions.export]\nurl = "http://127.0.0.1:{receiver.server.server_port}/ingest"\n'
            f'bearer_token_file = "{token}"\nbatch_max_messages = 20\nbatch_interval_seconds = 1\nmax_backoff_seconds = 1\n')

        # Simulators: Modbus TCP and OPC UA Basic256Sha256 SignAndEncrypt with a UserName identity.
        certs = root / "certs"
        certs.mkdir()
        certificates(certs)
        model = modbus.DataModel(unit=1)
        model.set_value("holding_register", 0, "float32", 21.5)
        mb = modbus.Simulator(model)
        opc = opcua_server.Simulator(endpoints=[("Basic256Sha256", "SignAndEncrypt")],
                                     certificate=(certs / "server.der").read_bytes(),
                                     private_key=sec.load_private_key(str(certs / "server.key")),
                                     trusted_certificates=[(certs / "client.der").read_bytes()],
                                     users={"collector": "s3cret-pw"}, allow_anonymous=False)
        opc.add_variable("ns=2;s=Counter", ua.INT32, 0)
        opc.add_variable("ns=2;s=Temperature", ua.DOUBLE, 21.5)
        ticking = threading.Event()

        def ticker():
            while not ticking.wait(0.05):
                model.tick()
                opc.tick()
        threading.Thread(target=ticker, daemon=True).start()

        # The collector's private files belong to its UID, mode 0600; it can read nothing else of ours.
        home = root / "collector"
        home.mkdir(mode=0o700)
        bundle = home / "suifleet-industrial.pyz"
        digest = build.build(bundle)
        for name in ("client.der", "client.key", "server.der"):
            shutil.copy(certs / name, home / name)
        (home / "password").write_text("s3cret-pw\n")
        collector_config = home / "collector.json"
        collector_config.write_text(json.dumps({
            "version": 1, "collector_id": "line1",
            "output": {"socket": str(socket_path), "component": "industrial", "stream": "industrial",
                       "topic": "industrial/samples", "max_buffered_records": 400, "max_records_per_message": 25,
                       "retry_min_ms": 100, "retry_max_ms": 500, "ipc_timeout_ms": 5000},
            "sources": [
                {"name": "plc1", "protocol": "modbus", "transport": "tcp", "host": "127.0.0.1", "port": mb.port,
                 "unit_id": 1, "poll_interval_ms": 100, "reconnect_min_ms": 100, "reconnect_max_ms": 500, "points": [
                     {"name": "temperature", "table": "holding_register", "address": 0, "type": "float32"},
                     {"name": "ticks", "table": "input_register", "address": 0, "type": "uint16"},
                     {"name": "toggle", "table": "coil", "address": 0, "type": "bool"}]},
                {"name": "opc1", "protocol": "opcua", "endpoint_url": opc.url, "security_policy": "Basic256Sha256",
                 "security_mode": "SignAndEncrypt", "certificate_file": str(home / "client.der"),
                 "private_key_file": str(home / "client.key"), "server_certificate_file": str(home / "server.der"),
                 "identity": {"type": "username", "username": "collector", "password_file": str(home / "password")},
                 "publishing_interval_ms": 100, "keepalive_count": 5, "lifetime_count": 600,
                 "max_notifications_per_publish": 50, "reconnect_min_ms": 100, "reconnect_max_ms": 500,
                 "points": [{"name": "counter", "node_id": "ns=2;s=Counter", "type": "int32", "sampling_interval_ms": 50,
                             "queue_size": 20},
                            {"name": "temperature", "node_id": "ns=2;s=Temperature", "mode": "poll",
                             "poll_interval_ms": 200, "type": "double"}]}]}))
        for path in [home, *home.iterdir()]:
            os.chown(path, COLLECTOR_UID, COLLECTOR_UID)
            if path.is_file():
                path.chmod(0o600)
        status_file = home / "status.json"
        runtime_dir = Path(runtime)
        log = (root / "daemon.log").open("a")
        collector_log = (root / "collector.log").open("a")
        procs = {}

        def start_daemon():
            procs["daemon"] = subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)},
                                               stdout=log, stderr=log)
            wait_for(lambda: socket_path.exists(), "IPC socket did not appear", procs["daemon"])

        def stop_daemon(kill=False):
            (procs["daemon"].kill if kill else procs["daemon"].terminate)()
            procs["daemon"].wait(timeout=30)
            if socket_path.exists():
                socket_path.unlink()

        def demote():
            os.setgroups([])
            os.setgid(COLLECTOR_UID)
            os.setuid(COLLECTOR_UID)

        def collector_status():
            try:
                return json.loads(status_file.read_text())
            except (OSError, ValueError):
                return {}

        def admin(operation, topic="", payload=None):
            sys.path.insert(0, str(ROOT / "sdk/python"))
            from suifleet_ipc import Client
            return Client(str(socket_path)).request(operation, topic=topic, payload=payload)

        def exported_records():
            out = {}
            for message in receiver.received("industrial").values():
                for record in message["records"]:
                    out[record["id"]] = record
            return out

        try:
            # `-I` ignores PYTHONPATH: a two-line launcher puts the collector's private copy of
            # the pinned cryptography runtime and the bundle on sys.path, as an operator package would.
            private_runtime = home / "runtime"
            shutil.copytree(runtime_dir, private_runtime)
            for path in [private_runtime, *private_runtime.rglob("*")]:
                os.chown(path, COLLECTOR_UID, COLLECTOR_UID)
            launcher = home / "run.py"
            launcher.write_text("import runpy, sys\nsys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
                                "sys.argv = [sys.argv[2]] + sys.argv[3:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
            os.chown(launcher, COLLECTOR_UID, COLLECTOR_UID)
            launcher.chmod(0o600)

            def start_collector():
                procs["collector"] = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(launcher), str(private_runtime), str(bundle), "--config",
                     str(collector_config), "--status-file", str(status_file)],
                    env={"PATH": "/usr/bin:/bin"}, stdout=collector_log, stderr=collector_log, preexec_fn=demote,
                    cwd=str(home))

            receiver.mode = "ok"
            start_daemon()
            start_collector()
            wait_for(lambda: {r["source"] for r in exported_records().values() if r["kind"] == "sample"
                              and r["quality"] == "GOOD"} == {"plc1", "opc1"},
                     "no collector data from both sources was exported", procs["collector"])
            records = exported_records()
            sample = next(r for r in records.values() if r.get("point") == "temperature" and r["source"] == "plc1")
            assert (sample["type"], sample["value"], sample["quality"]) == ("float32", 21.5, "GOOD"), sample
            secure = [r for r in records.values() if r["source"] == "opc1" and r["kind"] == "sample"]
            assert secure and all(r["type"] in ("int32", "double") and r["source_timestamp_ns"] for r in secure), secure[:3]
            ok(f"interoperability through the component boundary: zip application {digest[:16]} as UID {COLLECTOR_UID}, "
               "Modbus TCP and OPC UA Basic256Sha256/SignAndEncrypt + UserName records exported in signed batches")

            # Backpressure: with the receiver down the 48 KiB RejectNewData stream fills.
            receiver.mode = "down"
            wait_for(lambda: collector_status().get("writer", {}).get("last_append_error") == "full",
                     "the stream never reported full", procs["collector"])
            wait_for(lambda: collector_status()["sources"]["plc1"]["pending_skipped_cycles"] > 0
                     and collector_status()["sources"]["opc1"].get("publish_pauses", 0) >= 1,
                     "the full buffer did not pause polling and Publish", procs["collector"])
            status = collector_status()
            assert status["buffered"] <= status["capacity"] == 400, status
            plc, opc_state = status["sources"]["plc1"], status["sources"]["opc1"]
            assert plc["pending_skipped_cycles"] > 0, plc
            assert opc_state.get("publish_pauses", 0) >= 1, opc_state
            assert "s3cret" not in json.dumps(status) and "s3cret" not in (root / "collector.log").read_text()
            receiver.mode = "ok"
            wait_for(lambda: any(r["kind"] == "gap" and r["reason"] == "backpressure" for r in exported_records().values()),
                     "no backpressure gap record after the stream drained", procs["collector"])
            ok(f"backpressure: the daemon refused appends with 'full'; buffer held {status['buffered']}/400, "
               "Modbus polls skipped and OPC UA Publish withheld, then gap records reported the interval")

            # Daemon crash: records keep arriving into the collector buffer, then flow after restart.
            before = len(exported_records())
            stop_daemon(kill=True)
            time.sleep(2)
            failures = collector_status()["writer"]["append_failures"]
            start_daemon()
            wait_for(lambda: len(exported_records()) > before + 20, "export did not resume after the daemon crash")
            assert collector_status()["writer"]["append_failures"] >= failures
            ok("daemon kill -9: the collector buffered and retried; appends and signed export resumed after restart")

            # Collector restart: SIGTERM flushes the buffer before exit.
            procs["collector"].send_signal(signal.SIGTERM)
            assert procs["collector"].wait(timeout=30) == 0, "collector did not flush and exit cleanly"
            boots_before = {i.split(":")[1] for i in exported_records()}
            start_collector()
            wait_for(lambda: len({i.split(":")[1] for i in exported_records()}) > len(boots_before),
                     "no records from the restarted collector", procs["collector"])
            time.sleep(1)
            procs["collector"].send_signal(signal.SIGTERM)
            assert procs["collector"].wait(timeout=30) == 0
            wait_for(lambda: admin("stream_describe", "industrial")["exported"] == admin("stream_describe", "industrial")["newest"],
                     "final export did not catch up")
            ok("collector SIGTERM: buffered records flushed (exit 0) and a new boot continued the stream")

            # No loss: per boot, record counters are contiguous from 1 (duplicates possible after an IPC timeout).
            records = exported_records()
            by_boot = {}
            for record_id in records:
                _, boot, number = record_id.split(":")
                by_boot.setdefault(boot, set()).add(int(number))
            for boot, numbers in by_boot.items():
                assert numbers == set(range(1, max(numbers) + 1)), (boot, sorted(set(range(1, max(numbers) + 1)) - numbers)[:10])
            batches = [b for b in receiver.batches if b["stream"] == "industrial"]
            # SF-034 counts the sequence numbers a kill -9 skips (block reservation) as `lost_before`
            # once discard_exported has retired everything before them. Those numbers never held a
            # message: accept lost_before only as exactly that gap, at a 1024 block boundary.
            # The contiguous record ids above are the actual no-loss evidence.
            previous, crash_gaps = None, 0
            for batch in batches:
                manifest = json.loads(batch["manifest"])
                if batch["lost"]:
                    assert manifest["first_sequence"] % 1024 == 0 and previous is not None, manifest
                    assert batch["lost"] == manifest["first_sequence"] - previous - 1, (batch["lost"], manifest, previous)
                    crash_gaps += 1
                previous = manifest["last_sequence"]
            assert crash_gaps <= 1, "only the one daemon crash skips sequence numbers"
            ok(f"no loss: {sum(len(n) for n in by_boot.values())} records from {len(by_boot)} collector boots arrived "
               f"contiguously in {len(batches)} verified signed batches (stream lost_before only for the crash numbering gap)")
            if evidence:
                head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
                Path(evidence).write_text(json.dumps({
                    "ticket": "SF-070", "head": head or None, "checks": passed, "bundle_sha256": digest,
                    "signature_verification": "independent: SHA-256 body binding, BLAKE2b personal-message intent digest, "
                                              "RFC 8032 Ed25519 (Python), signer address (scripts/test_streams.py Receiver)"},
                    indent=2) + "\n")
        except Exception:
            print((root / "daemon.log").read_text()[-3000:])
            print((root / "collector.log").read_text()[-3000:])
            raise
        finally:
            ticking.set()
            for name in ("collector", "daemon"):
                process = procs.get(name)
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=30)
            mb.close()
            opc.close()
            shim.terminate()


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
