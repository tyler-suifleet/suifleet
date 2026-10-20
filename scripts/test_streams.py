#!/usr/bin/env python3
"""SF-034 durable streams and export with the real daemon. Run as root in a
disposable, network-isolated container (make test-streams): root only creates
real, distinct component UIDs for SO_PEERCRED. Local chain simulator and a
local HTTP export receiver; no keys, gas, public network or hardware.

The receiver verifies every batch independently of the daemon: the body's
SHA-256 against the manifest, the manifest's Sui personal-message signature
(BLAKE2b intent digest + RFC 8032 Ed25519, below) and that its signer is the
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

import test_ota
from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

ROOT = Path(__file__).resolve().parents[1]
PRODUCER, OTHER = 62001, 62002

# --- RFC 8032 Ed25519 verification (reference arithmetic; test use only) ------
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * I % P
    if (x * x - x2) % P or (x == 0 and sign):
        return None
    return P - x if x & 1 != sign else x


def _decode(point):
    y = int.from_bytes(point, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = None if y >= P else _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    C, Dd = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _mul(s, point):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, point)
        point, s = _add(point, point), s >> 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


G = (15112221349535400772501151409588531511454012693041857206046113283949847762202,
     46316835694926478169428394003475163141307993866256225615783033603165251855960, 1, 0)
G = (G[0], G[1], 1, G[0] * G[1] % P)


def ed25519_verify(public, message, signature):
    a, r = _decode(public), _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % L
    return _equal(_mul(s, G), _add(r, _mul(h, a)))


def uleb(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def verify_personal_message(message, serialized):
    """Sui personal-message signature: flag 0 (Ed25519) | sig | pubkey."""
    assert serialized[0] == 0 and len(serialized) == 97, "Ed25519 serialized signature"
    signature, public = serialized[1:65], serialized[65:]
    digest = hashlib.blake2b(bytes([3, 0, 0]) + uleb(len(message)) + message, digest_size=32).digest()
    assert ed25519_verify(public, digest, signature), "bad signature"
    return "0x" + hashlib.blake2b(b"\x00" + public, digest_size=32).hexdigest()


class Receiver:
    """Export destination. Modes: ok, down (503), ack-lost (stores, answers 500)."""

    def __init__(self, device_address):
        self.mode, self.device = "down", device_address
        self.batches, self.lock = [], threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if receiver.mode == "down":
                    return self.reply(503)
                manifest_bytes = base64.b64decode(self.headers["x-suifleet-manifest"])
                signer = verify_personal_message(manifest_bytes, base64.b64decode(self.headers["x-suifleet-signature"]))
                manifest = json.loads(manifest_bytes)
                assert signer == manifest["signer"] == receiver.device, (signer, manifest["signer"])
                assert hashlib.sha256(body).hexdigest() == manifest["body_sha256"]
                assert self.headers["authorization"] == "Bearer export-secret"
                batch = json.loads(body)
                sequences = [m["sequence"] for m in batch["messages"]]
                assert (sequences[0], sequences[-1], len(sequences)) == (manifest["first_sequence"], manifest["last_sequence"], manifest["messages"])
                with receiver.lock:
                    receiver.batches.append({"key": self.headers["idempotency-key"], "stream": batch["stream"], "lost": batch["lost_before"],
                                             "manifest": manifest_bytes, "signature": base64.b64decode(self.headers["x-suifleet-signature"]),
                                             "messages": [(m["sequence"], json.loads(base64.b64decode(m["data"]))) for m in batch["messages"]]})
                self.reply(500 if receiver.mode == "ack-lost" else 200)

            def reply(self, status):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def received(self, stream):
        """Deduplicated by sequence, as receivers must."""
        with self.lock:
            out = {}
            for b in self.batches:
                if b["stream"] == stream:
                    out.update(dict(b["messages"]))
            return out


def run(evidence=None, firehose=False):
    assert os.geteuid() == 0, "run in a disposable root container (make test-streams)"
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
    with tempfile.TemporaryDirectory(prefix="sf034-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        # Dispatched checkouts can be owner-only. Model installed read-only SDK
        # code in this disposable fixture, accessible to the real component UIDs.
        sdk = root / "sdk"
        sdk.mkdir(mode=0o755)
        (sdk / "suifleet_ipc.py").write_bytes((ROOT / "sdk/python/suifleet_ipc.py").read_bytes())
        (sdk / "suifleet_ipc.py").chmod(0o644)
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        address = subprocess.run([str(binary), "--derive-address", str(key)], text=True, capture_output=True, check=True).stdout.strip()
        receiver = Receiver(address)
        token = root / "export.token"
        token.write_text("export-secret\n")
        token.chmod(0o600)
        url = f"http://127.0.0.1:{chain.server_port}"
        export_url = f"http://127.0.0.1:{receiver.server.server_port}/ingest"
        (root / "deployment.json").write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
            chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
            type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": url, "aggregator_url": url})))
        state, socket_path = root / "state", root / "ipc.sock"
        export = (f'url = "{export_url}"\nbearer_token_file = "{token}"\nbatch_max_messages = 10\n'
                  'batch_interval_seconds = 1\nmax_backoff_seconds = 2\n')
        config = root / "config.toml"
        config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            f'[ipc.components.producer]\nuid = {PRODUCER}\n'
            'operations = ["stream_create", "stream_delete", "stream_append", "stream_read", "stream_describe", "stream_list"]\n'
            'streams = ["telemetry", "ring", "local-*"]\n'
            f'[ipc.components.other]\nuid = {OTHER}\noperations = ["stream_append", "stream_read"]\nstreams = ["other-*"]\n'
            '[streams]\nmax_streams = 4\nmax_total_bytes = 262144\n'
            '[[streams.definitions]]\nname = "telemetry"\nmax_size = 65536\nstrategy_on_full = "RejectNewData"\n'
            f'[streams.definitions.export]\n{export}'
            '[[streams.definitions]]\nname = "ring"\nmax_size = 4096\nstrategy_on_full = "OverwriteOldestData"\n'
            f'[streams.definitions.export]\n{export}')
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

        def ipc(uid, operation, topic="", payload=None, component=None):
            component = component or {PRODUCER: "producer", OTHER: "other", 0: ""}[uid]
            code = ("import os,sys,json; uid=int(sys.argv[1]); "
                    "uid and (os.setgid(uid), os.setuid(uid)); "
                    "sys.path.insert(0, sys.argv[7]); from suifleet_ipc import Client, IpcError\n"
                    "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                    "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
            out = subprocess.run([sys.executable, "-c", code, str(uid), str(socket_path), component, operation, topic, json.dumps(payload),
                                  str(sdk)], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        def describe(stream):
            return ipc(0, "stream_describe", stream)["data"]

        try:
            start()
            # ACLs, declared streams, component-created streams and quota.
            assert ipc(OTHER, "stream_append", "telemetry", {"x": 1}) == {"ok": False, "error": "request_failed"}
            assert ipc(PRODUCER, "stream_create", "local-a", {"max_size": 65536, "strategy_on_full": "RejectNewData"})["ok"]
            assert ipc(PRODUCER, "stream_create", "local-b", {"max_size": 4096, "strategy_on_full": "RejectNewData",
                       "export": {"url": "http://198.51.100.1/x"}})["error"] == "invalid", "components cannot declare exports"
            assert ipc(PRODUCER, "stream_create", "local-c", {"max_size": 200000, "strategy_on_full": "RejectNewData"})["error"] == "quota"
            assert ipc(PRODUCER, "stream_create", "other-x", {"max_size": 4096, "strategy_on_full": "RejectNewData"})["error"] == "request_failed"
            assert ipc(PRODUCER, "stream_delete", "telemetry")["error"] == "declared"
            assert ipc(OTHER, "stream_read", "other-x", {}) == {"ok": False, "error": "not-found"}
            assert ipc(PRODUCER, "stream_list")["data"]["streams"] == ["local-a", "ring", "telemetry"]
            print("PASS: ACLs per component, exports and declared streams operator-only, quota enforced")
            if firehose:
                sys.path.insert(0, str(ROOT / 'scripts/tests'))
                from test_firehose_export import exercise_native
                assert ipc(PRODUCER, "stream_create", "local-firehose", {"max_size": 4096, "strategy_on_full": "RejectNewData"})["ok"]
                for n in range(3):
                    assert ipc(PRODUCER, "stream_append", "local-firehose", {"native": n})["ok"]

                class NativeSource:
                    def stream_describe(self, name):
                        result = ipc(PRODUCER, "stream_describe", name)
                        assert result["ok"], result
                        return result["data"]

                    def stream_read(self, name, start):
                        from suifleet_ipc import IpcError
                        result = ipc(PRODUCER, "stream_read", name, {"start": start})
                        if not result["ok"]:
                            raise IpcError(result["error"])
                        return result["data"]["messages"]

                def restart_firehose():
                    stop(kill=True)
                    start()
                    result = ipc(PRODUCER, "stream_append", "local-firehose", {"native": 3})
                    assert result["ok"] and result["data"]["sequence"] >= 1024, result

                exercise_native(NativeSource(), root, restart_firehose)
                assert ipc(PRODUCER, "stream_delete", "local-firehose")["ok"]
                print("PASS: Firehose actual daemon durable stream / peer-UID IPC to signed SDK HTTP request; exporter checkpoint survives reopen and daemon SIGKILL reservation gap")


            # Offline buffering: the destination is down; messages wait on disk.
            for i in range(25):
                assert ipc(PRODUCER, "stream_append", "telemetry", {"i": i})["data"]["sequence"] == i
            time.sleep(3)
            d = describe("telemetry")
            assert d["exported"] == -1 and d["export"]["status"]["failures"] >= 1, d
            assert "secret" not in json.dumps(d)
            receiver.mode = "ok"
            wait_for(lambda: describe("telemetry")["exported"] == 24, "buffered messages were not exported")
            got = receiver.received("telemetry")
            assert got == {i: {"i": i} for i in range(25)}, got
            assert len([b for b in receiver.batches if b["stream"] == "telemetry"]) >= 3, "batches bounded at 10 messages"
            first = receiver.batches[0]
            for forged in (first["manifest"].replace(b'"messages":10', b'"messages":9'), first["manifest"] + b" "):
                try:
                    verify_personal_message(forged, first["signature"])
                    raise AssertionError("the verifier accepted a forged manifest")
                except AssertionError as error:
                    assert str(error) == "bad signature", error
            print(f"PASS: offline buffering: 25 messages exported in order in {len(receiver.batches)} signed batches after the destination returned; forged manifests fail verification")

            # Interrupted export: the destination stores a batch but the answer is lost.
            receiver.mode = "ack-lost"
            for i in range(25, 30):
                ipc(PRODUCER, "stream_append", "telemetry", {"i": i})
            wait_for(lambda: len({b["key"] for b in receiver.batches if b["key"].endswith(":25-29")}) == 1
                     and sum(b["key"].endswith(":25-29") for b in receiver.batches) >= 2, "the unacknowledged batch was not retried")
            assert describe("telemetry")["exported"] == 24, "checkpoint advanced without a 2xx"
            receiver.mode = "ok"
            wait_for(lambda: describe("telemetry")["exported"] == 29, "retried batch not checkpointed")
            assert receiver.received("telemetry") == {i: {"i": i} for i in range(30)}
            print("PASS: interrupted export: same range and Idempotency-Key retried; checkpoint only after a 2xx; no loss")

            # Crash: kill -9 with unexported messages, plus a torn tail.
            receiver.mode = "down"
            for i in range(30, 35):
                ipc(PRODUCER, "stream_append", "telemetry", {"i": i})
            stop(kill=True)
            segment = max((state / "streams" / hashlib.sha256(b"telemetry").hexdigest()).glob("*.seg"))
            with segment.open("ab") as f:
                f.write(b"\x09\x00\x00\x00torn")
            start()
            d = describe("telemetry")
            assert (d["newest"], d["exported"], d["messages"]) == (34, 29, 35), d
            after = ipc(PRODUCER, "stream_append", "telemetry", {"i": "after-crash"})["data"]["sequence"]
            assert after > 34, "sequence numbers are never reused after a crash"
            receiver.mode = "ok"
            wait_for(lambda: describe("telemetry")["exported"] == after, "export did not resume after the crash")
            got = receiver.received("telemetry")
            assert {k: v for k, v in got.items() if k < 35} == {i: {"i": i} for i in range(35)} and got[after] == {"i": "after-crash"}
            print(f"PASS: crash recovery: kill -9 and a torn tail; 35 messages intact, numbering resumed at {after} (no reuse), export resumed")

            # Clean restart: numbering is exact.
            stop()
            start()
            assert ipc(PRODUCER, "stream_append", "telemetry", {"i": "clean"})["data"]["sequence"] == after + 1
            print("PASS: clean restart: numbering continues exactly")

            # Full RejectNewData stream refuses with a protocol error code.
            receiver.mode = "down"
            accepted = 0
            while True:
                r = ipc(PRODUCER, "stream_append", "local-a", {"pad": "x" * 4000})
                if not r["ok"]:
                    assert r["error"] == "full", r
                    break
                accepted += 1
            assert accepted == 65536 // len(json.dumps({"pad": "x" * 4000}, separators=(",", ":"))), accepted
            print(f"PASS: quota exhaustion: RejectNewData stream refused message {accepted + 1} with 'full'")

            # Overwrite retention while offline: loss is reported, never hidden.
            for i in range(100):
                ipc(PRODUCER, "stream_append", "ring", {"r": i, "pad": "y" * 90})
            d = describe("ring")
            assert d["oldest"] > 0 and d["bytes"] <= 4096, d
            evicted = ipc(PRODUCER, "stream_read", "ring", {"start": 0})
            assert evicted == {"ok": False, "error": "evicted"} or evicted.get("error") == "evicted", evicted
            receiver.mode = "ok"
            wait_for(lambda: describe("ring")["exported"] == 99, "ring not exported")
            ring = [b for b in receiver.batches if b["stream"] == "ring"]
            assert ring[0]["lost"] == d["oldest"], (ring[0]["lost"], d["oldest"])
            assert sorted(receiver.received("ring")) == list(range(d["oldest"], 100))
            print(f"PASS: retention: OverwriteOldestData kept {100 - d['oldest']} of 100 while offline; reads report evicted; "
                  f"the export reported {ring[0]['lost']} retired messages")
            # A lowered quota below the stored inventory refuses startup, data kept.
            stop()
            full = config.read_text()
            config.write_text(full.replace("max_total_bytes = 262144", "max_total_bytes = 131072"))
            refused = subprocess.run([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, capture_output=True, text=True, timeout=60)
            assert refused.returncode != 0, "daemon started over the lowered stream quota"
            assert "need 3 streams and 135168 bytes" in refused.stdout + refused.stderr, refused.stderr[-2000:]
            if socket_path.exists():
                socket_path.unlink()
            config.write_text(full)
            start()
            kept = ipc(PRODUCER, "stream_read", "local-a", {"start": 0, "max": accepted})["data"]["messages"]
            assert len(kept) == accepted, len(kept)
            print(f"PASS: lowered quota: startup refused for 3 stored streams over max_total_bytes; with the old limits all {accepted} local-a messages remain")
            if evidence:
                head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
                Path(evidence).write_text(json.dumps({"ticket": "SF-034", "head": head or None, "checks": passed,
                    "fixture": {"file": "interfaces/compat/stream-cases.json", "sequences": 5, "steps": 46,
                                "executed_by": "make edge-test: streams::tests::shared_stream_cases"},
                    "signature_verification": "independent: SHA-256 body binding, BLAKE2b personal-message intent digest, RFC 8032 Ed25519 (Python), signer address"},
                    indent=2) + "\n")
        except Exception:
            print((root / "daemon.log").read_text()[-4000:])
            raise
        finally:
            if daemon.get("p") and daemon["p"].poll() is None:
                stop()
            shim.terminate()


if __name__ == "__main__":
    run(None if "--firehose" in sys.argv else (sys.argv[1] if len(sys.argv) > 1 else None), firehose="--firehose" in sys.argv)
