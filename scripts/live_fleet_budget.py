#!/usr/bin/env python3
"""SF-083 on public testnet with the dedicated agent wallet (docs/TESTNET.md,
docs/FLEET_BUDGET.md): real deposits, draws, refusals and a fleet-paid Walrus
upload, every effect checked.

- Publishes the head contracts and the fleet_storage package against them
  (verify-before-sign), cached by content hash beside the wallet.
- Registers a throwaway device, creates a budget with a deposit, enrolls the
  device (float, and a reserve in its own shared GasAccount), and the device
  draws from that account while the budget object stays at its version;
  then on-chain refusals: above the float cap, after revocation, while
  paused.
- The real daemon tops itself up over the public fullnode, then journals the
  period's draw limit.
- Swaps SUI for WAL through Walrus's pinned Testnet exchange into the fleet
  storage (and a slippage refusal), buys storage and extends the pool with
  the fleet's own WAL (fleet_storage::buy_storage, extend_pool); the device
  registers, uploads and certifies a blob held by the fleet, read back from
  the storage nodes, and its second upload exceeds its quota; the owner
  uploads a blob with the dApp's own fleet-paid upload code
  (scripts/live_fleet_upload.cjs), each transaction verified before signing.
- Removes the device (its reserve returns), withdraws the unused deposit and
  sweeps the device key.

Requires the machine-session lock (SUIFLEET_TESTNET_LOCK_TOKEN), the pinned
Walrus SDK installed privately for the device side
(scripts/live_walrus_upload.mjs) and dapp/node_modules for the owner side.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from types import SimpleNamespace

import deployment_manifest as dm
import sui_toolchain as toolchain
from live_common import CLOCK, DAEMON, ROOT, WALRUS_TESTNET, Live, key_wallet, pending_key, plain, recover_key, stage, sweep, wait_for

WALRUS_PACKAGE = "0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611"
SYSTEM = "0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af"
EXCHANGE_PACKAGE = "0x82593828ed3fcb8c6a235eac9abd0adbe9c5f9bbffa9b1e7a45cdd884481ef9f"
EXCHANGE = "0xf4d164ea2def5fe07dc573992a029e010dba09b1a8dcbc44c5c2e79567f39073"
DEPOSIT, FLOAT, DRAW, ALLOWANCE, RESERVE = 60_000_000, 20_000_000, 5_000_000, 30_000_000, 25_000_000
SWAP = 20_000_000
BLOB_BYTES = 2048
ENCODED = 66_034_000  # One RS2-encoded 2 KiB blob at 1000 shards (scripts/test_fleet_budget.cjs).
UPLOAD_GAS = 60_000_000
DAEMON_DRAW = 4_000_000


def unrelocate_module(module, package):
    """Normalize only Move's address-identifier table, never literal constants."""
    data = bytearray(module)
    assert data[:4] == bytes.fromhex("a11ceb0b"), "not Move bytecode"
    offset = 8
    def uleb():
        nonlocal offset
        value = shift = 0
        while True:
            byte = data[offset]; offset += 1
            value |= (byte & 127) << shift
            if byte < 128: return value
            shift += 7
            assert shift <= 28
    count = uleb()
    tables = []
    for _ in range(count):
        kind = data[offset]; offset += 1
        tables.append((kind, uleb(), uleb()))
    for kind, start, size in tables:
        if kind == 8:
            assert size % 32 == 0
            for at in range(offset + start, offset + start + size, 32):
                if data[at:at + 32] == bytes.fromhex(dm.object_id(package)[2:]):
                    data[at:at + 32] = bytes(32)
    return bytes(data)


def fixture_key(tools):
    digest = hashlib.sha256()
    for path in sorted([*(ROOT / "contracts/sources").glob("*.move"), ROOT / "contracts/Move.toml", ROOT / "contracts/Move.lock",
                        *(ROOT / "contracts/fleet_storage/sources").glob("*.move"), ROOT / "contracts/fleet_storage/Move.toml",
                        ROOT / "contracts/fleet_storage/Move.lock"]):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return f"{digest.hexdigest()[:24]}@{tools['build']['version'].split()[-1]}"


