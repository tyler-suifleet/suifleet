#!/usr/bin/env python3
"""SF-078 SNS publisher with the real daemon. Run as root in a disposable,
network-isolated container (make test-sns): root only creates real, distinct
component UIDs for SO_PEERCRED. Local chain simulator and the local SNS/
credential emulator from scripts/tests/test_sns_publish.py; no keys, gas,
AWS account, public network or hardware.

  routing         -> sensor request through durable pub/sub to a signed SNS
                     Publish with attributes; status delivered to a watcher
  caller denial   -> an IPC-authorized but ungranted component is reported
                     AccessDenied with no SNS request
  backpressure    -> during an SNS outage the bounded request queue refuses
                     publishers; after recovery every accepted request is
                     published once, in order
  restart         -> publisher stopped before ack, daemon SIGKILLed: the
                     redelivered request is acknowledged, not republished
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_ota import CAP, DEVICE, PACKAGE, REGISTRY, TYPE_ORIGINS, UPGRADE_CAP, Simulator, start_grpc, wait_for

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/tests"))
from test_sns_publish import ALERTS, client, configuration, emulator, request  # noqa: E402
import sns_publish as sp  # noqa: E402

SENSOR, INTRUDER, PUBLISHER, WATCHER = 64001, 64002, 64003, 64004
NAMES = {SENSOR: "sensor", INTRUDER: "intruder", PUBLISHER: "sns-publisher", WATCHER: "watcher"}
QUEUE_LIMIT = 4


def run():
    assert os.geteuid() == 0, "run in a disposable root container (make test-sns)"
    binary = ROOT / "suifleet-edge/target/debug/suifleet"
    sim = Simulator()
    sim.allow_reports, sim.fail_execution, sim.truncate_next_artifact = True, False, False

    class Chain(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                result = dict(jsonrpc="2.0", id=body["id"], result=sim.rpc(body["method"], body["params"]))
            except Exception as error:
                result = dict(jsonrpc="2.0", id=body["id"], error=dict(code=-32000, message=str(error)))
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    chain = ThreadingHTTPServer(("127.0.0.1", 0), Chain)
    threading.Thread(target=chain.serve_forever, daemon=True).start()
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{chain.server_port}")
    with tempfile.TemporaryDirectory(prefix="sf078-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        # Model installed read-only SDK code accessible to the real component UIDs.
        sdk = root / "sdk"
        sdk.mkdir(mode=0o755)
        (sdk / "suifleet_ipc.py").write_bytes((ROOT / "sdk/python/suifleet_ipc.py").read_bytes())
        (sdk / "suifleet_ipc.py").chmod(0o644)
        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        url = f"http://127.0.0.1:{chain.server_port}"
        (root / "deployment.json").write_text(json.dumps(dict(schema_version=1, protocol_version=1, network="localnet",
            chain_identifier=sim.chain, rpc_url=rpc_url, call_package_id=PACKAGE, upgrade_cap_id=UPGRADE_CAP, package_version="3",
            type_origins=TYPE_ORIGINS, shared_objects={"registry": {"id": REGISTRY, "initial_shared_version": "1"}},
            supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
            walrus={"publisher_url": url, "aggregator_url": url})))
        state, socket_path = root / "state", root / "ipc.sock"
        publish_policy = '[[ipc.components.{0}.pubsub_policies]]\noperations = ["aws.greengrass#PublishToTopic"]\nresources = ["{1}"]\n'
        subscribe_policy = '[[ipc.components.{0}.pubsub_policies]]\noperations = ["aws.greengrass#SubscribeToTopic"]\nresources = ["{1}"]\n'
        config = root / "config.toml"
        config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
            f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = "/bin/false"\n'
            f'[runtime]\nstate_dir = "{state}"\npoll_seconds = 1\nevent_discovery_seconds = 1\n'
            f'[ipc]\nsocket = "{socket_path}"\nadministrators = [0]\n'
            f'[ipc.components.sensor]\nuid = {SENSOR}\noperations = ["publish"]\n'
            + publish_policy.format("sensor", "sns/message")
            + f'[ipc.components.intruder]\nuid = {INTRUDER}\noperations = ["publish"]\n'
            + publish_policy.format("intruder", "sns/message")
            + f'[ipc.components.sns-publisher]\nuid = {PUBLISHER}\noperations = ["subscribe", "receive", "ack", "publish"]\n'
            + subscribe_policy.format("sns-publisher", "sns/message")
            + publish_policy.format("sns-publisher", "sns/message/status")
            + f'[ipc.components.watcher]\nuid = {WATCHER}\noperations = ["subscribe", "receive", "ack"]\n'
            + subscribe_policy.format("watcher", "sns/message/status")
            + f'[pubsub]\nmax_messages_per_subscription = {QUEUE_LIMIT}\n')
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

        def ipc(uid, operation, topic="", payload=None):
            code = ("import os,sys,json; uid=int(sys.argv[1]); os.setgid(uid); os.setuid(uid); "
                    "sys.path.insert(0, sys.argv[7]); from suifleet_ipc import Client, IpcError\n"
                    "try: print(json.dumps({'ok': True, 'data': Client(sys.argv[2], component=sys.argv[3]).request(sys.argv[4], topic=sys.argv[5], payload=json.loads(sys.argv[6]))}))\n"
                    "except IpcError as e: print(json.dumps({'ok': False, 'error': str(e)}))")
            out = subprocess.run([sys.executable, "-c", code, str(uid), str(socket_path), NAMES[uid], operation, topic,
                                  json.dumps(payload), str(sdk)], text=True, capture_output=True)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        class Native:
            """The publisher's real peer-UID IPC client."""

            def call(self, operation, topic, payload=None):
                result = ipc(PUBLISHER, operation, topic, payload)
                if not result["ok"]:
                    raise sp.IpcError(result["error"])
                return result["data"]

            def subscribe(self, topic_filter, qos=1):
                return self.call("subscribe", topic_filter, {"qos": qos})

            def receive(self, topic):
                return self.call("receive", topic)

            def ack(self, topic, message_id):
                return self.call("ack", topic, message_id)

            def publish(self, topic, payload):
                return self.call("publish", topic, payload)

        def send(uid, payload):
            return ipc(uid, "publish", "sns/message", payload)

        def statuses():
            out = []
            while (message := ipc(WATCHER, "receive", "sns/message/status")["data"]) is not None:
                assert message["sender"] == "sns-publisher", message
                out.append(message["payload"])
                assert ipc(WATCHER, "ack", "sns/message/status", message["id"])["ok"]
            return out

        try:
            start()
            assert ipc(WATCHER, "subscribe", "sns/message/status", {"qos": 1})["ok"]
            # The publisher cannot read or acknowledge another topic, nor a sensor publish a status.
            assert not ipc(PUBLISHER, "subscribe", "other/#", {"qos": 1})["ok"]
            assert not ipc(SENSOR, "publish", "sns/message/status", {"forged": True})["ok"]
            with emulator() as (sns, endpoint):
                cfg = configuration(root, grants={"sensor": [ALERTS]}, state_dir=str(root / "sns-state"))
                native = Native()
                publisher = sp.Publisher(cfg, native, client(cfg, endpoint))
                assert send(SENSOR, request("native", id="n-1", subject="Door", message_attributes={
                    "kind": dict(data_type="String", string_value="alarm")}))["ok"]
                assert send(INTRUDER, request("forged", id="n-2"))["ok"]
                assert publisher.step() == "published"
                assert publisher.step() == "denied"
                assert publisher.step() == "idle"
                assert [(r["Message"], r["Subject"]) for r in sns["requests"]] == [("native", "Door")]
                assert sns["requests"][0]["MessageAttributes"] == {"kind": {"DataType": "String", "StringValue": "alarm"}}
                assert statuses() == [dict(id="n-1", response=dict(status="success", sns_message_id="msg-1")),
                                      dict(id="n-2", response=dict(status="fail", error="AccessDenied",
                                                                   error_message="sender is not granted this topic"))]
                print("PASS: real daemon durable pub/sub with peer-UID senders -> signed SNS Publish with subject/attributes; "
                      "status delivered to a subscribed watcher; ungranted caller reported AccessDenied without an SNS request")

                # Outage: the bounded durable request queue is the backpressure point.
                sns["responses"] = [(503, "ServiceUnavailable")] * 2
                accepted = [n for n in range(QUEUE_LIMIT + 2) if send(SENSOR, request(f"queued-{n}", id=f"q-{n}"))["ok"]]
                assert accepted == list(range(QUEUE_LIMIT)), accepted
                for _ in range(2):
                    try:
                        publisher.step()
                        raise AssertionError("outage must retain the request")
                    except sp.PublishError as error:
                        assert str(error) == "retry-pending"
                while publisher.step() == "published":
                    pass
                published = [r["Message"] for r in sns["requests"][1:]]
                assert published == ["queued-0", "queued-0", "queued-0"] + [f"queued-{n}" for n in range(1, QUEUE_LIMIT)], published
                assert [d["Message"] for d in sns["deliveries"][1:]] == [f"queued-{n}" for n in range(QUEUE_LIMIT)]
                assert [s["id"] for s in statuses()] == [f"q-{n}" for n in range(QUEUE_LIMIT)]
                assert send(SENSOR, request("after", id="after"))["ok"]
                assert publisher.step() == "published"
                assert statuses()[0]["id"] == "after"
                print(f"PASS: backpressure: during an SNS outage the {QUEUE_LIMIT}-message durable queue refused later publishers; "
                      "after recovery each accepted request was delivered once, in order, and new requests are accepted")

                # Restart: SNS accepted, then the ack is lost and the daemon is SIGKILLed.
                assert send(SENSOR, request("once", id="r-1"))["ok"]
                real_ack = native.ack
                native.ack = lambda *a: (_ for _ in ()).throw(sp.IpcError("truncated response"))
                try:
                    publisher.step()
                    raise AssertionError("a lost ack must retain the request")
                except sp.IpcError:
                    pass
                native.ack = real_ack
                publisher.close()
                stop(kill=True)
                start()
                publisher = sp.Publisher(cfg, native, client(cfg, endpoint))
                try:
                    assert publisher.step() == "duplicate"
                    assert publisher.step() == "idle"
                finally:
                    publisher.close()
                assert [r["Message"] for r in sns["requests"]].count("once") == 1
                # Status is at least once: the first copy, then the redelivery's.
                assert [s["id"] for s in statuses()] == ["r-1", "r-1"]
                print("PASS: restart: request redelivered after a lost ack, publisher restart and daemon SIGKILL "
                      "was acknowledged from the durable ledger without republishing")
        except Exception:
            print((root / "daemon.log").read_text()[-4000:])
            raise
        finally:
            if daemon.get("p") and daemon["p"].poll() is None:
                stop()
            shim.terminate()


if __name__ == "__main__":
    run()
