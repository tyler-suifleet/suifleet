#!/usr/bin/env python3
"""Run a bounded fleet of real edge daemons against a disposable Sui localnet."""

import argparse
import base64
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
from html import escape
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import deployment_manifest as dm
import test_component as component
from test_daemon_localnet import CLOCK, DAEMON, run, wait_for


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def ids(values):
    return "vector[" + ",".join("@" + value for value in values) + "]"


def stop(process, *, crash=False):
    if process.poll() is None:
        process.send_signal(signal.SIGKILL if crash else signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def simulated_install(home, artifact):
    """Record an attempt without executing the artifact or changing the host."""
    payload = artifact.read_bytes()
    fields = component.header(payload)
    with (home / "installations.jsonl").open("a") as history:
        history.write(json.dumps({
            "execution": "simulated", **fields,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }) + "\n")
    if fields["fail"] != "none":
        return 1
    (home / "installed-artifact").write_bytes(payload)
    return 0


class Telemetry:
    """Local sink for cadence evidence. Only summaries leave this fixture."""

    def __init__(self):
        self.lock = threading.Lock()
        self.devices = {}

    def bind(self, device):
        with self.lock:
            self.devices[device.address] = {
                "device": device.object_id, "seq": {},
                "heartbeat_ms": [], "synthetic_log_lines": 0,
            }

    def receive(self, path, data):
        kind = path.rsplit("/", 1)[-1]
        results = []
        with self.lock:
            for envelope in data["messages"]:
                message = json.loads(base64.b64decode(envelope["message"], validate=True))
                device = self.devices.get(message["signer"])
                if not device or device["device"] != message["device_id"]:
                    results.append({"status": "rejected"})
                    continue
                seq = message["seq"]
                if seq <= device["seq"].get(kind, 0):
                    results.append({"status": "duplicate"})
                    continue
                device["seq"][kind] = seq
                if kind == "telemetry":
                    device["heartbeat_ms"].append(message["observed_ms"])
                    device["heartbeat_ms"] = device["heartbeat_ms"][-16:]
                else:
                    device["synthetic_log_lines"] += sum(
                        record["line"] == "virtual-fleet synthetic log"
                        for record in message["records"]
                    )
                results.append({"status": "accepted"})
        return {"results": results}

    def summary(self, device):
        with self.lock:
            state = self.devices[device.address]
            return {"heartbeat_ms": state["heartbeat_ms"][:],
                    "synthetic_log_lines": state["synthetic_log_lines"]}


@dataclass
class Device:
    name: str
    address: str
    home: Path
    key: Path | None = None
    object_id: str = ""
    cap: str = ""
    process: subprocess.Popen | None = None

    def start(self):
        with (self.home / "daemon.log").open("ab") as log:
            self.process = subprocess.Popen(
                [str(DAEMON)], cwd=self.home,
                env={**os.environ, "EDGE_CONFIG": str(self.home / "config.toml")},
                stdout=log, stderr=log,
            )

    def stop(self, *, crash=False):
        if self.process is not None:
            stop(self.process, crash=crash)
            self.process = None

    def journal(self):
        path = self.home / "state" / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def attempts(self):
        path = self.home / "installations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait(self, predicate, description, timeout=90):
        wait_for(predicate, f"{self.name}: {description}", self.process, timeout=timeout)


def scenario(args, root, telemetry, summary, manifest, operator, client, transaction, created, blobs):
    package = manifest["call_package_id"]
    registry = manifest["shared_objects"]["registry"]["id"]
    rpc = manifest["rpc_url"]
    walrus = manifest["walrus"]["aggregator_url"]
    summary.update(chain_identifier=manifest["chain_identifier"], package=package, registry=registry)
    devices = []
    for index in range(args.devices):
        name = f"virtual-{index + 1:03}"
        key = args.workspace / "identities" / name / "device.key"
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True, timeout=10).strip()
        devices.append(Device(name, dm.object_id(address), root / name, key))
    physical = [Device(f"external-{i + 1:03}", addr, root / f"external-{i + 1:03}")
                for i, addr in enumerate(args.physical)]
    if len({d.address for d in devices + physical}) != len(devices + physical):
        raise ValueError("all device signer addresses must be unique")

    def call(module, function, *values):
        return ["--move-call", f"{package}::{module}::{function}", *values]

    def deployment(function, *values, prefix=()):
        tx = transaction(*prefix, *call("deployment_manager", function, *values))
        return created(tx, "::deployment_manager::DeploymentRecord")[0]

    def register(device, group=None):
        device.home.mkdir(mode=0o700)
        client("faucet", "--url", args.faucet, "--address", device.address, json_output=False)
        registered = transaction(
            *call("device_registry", "register_device", f"@{registry}", json.dumps(device.name),
                  f"@{device.address}", '"linux"', CLOCK), "--assign", "device",
            *call("device_cap", "issue_cap", "device"),
            *call("device_registry", "activate_device", f"@{registry}", "device"),
            "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}")
        device.object_id = created(registered, "::device_registry::Device")[0]
        device.cap = created(registered, "::device_cap::DeviceCap")[0]
        owner = created(registered, "::device_registry::DeviceOwnerCap")[0]
        if group:
            transaction(*call("device_registry", "borrow_device", f"@{registry}", f"@{owner}"),
                        "--assign", "loan", *call("device_group", "add_device", f"@{group}", "loan.0"),
                        *call("device_registry", "return_device", f"@{registry}", "loan.0", "loan.1"))

        entry = {"address": device.address, "device": device.object_id, "cap": device.cap,
                 "execution": "simulated-installer" if device.key else "external-operator-managed"}
        summary["devices"][device.name] = entry
        dump(device.home / "deployment.json", manifest)
        dump(device.home / "identity.json", entry)
        if device.key is None:
            return
        telemetry.bind(device)
        command = shlex.join(["python3", str(Path(__file__).resolve()), "_install", str(device.home)])
        (device.home / "config.toml").write_text(
            '[sui]\nmanifest_path = "deployment.json"\n'
            f'[device]\nkeypair_path = {json.dumps(str(device.key))}\n'
            f'device_object_id = "{device.object_id}"\ndevice_cap_id = "{device.cap}"\n'
            f'[apply]\ncommand = {json.dumps(command)}\ntimeout_seconds = 10\n'
            f'[runtime]\nstate_dir = {json.dumps(str(device.home / "state"))}\n'
            f'poll_seconds = {args.poll_seconds}\nevent_discovery_seconds = {args.poll_seconds}\n'
            f'[telemetry]\nurl = "{walrus}/fleet/"\ninterval_seconds = {args.cadence}\n'
            f'[logs]\ninterval_seconds = {args.cadence}\nmax_lines_per_interval = 64\n')
        # Present before the daemon establishes its log cursors. Append fixture
        # lines once the daemon has started its reporting loops.
        source = device.home / "state" / "supervised" / "synthetic-input" / "component.log"
        source.parent.mkdir(parents=True)
        source.touch(mode=0o600)

    def group(name):
        return created(transaction(*call("device_group", "create_group", json.dumps(name),
                                         '"Virtual fleet local fixture"', CLOCK)),
                       "::device_group::DeviceGroup")[0]

    def payload(version, failure="none"):
        artifact = component.build(version, root.name, failure)
        tag = version.replace(".", "-")
        artifact_id, recipe_id = f"artifact-{tag}", f"recipe-{tag}"
        recipe = component.recipe(artifact, artifact_id)
        blobs.update({artifact_id: artifact, recipe_id: recipe})
        return [json.dumps(value) for value in (
            component.DEFAULT_COMPONENT, version, recipe_id,
            hashlib.sha256(artifact).hexdigest(), hashlib.sha256(recipe).hexdigest(),
        )]

    def field(record, name):
        qualified = f"deployment_manager::{name}"
        result = dm.rpc(rpc, "suix_getDynamicFieldObject", [record, {
            "type": f"{manifest['type_origins'][qualified]}::{qualified}",
            "value": {"dummy_field": False},
        }])
        return result.get("data")

    def check_receipts(record, expected, case, *, health=False):
        def read():
            data = field(record, "ReceiptsKey")
            if not data:
                return {}
            contents = data["content"]["fields"]["value"]["fields"]["statuses"]["fields"]["contents"]
            return {dm.object_id(item["fields"]["key"]): int(item["fields"]["value"]) for item in contents}

        wait_for(lambda: read() == expected, f"{record}: expected authoritative receipts {expected}",
                 timeout=args.receipt_timeout)
        entry = {"id": record, "case": case, "statuses": expected}
        for name in (("ReceiptsKey", "RolloutHealthKey") if health else ("ReceiptsKey",)):
            wait_for(lambda: field(record, name), f"{record}: {name} not indexed")
            data = field(record, name)
            if name == "RolloutHealthKey":
                sample = data["content"]["fields"]["value"]["fields"]
                healthy = {dm.object_id(d): ok for d, ok in zip(sample["devices"], sample["healthy"], strict=True)}
                require(healthy == dict.fromkeys(expected, True), f"{record}: unexpected rollout health {healthy}")
            object_id = data["objectId"]
            obj = dm.rpc(rpc, "sui_getObject", [object_id, {"showPreviousTransaction": True}])
            digest = obj["data"]["previousTransaction"]
            tx = dm.rpc(rpc, "sui_getTransactionBlock", [digest, {"showEffects": True}])
            require(tx["effects"]["status"]["status"] == "success", f"{record}: failed {name} effects")
            entry[f"{name}_latest_digest"] = digest
        summary["deployments"].append(entry)
        return entry

    def outcome(device, record, status, *, health=False):
        def settled():
            item = device.journal().get("deployments", {}).get(record, {})
            if not item.get("reported"):
                return False
            require(item["status"] == status, f"{device.name}: {record} settled unexpectedly: {item}")
            return not health or not item.get("rollout_health_pending", True)
        device.wait(settled, f"{record}: report did not settle with status {status}")

    # Each wave gets a group. The first device stays a non-member observer.
    # Later waves are registered sequentially, keeping chain writes bounded.
    for offset in range(0, len(devices), args.concurrent):
        wave = devices[offset:offset + args.concurrent]
        wave_number = offset // args.concurrent + 1
        version = f"1.{wave_number}"
        group_id = group(f"wave-{wave_number}")
        targets = wave[1:] if len(wave) > 1 else wave
        for device in wave:
            register(device, group_id if device in targets else None)
        if offset == 0:
            for device in physical:
                register(device, group_id)
            if physical:
                print(f"External bindings in {root}; forward RPC {rpc} and blobs {walrus}. "
                      "Start the operator-managed daemons when ready.", flush=True)

        with ExitStack() as workers:
            for device in wave:
                device.start()
                workers.callback(device.stop)
                device.wait(lambda: device.journal().get("initialized"), "enrollment cursor missing")
                device.wait(lambda: len(telemetry.summary(device)["heartbeat_ms"]) >= 1,
                            "first local heartbeat missing", timeout=args.cadence + 45)
                with (device.home / "state/supervised/synthetic-input/component.log").open("a") as log:
                    log.write("virtual-fleet synthetic log\n")

            cohort = targets + (physical if offset == 0 else [])
            record = deployment("create_deployment_verified", *payload(version + ".0"),
                                ids([group_id]), ids([]), CLOCK)
            for device in targets:
                outcome(device, record, 1)
            entry = check_receipts(record, {d.object_id: 1 for d in cohort}, "group-update")
            if offset == 0 and physical:
                entry["external_device_receipts"] = [d.object_id for d in physical]
                entry["physical_reboot_evidence"] = "unobserved"
            if len(wave) > 1:
                require(not wave[0].attempts(), "non-member ran a group updater")

            canary = wave[0]
            if args.profile == "faults":
                canary.stop(crash=True)
                failed = deployment("create_deployment_verified", *payload(version + ".1", "install"),
                                    ids([]), ids([canary.object_id]), CLOCK)
                before = len(canary.attempts())
                require(not canary.journal().get("deployments", {}).get(failed), "offline record appeared")
                canary.start()
                outcome(canary, failed, 2)
                check_receipts(failed, {canary.object_id: 2}, "offline-install-failure")
                require(len(canary.attempts()) == before + 1, "offline catch-up attempt count")
                canary.stop(crash=True)
                canary.start()

            members = ids([d.object_id for d in wave])
            cohorts = "vector[" + ",".join("0u64" if i == 0 else "1u64" for i in range(len(wave))) + "]"
            rollout = deployment(
                "share_deployment", "record",
                prefix=[*call("deployment_manager", "rollout_policy", str(len(wave)), str(len(wave)),
                              "1000", "0", "1", "10000", "3600000"), "--assign", "policy",
                        *call("deployment_manager", "new_rollout_verified", *payload(version + ".2"),
                              ids([]), members, cohorts, "policy", CLOCK), "--assign", "record"])
            before = {d.name: len(d.attempts()) for d in wave}
            for device in wave:
                device.wait(lambda: device.journal().get("deferred", {}).get(rollout, {}).get("reason")
                            == "waiting for rollout eligibility", "rollout gate missing")
            require(all(len(d.attempts()) == before[d.name] for d in wave), "applied without grant")

            def advance():
                transaction(*call("deployment_manager", "advance_rollout_onchain",
                                  f"@{rollout}", f"@{registry}", members, CLOCK))

            advance()
            outcome(canary, rollout, 1, health=True)
            require(all(len(d.attempts()) == before[d.name] for d in wave[1:]), "non-canary applied")
            if len(wave) > 1:
                advance()  # Health-gated transition.
                advance()  # Grant the next cohort.
                for device in wave[1:]:
                    outcome(device, rollout, 1, health=True)
            check_receipts(rollout, {d.object_id: 1 for d in wave}, "staged-rollout", health=True)
            summary["sampled_wave_daemon_rss_kib"].append(sum(rss_kib(d.process) for d in wave))
            for device in wave:
                device.wait(lambda: len(telemetry.summary(device)["heartbeat_ms"]) >= 2
                            and telemetry.summary(device)["synthetic_log_lines"] == 1,
                            "heartbeat/log cadence evidence incomplete", timeout=args.cadence * 2 + 45)
                versions = [attempt["version"] for attempt in device.attempts()]
                expected = [version + ".0"] if device in targets else []
                if args.profile == "faults" and device == canary:
                    expected.append(version + ".1")
                expected.append(version + ".2")
                require(versions == expected, f"{device.name}: updater replay or cross-target execution")
                summary["devices"][device.name].update(
                    attempts=device.attempts(), reporting=telemetry.summary(device))
        print(f"Wave {wave_number} passed ({len(wave)} real daemons).", flush=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def free_ports():
    with socket.socket() as rpc, socket.socket() as faucet:
        rpc.bind(("127.0.0.1", 0))
        faucet.bind(("127.0.0.1", 0))
        return rpc.getsockname()[1], faucet.getsockname()[1]


def rss_kib(process):
    for line in Path(f"/proc/{process.pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError(f"no active Linux process RSS for PID {process.pid}")


def plan_for(args):
    waves = (args.devices + args.concurrent - 1) // args.concurrent
    slots = min(args.devices, args.concurrent)
    writes = 2 * (args.devices + len(args.physical)) + 8 * waves
    return {
        "virtual_devices": args.devices, "max_daemons": slots, "waves": waves,
        "external_devices": len(args.physical), "poll_seconds": args.poll_seconds,
        "telemetry_interval_seconds": args.cadence, "profile": args.profile,
        "steady_device_poll_cycles_per_minute_estimate": round(60 * slots / args.poll_seconds, 1),
        "operator_transactions_upper_bound": 1 + writes,
        "operator_local_budget_mist_upper_bound": 1_500_000_000 + 200_000_000 * writes,
        "virtual_device_local_transactions_upper_bound": 4 * args.devices,
        "public_spend_mist": 0,
    }


def write_browser_report(root, summary):
    rows = []
    for name, entry in summary["devices"].items():
        reporting = entry.get("reporting", {})
        cells = (name, entry["execution"], entry["device"], len(entry.get("attempts", [])),
                 len(reporting.get("heartbeat_ms", [])), reporting.get("synthetic_log_lines", ""))
        rows.append("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in cells) + "</tr>")
    (root / "report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Virtual fleet report</title>'
        '<h1>Virtual fleet report</h1>'
        f'<p>Run {escape(summary["result"])} on disposable localnet '
        f'{escape(summary.get("chain_identifier", "unavailable"))}. Public spend 0 MIST.</p>'
        '<p>Physical installation and reboot evidence remain unobserved.</p>'
        '<p><a href="report.json">Receipts, digests, and resource measurements</a></p>'
        '<table><caption>Device outcomes</caption><thead><tr><th>Device</th><th>Execution</th>'
        '<th>Object</th><th>Simulated attempts</th><th>Heartbeats observed</th><th>Synthetic log lines</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></html>\n'
    )


def execute(args):
    require(Path("/.dockerenv").exists() and {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"},
            "run in a disposable Docker container with --network none")
    args.workspace = args.workspace.resolve()
    args.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(args.workspace.stat().st_mode & 0o077 == 0, "workspace must be private (0700)")
    with (args.workspace / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another fleet run already owns this workspace") from error
        root = args.workspace / ("run-" + uuid.uuid4().hex[:12])
        root.mkdir(mode=0o700)
        print(f"Run evidence directory: {root}", flush=True)
        summary = {"result": "incomplete", "network": "disposable-localnet", "plan": plan_for(args),
                   "operator_transactions": [], "devices": {}, "deployments": [],
                   "sampled_wave_daemon_rss_kib": []}
        telemetry = Telemetry()

        def effects(tx):
            gas = tx["effects"]["gasUsed"]
            net = int(gas["computationCost"]) + int(gas["storageCost"]) - int(gas["storageRebate"])
            summary["operator_transactions"].append({"digest": tx["digest"], "local_net_mist": net})

        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="virtual-fleet-node-") as tmp, (root / "node.log").open("wb") as log:
                port, faucet = free_ports()
                rpc = f"http://127.0.0.1:{port}"
                args.faucet = f"http://127.0.0.1:{faucet}/v2/gas"
                node = subprocess.Popen(
                    ["sui", "start", "--force-regenesis",
                     "--fullnode-rpc-port", str(port), f"--with-faucet=127.0.0.1:{faucet}"],
                    env={**os.environ, "SUI_CONFIG_DIR": tmp},
                    stdout=log, stderr=log,
                )
                try:
                    def ready():
                        try:
                            for listener in (port, faucet):
                                with socket.create_connection(("127.0.0.1", listener), timeout=0.5):
                                    pass
                            return True
                        except OSError:
                            return False

                    wait_for(ready, "local node or faucet failed to listen", node)
                    # Reads must succeed before any enrollment or fixture publication.
                    def has_checkpoint():
                        try:
                            chain = dm.chain_facts(rpc)["chain_identifier"]
                        except ValueError:
                            return False
                        require(chain not in {"4c78adac", "35834a8a"}, "public chain rejected")
                        return True

                    wait_for(has_checkpoint, "local node did not index its first checkpoint", node)
                    summary["sampled_node_rss_kib"] = rss_kib(node)
                    run(rpc, args.faucet, scenario=lambda *fixture: scenario(
                        args, root, telemetry, summary, *fixture),
                        receive=telemetry.receive, record_effects=effects)
                    summary["result"] = "passed"
                finally:
                    stop(node)
        finally:
            summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
            summary["operator_local_net_mist"] = sum(tx["local_net_mist"] for tx in summary["operator_transactions"])
            dump(root / "report.json", summary)
            write_browser_report(root, summary)
            print(f"{summary['result']}: {root / 'report.json'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="private directory for keys and local evidence")
    parser.add_argument("--plan", action="store_true", help="print resource estimates without starting a fleet")
    parser.add_argument("--devices", type=int, choices=range(2, 33), metavar="2..32", default=3)
    parser.add_argument("--concurrent", type=int, choices=range(2, 9), metavar="2..8", default=3)
    parser.add_argument("--profile", choices=("healthy", "faults"), default="faults")
    parser.add_argument("--poll-seconds", type=int, choices=range(1, 61), metavar="1..60", default=2)
    parser.add_argument("--cadence", type=int, choices=range(10, 61), metavar="10..60", default=10)
    parser.add_argument("--physical", action="append", type=dm.object_id, default=[], metavar="SIGNER_ADDRESS",
                        help="explicit independent external device signer, up to three")
    parser.add_argument("--receipt-timeout", type=int, choices=range(30, 1801), metavar="30..1800", default=90)
    args = parser.parse_args()
    if len(args.physical) > 3 or len(set(args.physical)) != len(args.physical):
        parser.error("--physical requires up to three distinct signer addresses")
    print(json.dumps(plan_for(args)), flush=True)
    if args.plan:
        return 0
    if args.workspace is None:
        parser.error("--workspace is required for a fleet run")
    signal.signal(signal.SIGTERM, lambda *_: signal.raise_signal(signal.SIGINT))
    try:
        execute(args)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_install":
        sys.exit(simulated_install(Path(sys.argv[2]), Path(sys.argv[3])))
    sys.exit(main())
