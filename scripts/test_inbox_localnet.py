#!/usr/bin/env python3
"""Device inboxes, group heads and the member index (SF-125) on a real local
Sui network, across a real package upgrade.

Publishes main's contracts (the merge base, or --base DIR), creates two
activated devices in a group and a group deployment with that old package,
then upgrades to this tree's contracts and checks, on chain:

  - type origins: new SF-125 types from the upgrade, existing types unchanged;
  - the existing group is indexed in place (device_ids drained into MemberKey
    fields) and indexed membership authorizes the new package's group report
    on the pre-upgrade record, while the old package's report fails closed;
  - an older package's add_device on an indexed group, and removal of both;
  - a first membership that creates the device's inbox, in the dApp/API
    builders' order (inbox before the custody loan; the reverse aborts);
  - a stranger cannot announce to the group (aborts on chain).

The real daemon then runs over a gRPC proxy (grpc_prune_proxy) in front of
the node, with the event scan bounded to an hour:

  - a device deployment and a group deployment reach it from its inbox and
    the group head alone, and are reported (receipts read from chain);
  - offline while the proxy reports history pruned above its cursor, it
    resynchronizes from objects alone; an unannounced deployment created in
    the pruned range is not applied;
  - with history available again, the event scan still applies a deployment
    created through the old package's entry point.

Run inside the dev image, network-none, with a throwaway node and faucet,
after building the daemon and its examples:

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_inbox_localnet.py [--base DIR] [--evidence FILE]

Refuses non-loopback endpoints and never reads the normal wallet.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from urllib.parse import urlparse

import deployment_manifest as dm
from test_desired_state import serve
from test_desired_state_localnet import base_contracts, wait_for
from test_ota import ARTIFACT, Simulator

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
PROXY = ROOT / "suifleet-edge/target/debug/examples/grpc_prune_proxy"
CLOCK = "@0x6"


def run(rpc_url, faucet_url, base_dir, evidence_path):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    evidence = {"digests": [], "checks": []}
    with tempfile.TemporaryDirectory(prefix="suifleet-inbox-localnet-") as temporary:
        root = Path(temporary)
        wallet = root / "wallet"
        wallet.mkdir()
        (wallet / "sui.keystore").write_text("[]")
        (wallet / "client.yaml").write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: inbox-test\n"
                                            f"  rpc: {rpc_url}\nactive_env: inbox-test\nactive_address: null\n")

        def client(*args, json_output=True, check=True):
            command = ["sui", "client", "--client.config", str(wallet / "client.yaml"), *map(str, args)]
            if json_output:
                command.append("--json")
            result = subprocess.run(command, text=True, capture_output=True, timeout=300)
            if result.returncode and check:
                raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
            if not json_output or result.returncode:
                return result
            out = result.stdout
            return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

        operator = client("new-address", "ed25519")["address"]
        stranger = client("new-address", "ed25519")["address"]
        client("switch", "--address", operator, json_output=False)

        def fund(address):
            client("faucet", "--url", faucet_url, "--address", address, json_output=False)

        def funded(address):
            gas = client("gas", address)
            return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0 if isinstance(gas, dict) else bool(gas)

        for address in (operator, stranger):
            fund(address)
            wait_for(lambda: funded(address), "local faucet did not supply gas")

        def record(result):
            assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
            evidence["digests"].append(result["digest"])
            return result

        def transaction(*ptb, sender=None):
            if sender:
                client("switch", "--address", sender, json_output=False)
            try:
                return record(client("ptb", *ptb, "--gas-budget", "200000000"))
            finally:
                if sender:
                    client("switch", "--address", operator, json_output=False)

        def fails(*ptb, sender=None):
            """A transaction that must abort on chain (or be rejected in dry run)."""
            if sender:
                client("switch", "--address", sender, json_output=False)
            try:
                result = client("ptb", *ptb, "--gas-budget", "200000000", check=False)
            finally:
                if sender:
                    client("switch", "--address", operator, json_output=False)
            if isinstance(result, dict):
                assert result["effects"]["status"]["status"] == "failure", result["effects"]["status"]
                evidence["digests"].append(result["digest"])
                return result["effects"]["status"].get("error", "")
            assert result.returncode, "expected the transaction to abort"
            message = result.stderr + result.stdout
            submitted = re.search(r"Error executing transaction '([1-9A-HJ-NP-Za-km-z]+)'", message)
            if submitted:
                status = dm.rpc(rpc_url, "sui_getTransactionBlock", [submitted[1], {"showEffects": True}])["effects"]["status"]
                assert status["status"] == "failure", status
                evidence["digests"].append(submitted[1])
            return message

        def created(result, suffix):
            return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

        def published(result):
            return next(c["packageId"] for c in result["objectChanges"] if c["type"] == "published")

        # ── Base package and pre-upgrade objects ──────────────────────────
        staging = root / "base"
        staging.mkdir()
        if base_dir:
            shutil.copytree(Path(base_dir) / "contracts", staging / "contracts")
            base = str(base_dir)
        else:
            base = base_contracts(staging)
        package = root / "package"
        shutil.copytree(staging / "contracts", package, ignore=shutil.ignore_patterns("build", "Published.toml"))
        common = ["--build-env", "testnet", "--pubfile-path", root / "Pub.toml", "--gas-budget", "500000000", "--silence-warnings"]
        first = record(client("test-publish", package, *common))
        old = published(first)
        upgrade_cap = created(first, "::package::UpgradeCap")[0]
        registry = created(first, "::device_registry::DeviceRegistry")[0]

        key = root / "device.key"
        key.write_text("07" * 32)
        key.chmod(0o600)
        signer = subprocess.check_output([str(DAEMON), "--derive-address", str(key)], text=True).strip()
        fund(signer)
        wait_for(lambda: funded(signer), "local faucet did not fund the device")

        def register(pkg, name, address):
            result = transaction("--move-call", f"{pkg}::device_registry::register_device", f"@{registry}", f'"{name}"',
                                 f"@{address}", '"linux"', CLOCK, "--assign", "device",
                                 "--move-call", f"{pkg}::device_cap::issue_cap", "device",
                                 "--move-call", f"{pkg}::device_registry::activate_device", f"@{registry}", "device",
                                 "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}")
            return (created(result, "::device_registry::Device")[0], created(result, "::device_cap::DeviceCap")[0],
                    created(result, "::device_registry::DeviceOwnerCap")[0])

        device, device_cap, device_owner = register(old, "daemon", signer)
        other, other_cap, other_owner = register(old, "other", operator)
        group = created(transaction("--move-call", f"{old}::device_group::create_group", '"fleet"', '""', CLOCK),
                        "::device_group::DeviceGroup")[0]

        def add_device(pkg, owner):
            return ("--move-call", f"{pkg}::device_registry::borrow_device", f"@{registry}", f"@{owner}", "--assign", "loan",
                    "--move-call", f"{pkg}::device_group::add_device", f"@{group}", "loan.0",
                    "--move-call", f"{pkg}::device_registry::return_device", f"@{registry}", "loan.0", "loan.1")

        transaction(*add_device(old, device_owner))
        transaction(*add_device(old, other_owner))
        blobs = {}
        sim = Simulator()

        def recipe(name):
            sim.add(name)
            body = sim.recipes.pop(f"recipe-{name}")
            body["componentName"] = name
            blobs[name] = body
            sim.recipes[name] = body
            return [f'"{name}"', '"1.0.0"', f'"{name}"', json.dumps(hashlib.sha256(ARTIFACT).hexdigest()),
                    json.dumps(hashlib.sha256(json.dumps(body).encode()).hexdigest())]

        before = created(transaction("--move-call", f"{old}::deployment_manager::create_deployment_verified",
                                     *recipe("pre-upgrade"), f"vector[@{group}]", "vector[]", CLOCK),
                         "::deployment_manager::DeploymentRecord")[0]

        # ── Upgrade ───────────────────────────────────────────────────────
        shutil.rmtree(package)
        shutil.copytree(ROOT / "contracts", package, ignore=shutil.ignore_patterns("build", "Published.toml"))
        new = published(record(client("test-upgrade", package, "--upgrade-capability", upgrade_cap, *common)))
        facts = dm.chain_facts(rpc_url, new, upgrade_cap)
        origins = dm.origins(facts["objects"][dm.object_id(new)])
        assert all(origins[name] == dm.object_id(new) for name in dm.INBOX_TYPES), "SF-125 types must originate in the upgrade"
        assert origins["device_group::DeviceGroup"] == dm.object_id(old)
        assert origins["deployment_manager::DeploymentRecord"] == dm.object_id(old)
        evidence["checks"].append("upgrade origins")

        def fields(object_id):
            return dm.rpc(rpc_url, "sui_getObject", [object_id, {"showContent": True}])["data"]["content"]["fields"]

        def member(device_id):
            value = dm.rpc(rpc_url, "suix_getDynamicFieldObject",
                           [group, {"type": f"{new}::device_group::MemberKey", "value": {"device_id": device_id}}])
            return "data" in value and value["data"]["content"]["fields"]["value"] is True

        # ── Existing group indexed in place ───────────────────────────────
        transaction("--move-call", f"{new}::device_group::index_members", f"@{group}", "1")
        assert len(fields(group)["device_ids"]) == 1
        transaction("--move-call", f"{new}::device_group::index_members", f"@{group}", "10")
        assert fields(group)["device_ids"] == [] and member(device) and member(other)
        report = lambda pkg: ("--move-call", f"{pkg}::deployment_manager::report_verified_status_for_group",
                              f"@{before}", f"@{registry}", f"@{other_cap}", f"@{group}", "1")
        why = fails(*report(old))
        assert "deployment_manager::report_verified_status_for_group" in why and "with code 3" in why, why  # ENotTargeted
        transaction(*report(new))
        evidence["checks"].append("existing group indexed; new-package group report authorized by MemberKey; old package fails closed")
        # An older package still sees only the vector; removal clears both.
        transaction(*add_device(old, other_owner))
        assert len(fields(group)["device_ids"]) == 1 and member(other)
        transaction("--move-call", f"{new}::device_group::remove_device", f"@{group}", f"@{other}")
        assert fields(group)["device_ids"] == [] and not member(other)
        evidence["checks"].append("old-package add_device on an indexed group; removal clears vector and index")

        # ── First membership creating the inbox (dApp/API builder order) ──
        # new_inbox needs the device in custody, so it precedes the loan; the
        # reverse order aborts with ENotOperator.
        def add_member_creating_inbox(during_loan):
            borrow = ["--move-call", f"{new}::device_registry::borrow_device", f"@{registry}", f"@{other_owner}", "--assign", "loan"]
            create = ["--move-call", f"{new}::fleet_inbox::new_inbox", f"@{registry}", f"@{other_owner}", "--assign", "created"]
            rest = ["--move-call", f"{new}::fleet_inbox::add_member", f"@{group}", "created", "loan.0",
                    "--move-call", f"{new}::device_registry::return_device", f"@{registry}", "loan.0", "loan.1",
                    "--move-call", f"{new}::fleet_inbox::share_inbox", "created"]
            return (*(borrow + create if during_loan else create + borrow), *rest)

        why = fails(*add_member_creating_inbox(True))
        assert "fleet_inbox::" in why and "with code 3" in why, why  # ENotOperator
        other_inbox = created(transaction(*add_member_creating_inbox(False)), "::fleet_inbox::DeviceInbox")[0]
        assert member(other) and fields(other_inbox)["groups"] == [group]
        transaction("--move-call", f"{new}::fleet_inbox::remove_member", f"@{group}", f"@{other_inbox}")
        assert not member(other) and fields(other_inbox)["groups"] == []
        evidence["checks"].append("first membership creates the inbox before the custody loan; the reverse order aborts")

        # ── Inbox and announcement authority ──────────────────────────────
        made = transaction("--move-call", f"{new}::fleet_inbox::new_inbox", f"@{registry}", f"@{device_owner}",
                           "--assign", "inbox", "--move-call", f"{new}::fleet_inbox::follow_group", "inbox", f"@{group}",
                           "--move-call", f"{new}::fleet_inbox::share_inbox", "inbox")
        inbox = created(made, "::fleet_inbox::DeviceInbox")[0]
        assert dm.object_id(inbox) == dm.inbox_id(registry, origins["fleet_inbox::InboxKey"], device), "inbox not at its derived address"
        assert fields(inbox)["groups"] == [group]
        stranger_record = ["--move-call", f"{new}::deployment_manager::new_deployment_verified", *recipe("intruder"),
                           f"vector[@{group}]", "vector[]", CLOCK, "--assign", "record",
                           "--move-call", f"{new}::fleet_inbox::announce_group", f"@{group}", "record", CLOCK,
                           "--move-call", f"{new}::deployment_manager::share_deployment", "record"]
        fails(*stranger_record, sender=stranger)
        evidence["checks"].append("stranger cannot announce to a group it does not administer")

        def deploy(name, *, direct=False, groups=False, announce=True, pkg=None):
            targets = [f"vector[@{group}]" if groups else "vector[]", f"vector[@{device}]" if direct else "vector[]"]
            if not announce:
                return created(transaction("--move-call", f"{pkg or new}::deployment_manager::create_deployment_verified",
                                           *recipe(name), *targets, CLOCK), "::deployment_manager::DeploymentRecord")[0]
            ptb = ["--move-call", f"{new}::deployment_manager::new_deployment_verified", *recipe(name), *targets, CLOCK, "--assign", "record"]
            if groups:
                ptb += ["--move-call", f"{new}::fleet_inbox::announce_group", f"@{group}", "record", CLOCK]
            if direct:
                ptb += ["--move-call", f"{new}::fleet_inbox::announce_device", f"@{inbox}", f"@{registry}", f"@{device_owner}", "record", CLOCK]
            ptb += ["--move-call", f"{new}::deployment_manager::share_deployment", "record"]
            return created(transaction(*ptb), "::deployment_manager::DeploymentRecord")[0]

        def receipt(deployment):
            field = dm.rpc(rpc_url, "suix_getDynamicFieldObject", [deployment, {
                "type": f"{origins['deployment_manager::ReceiptsKey']}::deployment_manager::ReceiptsKey", "value": {"dummy_field": False}}])
            statuses = field["data"]["content"]["fields"]["value"]["fields"]["statuses"]["fields"]["contents"]
            return {dm.object_id(e["fields"]["key"]): int(e["fields"]["value"]) for e in statuses}.get(dm.object_id(device))

        # ── The real daemon through a pruning proxy ───────────────────────
        lowest = root / "lowest"
        lowest.write_text("0")
        proxy = subprocess.Popen([str(PROXY), rpc_url, str(lowest)], stdout=subprocess.PIPE, text=True)
        server = serve(sim)
        process = None
        try:
            line = proxy.stdout.readline()
            assert line.startswith("listening "), line
            proxy_url = f"http://127.0.0.1:{int(line.split()[1])}"
            walrus = f"http://127.0.0.1:{server.server_port}"
            manifest = dm.resolve(SimpleNamespace(chain_identifier=facts["chain_identifier"], rpc=rpc_url, package=new,
                                                  upgrade_cap=upgrade_cap, registry=registry, network="localnet",
                                                  publisher=walrus, aggregator=walrus))
            assert all(name in manifest["type_origins"] for name in dm.INBOX_TYPES)
            manifest["rpc_url"] = proxy_url
            (root / "deployment.json").write_text(json.dumps(manifest))
            applied = root / "applied.jsonl"
            updater = root / "apply.py"
            updater.write_text(f"import sys\nopen({str(applied)!r}, 'a').write(sys.argv[1] + '\\n')\n")
            config = root / "config.toml"

            def configure(scan_seconds):
                config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                    f'[device]\nkeypair_path = "{key}"\ndevice_object_id = "{device}"\ndevice_cap_id = "{device_cap}"\n'
                    f'[apply]\ncommand = "python3 {updater}"\ntimeout_seconds = 30\n'
                    f'[runtime]\nstate_dir = "{root}/state"\npoll_seconds = 1\nevent_scan_seconds = {scan_seconds}\n')

            def state():
                path = root / "state/state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def reported(deployment):
                return state().get("deployments", {}).get(deployment, {}).get("reported")

            log_path = root / "daemon.log"
            log = log_path.open("w")

            def start():
                return subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            configure(3600)
            process = start()
            wait_for(lambda: state().get("heads", {}).get("seen") and state()["heads"].get("event_scan_ms"),
                     "daemon did not read its inbox and scan once", process)
            first_scan = state()["heads"]["event_scan_ms"]
            direct = deploy("inbox-direct", direct=True)
            grouped = deploy("inbox-group", groups=True)
            for deployment in (direct, grouped):
                wait_for(lambda: reported(deployment), f"{deployment} not applied/reported from objects", process)
                wait_for(lambda: receipt(deployment) == 1, f"{deployment} receipt not on chain", process)
            assert state()["heads"]["event_scan_ms"] == first_scan, "the event scan ran although it was not due"
            evidence["checks"].append("device and group deployments applied from inbox/group head alone, receipts on chain")
            process.terminate()
            process.wait(timeout=20)
            process = None

            # Offline past the node's history.
            tip = int(dm.rpc(rpc_url, "sui_getLatestCheckpointSequenceNumber", []))
            offline_direct = deploy("offline-direct", direct=True)
            offline_group = deploy("offline-group", groups=True)
            missed = deploy("unannounced-pruned", direct=True, announce=False, pkg=old)
            lowest.write_text(str(int(dm.rpc(rpc_url, "sui_getLatestCheckpointSequenceNumber", [])) + 1))
            assert int(lowest.read_text()) > tip
            configure(0)
            process = start()
            for deployment in (offline_direct, offline_group):
                wait_for(lambda: reported(deployment), f"{deployment} not resynchronized from objects", process)
                wait_for(lambda: receipt(deployment) == 1, f"{deployment} receipt not on chain", process)
            text = log_path.read_text()
            assert "Event history is pruned; resynchronized" in text, text[-3000:]
            assert missed not in state().get("deployments", {})
            evidence["checks"].append("offline beyond pruned history: resynchronized from objects alone; pruned unannounced record skipped")

            # History available again: the scan still serves older clients.
            lowest.write_text("0")
            legacy = deploy("old-entry-point", direct=True, announce=False, pkg=old)
            wait_for(lambda: reported(legacy), "old-package deployment not applied by the event scan", process)
            wait_for(lambda: receipt(legacy) == 1, "old-package deployment receipt not on chain", process)
            assert missed not in state().get("deployments", {}), "a pruned record was replayed"
            runs = applied.read_text().splitlines()
            assert len(runs) == 5, runs
            evidence["checks"].append("event scan safety net applies an old-package deployment")
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            proxy.kill()
            proxy.wait()
            server.shutdown()
    result = {"result": "PASS", "base": base, "old_package": old, "new_package": new, **evidence}
    if evidence_path:
        Path(evidence_path).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    parser.add_argument("--base", help="a directory holding the pre-upgrade contracts/ (default: merge base with origin/main)")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    run(args.rpc, args.faucet, args.base, args.evidence)