def record_publication(package_dir, chain, package, upgrade_cap, published_at=None, version=1):
    """The staged sui_edge's Published.toml names the fixture's publication,
    so fleet_storage resolves every dependency (sui_edge, Walrus, WAL) from a
    Published.toml; Walrus's pubfile mode accepts only local sources. An
    upgraded package records its latest version beside its original ID."""
    (package_dir / "Published.toml").write_text(
        f'[published.testnet]\nchain-id = "{chain}"\npublished-at = "{toolchain.normal(published_at or package)}"\n'
        f'original-id = "{toolchain.normal(package)}"\nversion = {int(version)}\ntoolchain-version = "{toolchain.BUILD["version"].split()[1].split("-")[0]}"\n'
        f'build-config = {{ flavor = "sui", edition = "2024" }}\nupgrade-capability = "{toolchain.normal(upgrade_cap)}"\n')


def publish_storage(live, storage_dir, label="publish fleet_storage"):
    """fleet_storage built by the pinned release compiler, its bytecode and
    dependencies verified in the unsigned transaction before signing."""
    execute = live.executor()
    record = live.evidence.setdefault("verified_package_transactions", {}).setdefault(label, {})

    def submit(budget):
        try:
            built = toolchain.compile_package(live.build_sui, storage_dir)
        except (RuntimeError, ValueError, KeyError) as error:
            raise toolchain.NotSubmitted(str(error)) from error
        lock = storage_dir / "Move.lock"
        pinned = lock.read_bytes()
        # publish builds for the active environment (testnet); it refuses --build-env.
        serialized = live.client("publish", storage_dir, "--silence-warnings", "--gas-budget", str(budget),
                                 "--serialize-unsigned-transaction", json_output=False, check=False)
        if lock.read_bytes() != pinned:
            raise toolchain.NotSubmitted("network CLI changed the pinned Move.lock")
        tx_bytes = serialized.stdout.strip().splitlines()[-1].strip() if serialized.stdout.strip() else ""
        if serialized.returncode or not tx_bytes:
            raise toolchain.NotSubmitted(f"publish could not build a transaction:\n{serialized.stderr[-3000:]}")
        record.update(toolchain.verify_package_transaction(toolchain.decode(tx_bytes), built, sender=live.operator, budget=budget),
                      digest=bytes(built["digest"]).hex(), dependencies=built["dependencies"])
        return execute("serialized-tx", tx_bytes)
    return live.execute(label, submit, gas=300_000_000)


def event(result, suffix):
    found = [e for e in result.get("events", []) if e["type"].endswith(suffix)]
    assert found, f"no {suffix} event"
    return found[0]["parsedJson"]


def balance_change(result, address):
    return sum(int(c["amount"]) for c in result.get("balanceChanges", [])
               if dm.object_id(c.get("owner", {}).get("AddressOwner", "0x0")) == dm.object_id(address) and c["coinType"].endswith("::sui::SUI"))


def net_gas(result):
    gas = result["effects"]["gasUsed"]
    return int(gas["computationCost"]) + int(gas["storageCost"]) - int(gas["storageRebate"])


def u8_vector(hex_text):
    return "vector[" + ",".join(f"{b}u8" for b in bytes.fromhex(hex_text)) + "]"


