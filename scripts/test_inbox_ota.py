#!/usr/bin/env python3
"""Device inboxes and group heads (SF-125): the real daemon against the
chain/Walrus simulator of scripts/test_ota.py (through the gRPC shim), without
keys or gas.

Covers the enrollment baseline for announced records, a device-targeted
deployment and an indexed-group deployment that exist only in objects (no
events), a group joined while the daemon was stopped, a scheduled start that runs on time although the poll interval is a
minute, a device that was offline longer than the node's event history and
resynchronizes from objects alone, and the bounded event scan that still
applies an unannounced deployment from an older client. A journal written
before inboxes (no heads) resumes from its event cursor's boundary, so
records created while it was offline still apply, with history retained
or pruned, including after an interruption once the cursor has moved.
Real-chain behaviour is covered by scripts/test_inbox_localnet.py.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import test_ota
from deployment_manifest import INBOX_TYPES, object_id
from test_ota import CAP, DEVICE, GROUP, OPERATOR, PACKAGE, REGISTRY, Simulator, start_grpc, wait_for
from test_desired_state import manifest, serve

for _name in INBOX_TYPES:
    test_ota.TYPE_ORIGINS[_name] = PACKAGE  # Introduced by the SF-125 upgrade.
GROUP2 = "0x11"
INBOX_KEY = f"0x2::derived_object::DerivedObjectKey<{PACKAGE}::fleet_inbox::InboxKey>"


class InboxSimulator(Simulator):
    def __init__(self):
        super().__init__()
        self.allow_reports = True
        self.fail_execution = False
        self.truncate_next_artifact = False
        self.now_ms = 1_000_000
        self.lowest = 0
        self.unindexed = set()  # Deployments whose events the node does not serve.
        self.inbox = None
        self.fail_inbox = False
        self.heads = {}
        self.members = {}  # group -> indexed members (MemberKey fields)
        self.event_ms = []  # Chain time of event i, in checkpoint i + 1.
        self.groups[GROUP] = {"admin": OPERATOR, "device_ids": []}

    def add(self, *args, **kwargs):
        super().add(*args, **kwargs)
        self.event_ms.append(self.now_ms)

    def head(self):
        return {"seq": "0", "latest": None, "next_start_ms": "0", "components": {"contents": []}}

    def announce(self, head, id, start_ms=0):
        head["seq"] = str(int(head["seq"]) + 1)
        head["latest"] = id
        name = next(e["parsedJson"]["component_name"] for e in self.events if e["parsedJson"]["deployment_id"] == id)
        head["components"]["contents"] = [c for c in head["components"]["contents"] if c["key"] != name] + [
            {"key": name, "value": id}]
        if start_ms > self.now_ms:
            head["next_start_ms"] = str(start_ms)

    def deploy(self, id, *, groups=None, direct=True, created_ms=None, announce=True, start_ms=0):
        self.add(id, groups=groups, direct=direct)
        self.schema_fields[id] = {"created_at": str(self.now_ms if created_ms is None else created_ms)}
        if start_ms:
            self.controls[id] = {"revision": 1, "state": 0, "not_before_ms": start_ms, "expires_ms": 0,
                                 "window_start_ms": 0, "window_end_ms": 0, "notice_ms": 0}
        if announce:
            self.unindexed.add(id)
            for group in groups or []:
                self.announce(self.heads.setdefault(group, self.head()), id, start_ms)
            if direct:
                self.announce(self.inbox["head"], id, start_ms)

    def rpc(self, method, params):
        key = lambda name: f"{test_ota.TYPE_ORIGINS[name]}::{name}"
        if method == "shim_height":
            return {"height": len(self.events), "lowest": self.lowest, "timestamp_ms": self.now_ms, "checkpoints": True}
        if method == "shim_checkpoint":
            # Summaries outlive pruned events, as on a public node.
            n = params[0]
            return {"timestamp_ms": 0 if n == 0 else self.event_ms[n - 1]} if n <= len(self.events) else None
        if method == "shim_events" and params[0].endswith("::deployment_manager::DeploymentCreated"):
            # Unannounced-only records keep their ledger position but read as
            # another tenant's event, so only objects can reveal them.
            foreign = dict(deployment_id="0xf0", component_name="other", version="1", recipe_blob_id="x",
                           artifact_sha256="0" * 64, target_groups=[], target_devices=[], creator="0xbad")
            return [foreign if e["parsedJson"]["deployment_id"] in self.unindexed else e["parsedJson"]
                    for e in self.events]
        if method == "shim_discovery_events":
            # The combined SF-122 stream: event i is still in checkpoint
            # i + 1, and unannounced-only records stay masked.
            assert not self.memberships, "inbox scenarios list groups only in the inbox"
            kind = f"{test_ota.ORIGIN}::deployment_manager::DeploymentCreated"
            return [dict(event_type=kind, json=event) for event in self.rpc("shim_events", [kind])]
        if method == "shim_fields":
            device = object_id(self.device)[2:]
            return super().rpc(method, params) + [[REGISTRY, INBOX_KEY, device]] + [
                [group, key("fleet_inbox::HeadKey"), "00"] for group in self.groups] + [
                [group, key("device_group::MemberKey"), device] for group in self.groups]
        if method == "suix_getDynamicFieldObject":
            parent, kind = params[0], params[1]["type"]
            if kind == INBOX_KEY:
                if self.fail_inbox:
                    raise ConnectionError("simulated unavailable inbox read")
                data = self.inbox and {"owner": {"Shared": {"initial_shared_version": "7"}}, "content": {
                    "type": f"{PACKAGE}::fleet_inbox::DeviceInbox",
                    "fields": {"id": "0x1b0c", "device_id": object_id(self.device), "version": "1", **self.inbox}}}
                return {"derived": True, "data": data}
            if kind.endswith("::fleet_inbox::HeadKey"):
                return {"data": {"content": {"fields": {"value": self.heads.get(parent)}}}}
            if kind.endswith("::device_group::MemberKey"):
                value = True if self.device in self.members.get(parent, ()) else None
                return {"data": {"content": {"fields": {"value": value}}}}
        return super().rpc(method, params)


def run(binary):
    sim = InboxSimulator()
    server = serve(sim)
    shim, rpc_url = start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-inbox-") as temporary:
            root = Path(temporary)
            key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
            applied = root / "applied.jsonl"
            updater = root / "apply.py"
            updater.write_text(f"import sys\nopen({str(applied)!r}, 'a').write(sys.argv[1] + '\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            (root / "deployment.json").write_text(json.dumps(manifest(sim, url, rpc_url)))
            config = root / "config.toml"
            log_path = root / "daemon.log"
            log = log_path.open("w")

            def configure(poll_seconds, scan_seconds):
                config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                    f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{DEVICE}"\ndevice_cap_id = "{CAP}"\n'
                    f'[apply]\ncommand = {json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds = 30\n'
                    f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = {poll_seconds}\n'
                    f'event_discovery_seconds = {max(1, scan_seconds)}\n')

            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def outcome(id):
                return state().get("deployments", {}).get(id, {}).get("status")

            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            def stop():
                nonlocal process
                process.terminate(); process.wait(timeout=10); process = None

            def settle():
                """Wait for two completed cycles, including reconciliation and logs."""
                for _ in range(2):
                    polls = test_ota.completed_polls(log_path)
                    wait_for(lambda: test_ota.completed_polls(log_path) > polls, "daemon stopped polling", process)
                time.sleep(0.3)

            # 1. Enrollment baseline. The inbox exists with a record created
            #    before enrollment: announced history is not replayed.
            sim.inbox = {"head": sim.head(), "groups": []}
            sim.deploy("0x700", created_ms=sim.now_ms - 10)
            configure(poll_seconds=1, scan_seconds=3600)
            process = start()
            wait_for(lambda: state().get("heads", {}).get("seen"), "daemon did not read its inbox", process)
            settle()
            assert outcome("0x700") is None, state()
            print("PASS: records announced before enrollment are not applied")

            # 2. A device deployment that only objects know about (its event is
            #    not served) is applied from the inbox, and reported. The
            #    inbox version in the SF-122 batch wakes the poll; with the
            #    discovery bound an hour away, no event scan runs.
            scans = sim.reads.get("ListEvents", 0)
            sim.now_ms += 1000
            sim.deploy("0x701")
            wait_for(lambda: outcome("0x701") == 1, "inbox deployment was not applied", process)
            wait_for(lambda: dict(sim.reports).get("0x701") == 1, "inbox deployment was not reported", process)
            assert sim.reads.get("ListEvents", 0) == scans, "an announced deployment needed an event scan"
            print("PASS: a device-targeted deployment is applied from its inbox without events")

            # 3. Indexed group membership listed only by the inbox: no
            #    DeviceAddedToGroup event and no vector entry.
            sim.members[GROUP] = {DEVICE}
            sim.inbox["groups"] = [GROUP]
            settle()
            sim.now_ms += 1000
            sim.deploy("0x702", groups=[GROUP], direct=False)
            wait_for(lambda: outcome("0x702") == 1, "group head deployment was not applied", process)
            assert GROUP in state()["groups"]
            print("PASS: an indexed group found through the inbox and its head applies a group deployment")

            # 3a. Quiet polls with the inbox and a group head watched stay one
            #     version batch each (SF-122): no inbox, head or event reads.
            settle()
            before = dict(sim.reads)
            wait_for(lambda: sim.reads.get("BatchGetObjects", 0) >= before.get("BatchGetObjects", 0) + 3,
                     "three idle polls missing", process)
            delta = {kind: count - before.get(kind, 0) for kind, count in sim.reads.items() if count != before.get(kind, 0)}
            assert delta == {"BatchGetObjects": 3}, delta
            print("PASS: idle polls watching the inbox and group heads are one version batch each")
            stop()

            # 3b. Joined a group while offline: its head applies what was
            #     announced after the last poll, and the event scan is not
            #     due on restart. Earlier records (before the device joined)
            #     are not applied.
            sim.groups[GROUP2] = {"admin": OPERATOR, "device_ids": []}
            sim.deploy("0x706", groups=[GROUP2], direct=False, created_ms=sim.now_ms - 1)
            sim.now_ms += 1000
            sim.members[GROUP2] = {DEVICE}
            sim.inbox["groups"] = [GROUP, GROUP2]
            sim.deploy("0x707", groups=[GROUP2], direct=False)
            sim.now_ms += 1000
            process = start()
            wait_for(lambda: outcome("0x707") == 1, "group joined while offline: its deployment was not applied", process)
            settle()
            assert outcome("0x706") is None, state()
            assert GROUP2 in state()["groups"]
            print("PASS: a group joined while offline applies what was announced since the last poll")
            stop()

            # 4. A scheduled start runs on time although polls are a minute
            #    apart: the inbox carries it and the daemon arms a timer.
            sim.now_ms += 1000
            start_ms = int(time.time() * 1000) + 4000
            sim.deploy("0x703", start_ms=start_ms)
            configure(poll_seconds=60, scan_seconds=3600)
            process = start()
            wait_for(lambda: state().get("deferred", {}).get("0x703", {}).get("reason") == "not yet scheduled",
                     "scheduled deployment was not deferred", process)
            wait_for(lambda: state()["heads"]["next_start_ms"] == start_ms, "next start was not recorded", process)
            wait_for(lambda: outcome("0x703") == 1, "scheduled start waited for the next poll", process, timeout=15)
            assert time.time() * 1000 >= start_ms
            print("PASS: a scheduled start runs on time with a 60 s poll interval")
            stop()

            # 5. Offline past the node's history: the node rejects the cursor's
            #    range, and the device resynchronizes from objects alone.
            cursor = state()["cursor"]
            for n in range(3):
                sim.add(f"0x7a{n}", direct=False)  # Other tenants' traffic, later pruned.
            sim.lowest = len(sim.events)
            sim.now_ms += 1000
            sim.deploy("0x704")
            configure(poll_seconds=1, scan_seconds=0)
            process = start()
            wait_for(lambda: outcome("0x704") == 1, "offline device did not resynchronize from objects", process)
            wait_for(lambda: state().get("cursor") != cursor, "pruned cursor was not moved to the tip", process)
            settle()
            text = log_path.read_text()
            assert "Event history is pruned; resynchronized" in text, text[-2000:]
            assert "Sync failed" not in text.split("Event history is pruned")[-1], text[-2000:]
            print("PASS: a device offline beyond the event history resynchronizes from objects alone")
            stop()

            def legacy():
                # A journal from a daemon before inboxes: no heads at all.
                path = root / "state" / "state.json"
                saved = json.loads(path.read_text())
                saved.pop("heads")
                path.write_text(json.dumps(saved))

            # 5b. Such a journal, offline past the node's history: heads start
            #     at the event cursor's checkpoint time, not the upgrade. The
            #     daemon is killed after the migration, before any head is
            #     read: the moved cursor is durable only with that baseline.
            legacy()
            cursor = state()["cursor"]
            sim.now_ms += 1000
            sim.deploy("0x708")
            created_ms = sim.now_ms
            sim.add("0x7b0", direct=False)
            sim.lowest = len(sim.events)
            sim.now_ms += 1000
            sim.fail_inbox = True
            failures = log_path.read_text().count("Sync failed")
            process = start()
            wait_for(lambda: log_path.read_text().count("Sync failed") > failures, "inbox read did not fail", process)
            process.kill(); process.wait(); process = None
            interrupted = state()
            assert interrupted["cursor"] != cursor, interrupted
            assert interrupted["heads"]["since_ms"] < created_ms, interrupted["heads"]
            assert outcome("0x708") is None, interrupted
            sim.fail_inbox = False
            injected = [e for e in sim.errors if "simulated unavailable inbox read" in e]
            assert injected, sim.errors
            sim.errors = [e for e in sim.errors if e not in injected]
            process = start()
            wait_for(lambda: outcome("0x708") == 1, "legacy journal lost a record created while offline (pruned)", process)
            settle()
            text = log_path.read_text()
            assert "the device inbox and group heads apply records from the event cursor's checkpoint time" in text, text[-2000:]
            print("PASS: a journal without heads keeps its event boundary when history is pruned")
            stop()

            # 5c. And with history retained: the event scan runs first, heads
            #     start where it stopped.
            legacy()
            sim.now_ms += 1000
            sim.deploy("0x709")
            sim.now_ms += 1000
            process = start()
            wait_for(lambda: outcome("0x709") == 1, "legacy journal lost a record created while offline", process)
            settle()
            assert state()["heads"]["since_ms"] <= sim.event_ms[-1], state()["heads"]
            print("PASS: a journal without heads keeps its event boundary when history is retained")

            # 6. The bounded event scan still applies a deployment an older
            #    client created without announcing it.
            sim.lowest = 0
            sim.now_ms += 1000
            sim.deploy("0x705", announce=False)
            wait_for(lambda: outcome("0x705") == 1, "unannounced deployment was not found by the event scan", process)
            wait_for(lambda: len(sim.reports) == 8, "reports were not all delivered", process)
            stop()
            applied_runs = applied.read_text().splitlines()
            assert len(applied_runs) == 8, applied_runs
            assert {id for id, _ in sim.reports} == {"0x701", "0x702", "0x703", "0x704", "0x705", "0x707", "0x708", "0x709"}, sim.reports
            assert not sim.errors, sim.errors
            print("PASS: the event scan remains a safety net for unannounced deployments; each applied once")
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait()
        shim.kill(); shim.wait()
        server.shutdown(); server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