def owner_upload(live, root, manifest, storage_package, storage, budget, budget_cap):
    """The dApp's fleet-paid upload, run by scripts/live_fleet_upload.cjs.
    Each transaction it builds is decoded and must be exactly one expected
    fleet_storage call from the wallet within the reserved gas before the
    wallet signs it."""
    path = root / "owner-blob.bin"
    path.write_bytes(b"suifleet sf083 owner upload\n" + secrets.token_bytes(BLOB_BYTES - 28))
    env = {**os.environ, "NEXT_PUBLIC_DEPLOYMENT_MANIFEST": json.dumps(manifest)}
    node = subprocess.Popen(["node", str(ROOT / "scripts/live_fleet_upload.cjs"), live.rpc_url, live.operator, storage_package, storage,
                             budget, budget_cap, str(path), str(UPLOAD_GAS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    digests = []
    try:
        for step in ("register_fleet_blob", "certify_blob"):
            line = json.loads(node.stdout.readline())

            def submit(reserved, tx=line["tx"], step=step):
                data = toolchain.decode(tx)
                calls = [(dm.object_id(c.get("package", "0x0")), c.get("module"), c.get("function")) for c in data["commands"]]
                if dm.object_id(data["sender"]) != live.operator or dm.object_id(data["gas_owner"]) != live.operator:
                    raise toolchain.NotSubmitted("the dApp transaction is not sent and paid by the wallet")
                if int(data["gas_budget"]) != UPLOAD_GAS or reserved != UPLOAD_GAS:
                    raise toolchain.NotSubmitted("the dApp transaction's gas budget differs from the reserved allowance")
                if calls != [(dm.object_id(storage_package), "fleet_storage", step)]:
                    raise toolchain.NotSubmitted(f"unexpected dApp transaction commands {calls}")
                return live.executor()("serialized-tx", tx)
            r = live.execute(f"owner {step.replace('_', ' ')} (dApp upload)", submit, gas=UPLOAD_GAS)
            digests.append(r["digest"])
            node.stdin.write(json.dumps({"digest": r["digest"]}) + "\n")
            node.stdin.flush()
        result = json.loads(node.stdout.readline())
        assert node.wait(timeout=120) == 0
    finally:
        if node.poll() is None:
            node.kill()
            node.wait()
    return result, digests


def run(args):
    if not os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
        raise RuntimeError("machine-session network:suifleet-testnet lock is required")
    os.umask(0o077)
    sdk = Path(args.walrus_sdk)
    assert (sdk / "node_modules/@mysten/walrus/package.json").exists(), "install the pinned Walrus SDK first (scripts/live_walrus_upload.mjs)"
    live = Live(args.wallet, "testnet", args.max_spend_mist, build_sui=args.build_sui, network_sui=args.network_sui)
    live.preflight(auto_fund=True)
    measured = live.evidence.setdefault("measured", {})
    root = Path(tempfile.mkdtemp(prefix="sf083-live-"))
    state_path = live.wallet.parent / "sf083-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("chain") not in (None, live.chain):
        state = {}  # A testnet reset discards the fixture.
    key_path = live.wallet.parent / "sf083-device.key"
    recover_key(live, key_path, root)
    daemon = None
    try:
        # ── Fixture: head sui_edge plus fleet_storage, reused while unchanged ──
        key = fixture_key(live.tools)
        fixtures = state.setdefault("fixtures", {})
        state.update(chain=live.chain)
        save = lambda: state_path.write_text(json.dumps(state, indent=2) + "\n")
        if args.adopt and key not in fixtures:
            # A publication this run's checks verified before signing, whose
            # run failed before caching it (its evidence names the digest).
            package, registry, cap = map(dm.object_id, args.adopt)
            fixtures[key] = dict(package=package, registry=registry, upgrade_cap=cap, adopted=True)
            save()
        exists = lambda object_id: isinstance(live.client("object", object_id, check=False), dict)
        fixture = fixtures.get(key)
        if fixture and not all(exists(fixture[k]) for k in ("package", "registry")):
            fixture = None
        package_dir = root / "contracts"
        stage(ROOT / "contracts", package_dir)
        core_build = toolchain.compile_package(live.build_sui, package_dir)
        def same_modules(package, built):
            raw = live.client("object", package, "--bcs")["data"]["Package"]
            return sorted(unrelocate_module(bytes(v), package) for v in raw["module_map"].values()) == sorted(base64.b64decode(v) for v in built["modules"])
        if not fixture:
            # A storage-only change must not republish identical core bytecode.
            # Re-verify on-chain modules and ownership before adopting a cache.
            for previous in fixtures.values():
                if exists(previous["package"]) and same_modules(previous["package"], core_build):
                    fixture = {k: previous[k] for k in ("package", "registry", "upgrade_cap")}
                    fixtures[key] = fixture
                    save()
                    break
        if fixture:
            assert same_modules(fixture["package"], core_build), "cached core bytecode differs from pinned compilation"
            assert dm.object_id(live.owner(fixture["upgrade_cap"])) == live.operator, "fixture UpgradeCap is not the wallet's"
            facts = dm.chain_facts(live.rpc_url, fixture["package"], fixture["upgrade_cap"], fixture["registry"])
            assert dm.upgrade_cap(facts["objects"][dm.object_id(fixture["upgrade_cap"])])[:2] == (dm.object_id(fixture["package"]), "1")
            live.check(f"reused the fixture {key}: package {fixture['package']}, UpgradeCap at version 1 held by the wallet")
        else:
            r = live.publish("publish sui_edge head", package_dir, root / "Pub.testnet.toml")
            fixture = fixtures[key] = dict(package=live.published(r), registry=live.created(r, "::device_registry::DeviceRegistry")[0],
                                           upgrade_cap=live.created(r, "::package::UpgradeCap")[0])
            save()  # Package storage is never refunded: cache it before anything else can fail.
            live.check(f"published sui_edge {fixture['package']} with verified bytecode")
        if not fixture.get("storage_package") or not exists(fixture["storage_package"]):
            record_publication(package_dir, live.chain, fixture["package"], fixture["upgrade_cap"])
            r = publish_storage(live, package_dir / "fleet_storage")
            fixture.update(storage_package=live.published(r), storage_upgrade_cap=live.created(r, "::package::UpgradeCap")[0])
            save()
            live.check(f"published fleet_storage {fixture['storage_package']} linked to it and the live Walrus package, bytecode verified")
        package, registry, storage_package = fixture["package"], fixture["registry"], fixture["storage_package"]
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=package, upgrade_cap=fixture["upgrade_cap"],
                                              registry=registry, network="testnet", publisher=WALRUS_TESTNET[0], aggregator=WALRUS_TESTNET[1]))
        assert all(dm.object_id(manifest["type_origins"][name]) == dm.object_id(package) for name in dm.BUDGET_TYPES)
        live.evidence.update(fixture=fixture, fixture_key=key)
        live.check("manifest resolved over gRPC with every fleet budget type origin at the package")
        call = lambda module, fn, pkg=package: f"{pkg}::{module}::{fn}"

        # ── A throwaway device and the budget ──
        seed = secrets.token_hex(32)
        pending_key(key_path, seed)
        device_config, device_address = key_wallet(live, seed, root, "device")
        r = live.transaction("register and activate the test device", "--move-call", call("device_registry", "register_device"), f"@{registry}",
                             '"sf083-device"', f"@{device_address}", '"aarch64"', CLOCK, "--assign", "device",
                             "--move-call", call("device_cap", "issue_cap"), "device",
                             "--move-call", call("device_registry", "activate_device"), f"@{registry}", "device", "--assign", "owner",
                             "--transfer-objects", "[owner]", f"@{live.operator}", budget=40_000_000)
        device_id = live.created(r, "::device_registry::Device")[0]
        device_cap = live.created(r, "::device_cap::DeviceCap")[0]
        owner_cap = live.created(r, "::device_registry::DeviceOwnerCap")[0]
        r = live.transaction("create the fleet budget with a deposit", "--split-coins", "gas", f"[{DEPOSIT}u64]", "--assign", "deposit",
                             "--move-call", call("fleet_budget", "new_limits"), f"{FLOAT}u64", "3600000u64", f"{ALLOWANCE}u64", "3u64", "0u64",
                             "--assign", "limits",
                             "--move-call", call("fleet_budget", "create"), f"@{registry}", "deposit", "limits", f"{DEPOSIT // 3}u64",
                             "--assign", "cap", "--transfer-objects", "[cap]", f"@{live.operator}", budget=30_000_000, funding=DEPOSIT)
        budget = live.created(r, "::fleet_budget::FleetBudget")[0]
        budget_cap = live.created(r, "::fleet_budget::BudgetOwnerCap")[0]
        assert int(event(r, "::fleet_budget::Deposited")["amount"]) == DEPOSIT
        measured["create_budget_net_gas_mist"] = net_gas(r)
        r = live.transaction("enroll the device with its float and reserve", "--move-call", call("fleet_budget", "enroll_device"), f"@{budget}", f"@{budget_cap}",
                             f"@{registry}", f"@{owner_cap}", f"{FLOAT}u64", f"{RESERVE}u64", CLOCK, budget=20_000_000)
        assert balance_change(r, device_address) == FLOAT, r.get("balanceChanges")
        account = live.created(r, "::fleet_budget::GasAccount")[0]
        assert "shared" in json.dumps(live.owner(account)).lower(), live.owner(account)
        assert int(event(r, "::fleet_budget::ReserveChanged")["reserve"]) == RESERVE
        live.evidence.update(device=device_id, device_cap=device_cap, owner_cap=owner_cap, device_address=device_address, budget=budget,
                             budget_cap=budget_cap, account=account)
        live.check(f"budget {budget} created with a {DEPOSIT} MIST deposit; the device address received exactly its {FLOAT} MIST float "
                   f"and its own shared GasAccount {account} holds a {RESERVE} MIST reserve")
        # The float came from the charged deposit; the device's own gas is bounded by it.
        device = live.child(device_config, FLOAT + RESERVE)
        version = lambda object_id: str(live.object(object_id)["version"])
        before = version(budget)
        r = live.transaction("device draws from its account", "--move-call", call("fleet_budget", "draw_gas"), f"@{budget}", f"@{account}",
                             f"@{registry}", f"@{device_cap}", f"{DRAW}u64", CLOCK, budget=10_000_000, sender=device)
        drawn = event(r, "::fleet_budget::GasDrawn")
        assert int(drawn["amount"]) == DRAW and dm.object_id(drawn["device_address"]) == dm.object_id(device_address)
        assert int(drawn["reserve"]) == RESERVE - DRAW, drawn
        # Draws only read the fleet budget: its version is unchanged.
        assert version(budget) == before, (version(budget), before)
        assert not any(dm.object_id(o["reference"]["objectId"]) == dm.object_id(budget) for o in r["effects"].get("mutated", [])), r["effects"].get("mutated")
        measured["draw_gas"] = dict(r["effects"]["gasUsed"], net_mist=net_gas(r), digest=r["digest"], budget_version=before)
        live.check(f"the device drew {DRAW} MIST from its own account; GasDrawn recorded it; the fleet budget stayed at version {before} "
                   f"(net gas {net_gas(r)} MIST)")
        draw_args = lambda amount: ("--move-call", call("fleet_budget", "draw_gas"), f"@{budget}", f"@{account}", f"@{registry}", f"@{device_cap}",
                                    f"{amount}u64", CLOCK)
        live.attack("draw above the float cap", *draw_args(FLOAT + 1), module="fleet_budget", function="draw_gas", code=8, sender=device, budget=10_000_000)
        live.transaction("owner revokes the device", "--move-call", call("device_registry", "revoke_device"), f"@{registry}", f"@{owner_cap}", budget=10_000_000)
        live.attack("draw after revocation", *draw_args(1), module="device_cap", function="assert_active", code=2, sender=device, budget=10_000_000)
        live.transaction("owner reinstates the device", "--move-call", call("device_registry", "reinstate_device"), f"@{registry}", f"@{owner_cap}", budget=10_000_000)
        live.transaction("owner pauses the fleet", "--move-call", call("fleet_budget", "set_paused"), f"@{budget}", f"@{budget_cap}", "true", budget=10_000_000)
        live.attack("draw while paused", *draw_args(1), module="fleet_budget", function="assert_may_draw", code=3, sender=device, budget=10_000_000)
        live.transaction("owner resumes the fleet", "--move-call", call("fleet_budget", "set_paused"), f"@{budget}", f"@{budget_cap}", "false", budget=10_000_000)
        live.check("on-chain refusals: above the float cap (8), after revocation (device_cap 2), while paused (3)")

        # ── The real daemon tops itself up over the public fullnode ──
        state_dir = root / "daemon-state"
        template = root / "daemon-template.toml"
        template.write_text(f'[device]\nkeypair_path = {json.dumps(str(root / "device/test.key"))}\ndevice_object_id = "{device_id}"\n'
                            f'device_cap_id = "{device_cap}"\n[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\n'
                            f'[runtime]\nstate_dir = {json.dumps(str(state_dir))}\npoll_seconds = 3\n'
                            f'[budget]\nbudget_id = "{budget}"\nlow_water_mist = 1000000000000\ndraw_mist = {DAEMON_DRAW}\n')
        release = root / "release"
        dm.generate(manifest, release, template)
        log = (root / "daemon.log").open("w")
        live.assert_lock()
        daemon = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(release / "config.toml")}, stdout=log, stderr=log)
        journal = lambda: (json.loads((state_dir / "state.json").read_text()).get("funding") or {}) if (state_dir / "state.json").exists() else {}
        wait_for(lambda: journal().get("blocked"), "the daemon did not draw to its limit", daemon, timeout=300)
        daemon.terminate()
        daemon.wait(timeout=30)
        daemon = None
        funding = journal()
        assert funding.get("draws", 0) >= 1 and funding.get("last_draw_digest"), funding
        effects = live.executed(funding["last_draw_digest"])
        assert effects["effects"]["status"]["status"] == "success"
        assert any(e["type"].endswith("::fleet_budget::GasDrawn") for e in effects.get("events", []))
        assert "draw limit" in funding["blocked"] or "used up" in funding["blocked"], funding
        live.evidence["daemon"] = dict(funding, last_draw_effects=effects["effects"]["gasUsed"])
        live.check(f"the real daemon drew {funding['draws']} time(s) over gRPC (last {funding['last_draw_digest']}), then journaled: {funding['blocked']}")

        # ── Owner-prepaid Walrus storage ──
        r = live.transaction("create fleet storage", "--move-call", call("fleet_storage", "new_quota", storage_package), "3600000u64",
                             "200000000u64", "1u64", "4096u64", "5000000u64", "--assign", "quota",
                             "--move-call", call("fleet_storage", "create", storage_package), f"@{budget}", f"@{budget_cap}", "quota", budget=20_000_000)
        storage = live.created(r, "::fleet_storage::FleetStorage")[0]
        rate = plain(live.object(EXCHANGE)["content"])["rate"]
        expected_wal = SWAP * int(rate["wal"]) // int(rate["sui"])
        swap = ("--split-coins", "gas", f"[{SWAP}u64]", "--assign", "sui",
                "--move-call", f"{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal", f"@{EXCHANGE}", "sui", "--assign", "wal")
        deposit_min = lambda minimum: ("--move-call", call("fleet_storage", "deposit_wal_at_least", storage_package), f"@{storage}", f"@{budget}",
                                       f"@{budget_cap}", "wal", f"{minimum}u64")
        live.attack("swap below its slippage limit", *swap, *deposit_min(expected_wal + 1), module="fleet_storage",
                    function="deposit_wal_at_least", code=12, budget=20_000_000)
        r = live.transaction("swap SUI for WAL into fleet storage", *swap, *deposit_min(expected_wal), budget=20_000_000, funding=SWAP)
        assert int(event(r, "::fleet_storage::WalDeposited")["amount"]) == expected_wal
        live.check(f"SUI-only deposit: {SWAP} MIST swapped through the pinned exchange for {expected_wal} FROST in one PTB; a higher minimum aborted (12)")
        (root / "blob.bin").write_bytes(b"suifleet sf083 fleet-paid upload\n" + secrets.token_bytes(BLOB_BYTES - 33))
        upload = subprocess.Popen(["node", str(ROOT / "scripts/live_walrus_upload.mjs"), str(sdk), live.rpc_url, str(root / "blob.bin"), WALRUS_PACKAGE],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        encoded = json.loads(upload.stdout.readline())
        size = 2 * ENCODED  # The device's blob and the owner's.
        r = live.transaction("buy storage with the fleet's WAL", "--move-call", call("fleet_storage", "buy_storage", storage_package), f"@{storage}",
                             f"@{budget}", f"@{budget_cap}", f"@{SYSTEM}", f"{size}u64", "3u32", budget=30_000_000)
        purchased, deposited = event(r, "::fleet_storage::StoragePurchased"), event(r, "::fleet_storage::StorageDeposited")
        assert int(deposited["pool_size"]) == size and int(purchased["cost"]) > 0, (purchased, deposited)
        assert int(purchased["wal_balance"]) == expected_wal - int(purchased["cost"]), purchased
        start_epoch, end_epoch = int(deposited["start_epoch"]), int(deposited["end_epoch"])
        r = live.transaction("extend the pool with the fleet's WAL", "--move-call", call("fleet_storage", "extend_pool", storage_package), f"@{storage}",
                             f"@{budget}", f"@{budget_cap}", f"@{SYSTEM}", "1u32", budget=30_000_000)
        extended = event(r, "::fleet_storage::StorageDeposited")
        assert int(extended["pool_size"]) == size and int(extended["end_epoch"]) == end_epoch + 1 and int(extended["start_epoch"]) == start_epoch, extended
        live.evidence["storage"] = dict(storage=storage, purchased=purchased, pool=extended, extension=event(r, "::fleet_storage::StoragePurchased"))
        live.check(f"bought {size} encoded bytes for epochs {start_epoch}-{end_epoch} with {purchased['cost']} FROST of the fleet's own WAL, "
                   f"then extended the pool to epoch {end_epoch + 1}; no WAL left the fleet object")
        r = live.transaction("device registers a fleet-paid blob", "--move-call", call("fleet_storage", "register_blob", storage_package), f"@{storage}",
                             f"@{budget}", f"@{account}", f"@{registry}", f"@{device_cap}", f"@{SYSTEM}", f"{encoded['blob_id_u256']}u256",
                             f"{encoded['root_hash_u256']}u256", f"{encoded['size']}u64", "1u8", CLOCK, budget=15_000_000, sender=device)
        registered = event(r, "::fleet_storage::UploadRegistered")
        blob = registered["blob_object_id"]
        assert int(registered["encoded_size"]) == ENCODED and int(registered["pool_size"]) == size - ENCODED, registered
        assert int(registered["end_epoch"]) == end_epoch + 1, registered
        measured["register_blob"] = dict(r["effects"]["gasUsed"], net_mist=net_gas(r), write_fee_frost=int(registered["write_fee"]))
        upload.stdin.write(blob + "\n")
        upload.stdin.flush()
        certificate = json.loads(upload.stdout.readline())
        r = live.transaction("device certifies the fleet blob", "--move-call", call("fleet_storage", "certify_blob", storage_package), f"@{storage}",
                             f"@{SYSTEM}", f"@{blob}", u8_vector(certificate["signature"]), u8_vector(certificate["signers_bitmap"]),
                             u8_vector(certificate["message"]), budget=5_000_000, sender=device)
        measured["certify_blob"] = dict(r["effects"]["gasUsed"], net_mist=net_gas(r))
        upload.stdin.write("read\n")
        upload.stdin.flush()
        read = json.loads(upload.stdout.readline())
        upload.wait(timeout=60)
        assert read["read_matches"], read
        stored = live.object(blob)
        content = plain(stored["content"])
        assert content["certified_epoch"] is not None and content["deletable"] is False
        assert "ObjectOwner" in json.dumps(stored.get("owner")), stored.get("owner")
        live.evidence["upload"] = dict(blob_object=blob, blob_id=encoded["blob_id"], encoded=encoded, registered=registered,
                                       confirmations=certificate["confirmations"], nodes=certificate["nodes"], owner=stored.get("owner"),
                                       certified_epoch=content["certified_epoch"], end_epoch=content["storage"]["end_epoch"])
        live.check(f"the device registered and certified blob {encoded['blob_id']} from fleet storage: exactly its encoded size, "
                   f"non-deletable, owned by the fleet object, read back from {certificate['confirmations']} confirming nodes")
        live.attack("second upload above the period quota", "--move-call", call("fleet_storage", "register_blob", storage_package), f"@{storage}",
                    f"@{budget}", f"@{account}", f"@{registry}", f"@{device_cap}", f"@{SYSTEM}", "1u256", "1u256", "16u64", "1u8", CLOCK,
                    module="fleet_storage", function="register_blob", code=6, sender=device, budget=2_000_000)
        live.check("the device's second upload in the period aborted on its quota (6)")

        # ── The owner's release upload, through the dApp's own code ──
        result, digests = owner_upload(live, root, manifest, storage_package, storage, budget, budget_cap)
        info = result["result"]
        assert info["owner"] == "fleet" and result["read_matches"], result
        stored = live.object(info["blobObjectId"])
        content = plain(stored["content"])
        assert content["certified_epoch"] is not None and content["deletable"] is False and "ObjectOwner" in json.dumps(stored.get("owner"))
        assert int(content["storage"]["end_epoch"]) == end_epoch + 1 and int(content["storage"]["storage_size"]) == ENCODED, content["storage"]
        assert not plain(live.object(storage)["content"]).get("pool"), "the owner's upload used the pool's last space"
        live.evidence["owner_upload"] = dict(result, digests=digests, owner=stored.get("owner"), certified_epoch=content["certified_epoch"])
        live.check(f"the dApp's fleet-paid upload registered and certified owner blob {info['blobId']} from fleet storage (write fee "
                   f"{info['costFrost']} FROST), each transaction verified before signing: fleet-owned, non-deletable, read back")

        if getattr(args, "retention", False):
            from retention_worker import Worker
            # Restricted device identity is also the executor; it has no owner cap.
            gas_reserve = 5_000_000
            renewal_swap = 5_000_000
            renewal_wal = renewal_swap * int(rate["wal"]) // int(rate["sui"])
            live.transaction("configure bounded retention executor", "--move-call",
                             call("fleet_storage", "configure_retention", storage_package), f"@{storage}", f"@{budget}", f"@{budget_cap}",
                             f"@{device_address}", "6u32", f"{renewal_wal}u64", f"{gas_reserve}u64", "1000000u64", budget=15_000_000)
            live.transaction("fund isolated retention reserves", "--split-coins", "gas", f"[{renewal_swap}u64,{gas_reserve}u64]",
                             "--assign", "funds", "--move-call", f"{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal", f"@{EXCHANGE}", "funds.0",
                             "--assign", "wal", "--move-call", call("fleet_storage", "fund_retention", storage_package), f"@{storage}", "wal", "funds.1",
                             budget=20_000_000, funding=renewal_swap + gas_reserve)
            target = end_epoch + 2
            prepaid = live.transaction("authorize finite blob renewal", "--move-call", call("fleet_storage", "authorize_renewal", storage_package),
                             f"@{storage}", f"@{budget}", f"@{budget_cap}", f"@{blob}", f"{target}u32", f"{renewal_wal}u64",
                             "--move-call", call("fleet_storage", "prepay_renewal", storage_package),
                             f"@{storage}", f"@{budget}", f"@{budget_cap}", f"@{SYSTEM}", f"@{blob}", budget=15_000_000)
            live.evidence["retention_prepayment"] = dict(event(prepaid, "::fleet_storage::RenewalPrepaid"), digest=prepaid["digest"])
            worker = Worker({"chain": live.chain, "network": "testnet", "rpc": live.rpc_url,
                             "storage": storage, "package": storage_package, "storage_origin": storage_package,
                             "retention_origin": storage_package, "executor": device_address,
                             "executor_key_file": str(root / "device/test.key"), "system": SYSTEM,
                             "walrus_package": WALRUS_PACKAGE, "gas_budget": "10000000", "spend_ceiling": "10000000"}, root / "retention")
            def transport(action, value=None):
                if action != "submit":
                    return worker.call(action, value)
                live.assert_lock()
                def submit(reserved):
                    assert reserved == 10_000_000
                    return live.executor(device_config)("execute-signed-tx", "--tx-bytes", value["bytes"], "--signatures", value["signature"])
                r = live.execute("browser-independent restricted renewal worker", submit, gas=10_000_000, sender=device)
                renewed = event(r, "::fleet_storage::RenewalCompleted")
                assert int(renewed["end_epoch"]) == target and int(renewed["fee"]) == 0
                live.evidence["retention_renewal"] = dict(renewed, digest=r["digest"], net_gas_mist=net_gas(r))
                return {"status": "submitted"}
            worker.transport = transport
            worker.tick()
            wait_for(lambda: int(plain(live.object(blob)["content"])["storage"]["end_epoch"]) == target,
                     "renewal expiry not observed", timeout=60)
            worker.tick()
            assert worker.state["last_success"][dm.object_id(blob)]["end"] == target
            worker.tick()
            assert len([t for t in live.evidence["transactions"] if t["label"] == "browser-independent restricted renewal worker"]) == 1
            worker.lock.close()
            live.check("owner-run worker renewed before exclusive expiry with separate executor identity, verified effects and expiry; restart/no-op did not spend twice")

        # ── Cleanup: device removed (its reserve returns), unused deposit back, device swept ──
        r = live.transaction("remove the device; its reserve returns", "--move-call", call("fleet_budget", "remove_device"), f"@{budget}",
                             f"@{budget_cap}", f"@{account}", budget=20_000_000)
        assert int(event(r, "::fleet_budget::ReserveChanged")["amount"]) == RESERVE - DRAW - DAEMON_DRAW * funding["draws"]
        balance = plain(live.object(budget)["content"])["balance"]
        remaining = int(balance["value"] if isinstance(balance, dict) else balance)
        r = live.transaction("withdraw the unused deposit", "--move-call", call("fleet_budget", "withdraw"), f"@{budget}",
                             f"@{budget_cap}", f"{remaining}u64", "--assign", "back",
                             "--transfer-objects", "[back]", f"@{live.operator}", budget=20_000_000)
        assert int(event(r, "::fleet_budget::Withdrawn")["amount"]) == remaining
        live.evidence["withdrawn_mist"] = remaining
        recovered = sweep(live, seed, root, device[1])
        key_path.unlink()
        live.evidence["device_swept_mist"] = recovered
        live.check(f"withdrew {remaining} MIST unused deposit and swept {recovered} MIST from the device key")
    finally:
        if daemon and daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        live.finish(args.evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--retention", action="store_true", help="SF092 real worker renewal and effects/expiry checks")
    parser.add_argument("--wallet", required=True, help="the dedicated agent wallet's client.yaml")
    parser.add_argument("--max-spend-mist", type=int, default=600_000_000)
    parser.add_argument("--walrus-sdk", default="/tmp/sf083-walrus-sdk")
    parser.add_argument("--evidence", default="sf083-testnet-evidence.json")
    parser.add_argument("--build-sui", default="sui")
    parser.add_argument("--network-sui", default=os.environ.get("SUIFLEET_NETWORK_SUI", toolchain.NETWORK_DEFAULT))
    parser.add_argument("--adopt", nargs=3, metavar=("PACKAGE", "REGISTRY", "UPGRADE_CAP"),
                        help="cache a verified publication of these exact contracts that an earlier run failed to record")
    run(parser.parse_args())
