#!/usr/bin/env python3
"""Deploy the complete fleet stack on the hosted Testnet evaluation fleet (SF-173).

The evaluation fleet is the explicit `profiles/seal-testnet` deployment. This
tool brings it to the current contracts and records everything the hosted
console needs in that profile:

1. upgrades its sui_edge package in place under its UpgradeCap (fleet budget,
   fleet settings and every later module), keeping its registry and devices;
2. publishes the fleet_storage and fleet_data packages against that upgrade;
3. creates the fleet budget (with a SUI deposit), its fleet settings, the
   prepaid Walrus storage (with WAL swapped from SUI at Walrus's pinned
   Testnet exchange) and the fleet-data index with its health cadence;
4. regenerates deployment.json, fleet-storage.json, fleet-data.json,
   frontend.env and the public evaluation-fleet.json record.

Every step is keyed by its content (contract source digests and compiler, or
the objects it binds) and checkpointed beside the wallet before and after it
executes. A completed step is re-verified on chain with free reads and reused,
so identical code is never republished and objects are never recreated. A
step whose outcome is unknown stops the run until it is reconciled with
`--adopt STEP=DIGEST` (its executed effects are read back, never resubmitted).

`--dry-run` reads only: chain, account, profile, UpgradeCap and cached steps,
and builds and verifies the unsigned upgrade (Sui's compatibility check)
without signing. `--top-up-sui`/`--top-up-wal-sui` add funds to an existing
evaluation fleet. Testnet and the dedicated wallets only; no Mainnet, no
production credentials. Mutations need the network:suifleet-testnet lock.
See docs/TESTNET.md#sf-173-hosted-evaluation-fleet.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import deployment_manifest as dm
import sui_toolchain as toolchain
from live_common import Live, ROOT, CLOCK, WALRUS_TESTNET, plain, stage
from live_fleet_budget import record_publication, net_gas, EXCHANGE_PACKAGE, EXCHANGE
from live_fleet_data import source_key

PROFILE = ROOT / "profiles/seal-testnet"
STATE = "sf173-state.json"
# Defaults: the dApp's own budget limits and storage quota (lib/fleet-budget.ts),
# fleet-settings defaults with a 10-minute health report and fleet-data cadence.
LIMITS = dict(float_cap=60_000_000, period_ms=86_400_000, period_allowance=250_000_000, max_draws=6, min_draw_interval_ms=600_000)
LOW_BALANCE = 50_000_000
QUOTA = dict(period_ms=86_400_000, encoded_bytes=512 * 1024 * 1024, uploads=24, max_blob=8 * 1024 * 1024, write_fees=100_000_000)
SETTINGS = dict(check_interval_ms=1_800_000, rollout_check_interval_ms=60_000, health_interval_ms=600_000,
                archive_interval_ms=604_800_000, archive_retention_epochs=3)
DATA_CADENCE = dict(health_interval_ms=600_000, batch_interval_ms=86_400_000, emergency_interval_ms=300_000)
DEPOSIT, SWAP = 100_000_000, 20_000_000
MAX_SPEND = 1_200_000_000


def core_key(compiler):
    """The sui_edge sources/lock and the release compiler that builds them."""
    digest = hashlib.sha256()
    for path in sorted([*(ROOT / "contracts/sources").glob("*.move"), ROOT / "contracts/Move.toml", ROOT / "contracts/Move.lock"]):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return f"{digest.hexdigest()[:24]}@{compiler}"


def balance_value(value):
    return int(value["value"] if isinstance(value, dict) else value)


class Reconcile(RuntimeError):
    """A step may have executed; read its effects before doing anything else."""


class EvaluationLive(Live):
    def client(self, *command, **options):
        deployment = getattr(self, "deployment", None)
        if deployment and command[0] == "execute-signed-tx":
            tx = command[command.index("--tx-bytes") + 1]
            deployment.record_submission(self.decode(tx)["digest"])
        return super().client(*command, **options)


class Deployment:
    def __init__(self, live, args):
        self.live, self.args = live, args
        live.deployment = self
        self.active_step = None
        self.profile = args.profile.resolve()
        self.base = dm.validate(json.loads((self.profile / "deployment.json").read_text()))
        self.seal = json.loads((self.profile / "seal.json").read_text())
        self.registry = dm.object_id(self.base["shared_objects"]["registry"]["id"])
        self.cap = dm.object_id(self.base["upgrade_cap_id"])
        self.state_path = live.wallet.parent / STATE
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        namespace = f"{live.chain}:{live.operator}:{self.registry}"
        self.fleet = self.state.setdefault(namespace, {"steps": {}, "pending": {}})
        # A committed public record restores the cache on another machine;
        # every reused step is still re-verified on chain before use.
        record = self.profile / "evaluation-fleet.json"
        if record.exists():
            public = json.loads(record.read_text())
            if (public.get("chain"), public.get("operator"), public.get("registry")) == (live.chain, live.operator, self.registry):
                for name, step in public.get("steps", {}).items():
                    self.fleet["steps"].setdefault(name, step)

    def save(self):
        if self.args.dry_run:
            return
        # Persist intent before signing; a torn checkpoint must never turn an
        # uncertain publication into an apparently missing step on restart.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.state_path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(self.state, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, self.state_path)
                fd = os.open(self.state_path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)

    def step(self, name, run, extract, verify):
        """Run `run` at most once for `name`; reuse a completed, re-verified step."""
        steps, pending = self.fleet["steps"], self.fleet["pending"]
        if name in steps:
            verify(steps[name])
            self.live.check(f"reused checkpoint {name}; no mutation")
            return steps[name]
        if name in pending:
            digest = self.args.adopt.get(name)
            if not digest:
                raise Reconcile(f"step {name} may have executed (pending since {pending[name]['started']}); find its "
                                f"transaction in the run evidence or wallet history and rerun with --adopt {name}=DIGEST")
            if digest != pending[name].get("digest"):
                raise Reconcile("adoption must match the exact transaction checkpointed before submission; "
                                "a legacy intent without a digest requires operator reconciliation")
            result = self.live.executed(digest)
            if not result or result["effects"]["status"]["status"] != "success":
                raise Reconcile(f"{digest} is not a successful executed transaction for {name}")
            self.live.evidence.setdefault("adopted", {})[name] = digest
        elif self.args.dry_run:
            self.live.evidence.setdefault("planned", []).append(name)
            print("PLAN:", name)
            return None
        else:
            pending[name] = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.save()
            self.active_step = name
            try:
                result = run()
            except toolchain.NotSubmitted:
                # This exception is reserved for verified pre-sign refusals.
                # Unknown submissions keep their intent and cannot be replayed.
                pending.pop(name)
                self.save()
                raise
            finally:
                self.active_step = None
        done = dict(extract(result), digest=result["digest"], net_gas_mist=net_gas(result))
        steps[name] = done
        pending.pop(name, None)
        self.save()
        verify(done)
        return done

    def record_submission(self, digest):
        if self.active_step is None:
            # Top-ups deliberately are new transfers, outside cached setup.
            raise Reconcile("uncached mutation refused; use an explicit checkpointed top-up step")
        pending = self.fleet["pending"][self.active_step]
        if pending.get("digest") not in (None, digest):
            raise Reconcile("cannot replace a checkpointed transaction with different bytes")
        pending["digest"] = digest
        self.save()

    # ── Free on-chain checks of reused steps ─────────────────────────────────

    def package_cap(self, package, cap, version="1"):
        facts = dm.chain_facts(self.live.rpc_url, package, cap)
        assert facts["objects"][dm.object_id(package)]["kind"] == "package"
        found = dm.upgrade_cap(facts["objects"][dm.object_id(cap)])
        assert found[:2] == (dm.object_id(package), version), f"UpgradeCap {cap} no longer identifies {package}"
        assert dm.object_id(found[2]) == self.live.operator, f"UpgradeCap {cap} is not the operator's"

    def shared(self, object_id, suffix):
        found = self.live.object(object_id)
        assert "Shared" in (found.get("owner") or {}), f"{object_id} is not shared"
        assert str(found.get("objType", found.get("type", ""))).endswith(suffix), f"{object_id} is not a {suffix}"

    def owned(self, object_id, suffix):
        found = self.live.object(object_id)
        assert dm.object_id((found.get("owner") or {}).get("AddressOwner", "0x0")) == self.live.operator, f"{object_id} is not the operator's"
        assert str(found.get("objType", found.get("type", ""))).endswith(suffix), f"{object_id} is not a {suffix}"

    # ── Steps ────────────────────────────────────────────────────────────────

    def core(self, tree):
        live, base = self.live, self.base
        key = core_key(live.tools["build"]["version"].split()[-1])
        stage(ROOT / "contracts", tree)
        facts = dm.chain_facts(live.rpc_url, self.cap)
        current, version, owner = dm.upgrade_cap(facts["objects"][self.cap])
        assert dm.object_id(owner) == live.operator, "the evaluation fleet's UpgradeCap is not the operator's"
        name = f"core:{key}"
        if name not in self.fleet["steps"] and name not in self.fleet["pending"]:
            # Only a cap still at the profile's recorded package may be upgraded:
            # anything else was changed outside this tool and needs reconciliation.
            known = {s["package"]: s.get("version") for k, s in self.fleet["steps"].items() if k.startswith("core:")}
            known[dm.object_id(base["call_package_id"])] = base["package_version"]
            assert current in known and known[current] == version, f"UpgradeCap now points at {current} v{version}, not a recorded release"
        pubfile = tree.parent / "Pub.upgrade.toml"
        toolchain.write_pubfile(pubfile, live.chain, tree, current, self.cap, version=int(version))
        if self.args.dry_run and name not in self.fleet["steps"]:
            self.check_upgrade(tree, pubfile, current, version)

        def run():
            return live.publish("upgrade the evaluation fleet's sui_edge to current contracts", tree, pubfile,
                                upgrade={"cap": self.cap, "package": current})

        def extract(result):
            return {"package": dm.object_id(live.published(result)), "version": str(int(version) + 1), "previous": current}

        def verify(step):
            self.package_cap(step["package"], self.cap, step["version"])
        return self.step(name, run, extract, verify)

    def check_upgrade(self, tree, pubfile, current, version):
        """Free: compile, build the unsigned upgrade (the CLI's compatibility
        check against the live package) and verify its bytecode. Not signed."""
        live = self.live
        built = toolchain.compile_package(live.build_sui, tree)
        # Any budget the balance covers builds the same commands; leave margin
        # for concurrent readers of the address balance.
        budget = min(live.start_balance // 2, 400_000_000)
        tx_bytes = toolchain._serialized(live.client, ["test-upgrade", tree, "--upgrade-capability", self.cap, "--build-env", "testnet",
                                                       "--pubfile-path", pubfile, "--silence-warnings"], budget)
        checked = toolchain.verify_package_transaction(live.decode(tx_bytes), built, sender=live.operator, budget=budget,
                                                       upgrade={"cap": self.cap, "package": current})
        live.evidence["upgrade_dry_run"] = dict(checked, digest=bytes(built["digest"]).hex(), from_package=current, from_version=version)
        live.check(f"unsigned upgrade of {current[:12]}… v{version} built, compatibility-checked and bytecode-verified; not signed")

    def packages(self, tree, core):
        live = self.live
        original = self.base["type_origins"]["device_registry::DeviceRegistry"]
        record_publication(tree, live.chain, original, self.cap, published_at=core["package"], version=int(core["version"]))
        storage_dir, data_dir = tree / "fleet_storage", tree / "fleet_data"

        def publication(directory, label):
            def publish():
                execute = live.executor()
                def client(*command, **options):
                    if command[0] == "execute-signed-tx":
                        return execute(*command)
                    return live.client(*command, **options)
                def submit(gas):
                    # Walrus dependencies use Git sources; the CLI's alternate
                    # pubfile mode only accepts local dependency sources.
                    try:
                        built = toolchain.compile_package(live.build_sui, directory)
                        pinned = (directory / "Move.lock").read_bytes()
                        tx = toolchain._serialized(client, ["publish", directory, "--silence-warnings"], gas)
                        if (directory / "Move.lock").read_bytes() != pinned:
                            raise toolchain.NotSubmitted("transaction compiler changed Move.lock")
                        decoded = live.decode(tx)
                        checked = toolchain.verify_package_transaction(decoded, built, sender=live.operator, budget=gas)
                    except (RuntimeError, ValueError, KeyError) as error:
                        raise toolchain.NotSubmitted(str(error)) from error
                    live.evidence.setdefault("verified_package_transactions", {})[label] = checked
                    return toolchain.execute_exact(client, live.signer(None, live.operator), tx, decoded["digest"])
                return live.execute(label, submit, gas=300_000_000, require_full=True)
            return (publish,
                    lambda r: {"package": dm.object_id(live.published(r)), "cap": live.created(r, "::package::UpgradeCap")[0]},
                    lambda step: self.package_cap(step["package"], step["cap"]))
        storage = self.step(f"fleet_storage:{source_key(ROOT / 'contracts/fleet_storage', core['package'])}",
                            *publication(storage_dir, "publish the evaluation fleet's fleet_storage"))
        if storage is None:
            return None, None
        record_publication(storage_dir, live.chain, storage["package"], storage["cap"])
        data = self.step(f"fleet_data:{source_key(ROOT / 'contracts/fleet_data', storage['package'])}",
                         *publication(data_dir, "publish the evaluation fleet's fleet_data"))
        return storage, data

    def objects(self, core, storage, data):
        live, call = self.live, (lambda pkg, module, fn: f"{pkg}::{module}::{fn}")
        pkg = core["package"]
        budget = self.step(f"budget:{self.registry}", lambda: live.transaction(
            "create the evaluation fleet budget with a deposit", "--split-coins", "gas", f"[{self.args.deposit_mist}u64]", "--assign", "deposit",
            "--move-call", call(pkg, "fleet_budget", "new_limits"), *(f"{LIMITS[k]}u64" for k in LIMITS), "--assign", "limits",
            "--move-call", call(pkg, "fleet_budget", "create"), f"@{self.registry}", "deposit", "limits", f"{LOW_BALANCE}u64", "--assign", "cap",
            "--transfer-objects", "[cap]", f"@{live.operator}", budget=30_000_000, funding=self.args.deposit_mist),
            lambda r: {"budget": live.created(r, "::fleet_budget::FleetBudget")[0], "cap": live.created(r, "::fleet_budget::BudgetOwnerCap")[0]},
            lambda s: (self.shared(s["budget"], "::fleet_budget::FleetBudget"), self.owned(s["cap"], "::fleet_budget::BudgetOwnerCap")))
        if budget is None:
            return None
        settings_args = [f"{SETTINGS[k]}u64" for k in list(SETTINGS)[:4]] + [f"{SETTINGS['archive_retention_epochs']}u32"]
        settings_key = hashlib.sha256(json.dumps(SETTINGS, sort_keys=True).encode()).hexdigest()[:12]
        self.step(f"settings:{budget['budget']}:{settings_key}", lambda: live.transaction(
            "save the evaluation fleet's settings", "--move-call", call(pkg, "fleet_settings", "set"), f"@{budget['budget']}", f"@{budget['cap']}",
            *settings_args, CLOCK, budget=15_000_000),
            lambda r: {"settings": SETTINGS}, lambda s: None)
        spkg = storage["package"]
        fleet_storage = self.step(f"storage:{spkg}:{budget['budget']}", lambda: live.transaction(
            "create the evaluation fleet's prepaid Walrus storage", "--move-call", call(spkg, "fleet_storage", "new_quota"), *(f"{QUOTA[k]}u64" for k in QUOTA),
            "--assign", "quota", "--move-call", call(spkg, "fleet_storage", "create"), f"@{budget['budget']}", f"@{budget['cap']}", "quota", budget=20_000_000),
            lambda r: {"storage": live.created(r, "::fleet_storage::FleetStorage")[0]},
            lambda s: self.shared(s["storage"], "::fleet_storage::FleetStorage"))
        self.step(f"initial-wal:{fleet_storage['storage']}", lambda: self.swap(spkg, fleet_storage["storage"], budget, self.args.swap_mist,
                                                                                "fund the evaluation fleet's storage with WAL"),
                  lambda r: {"swap_mist": self.args.swap_mist}, lambda s: None)
        dpkg = data["package"]
        fleet = self.step(f"fleet_data:{dpkg}:{budget['budget']}:{fleet_storage['storage']}", lambda: live.transaction(
            "create and configure the evaluation fleet's data index", "--move-call", call(dpkg, "fleet_data", "create"), f"@{budget['budget']}", f"@{budget['cap']}",
            f"@{fleet_storage['storage']}", f'"{live.chain}"', budget=20_000_000),
            lambda r: {"fleet": live.created(r, "::fleet_data::FleetData")[0]},
            lambda s: self.shared(s["fleet"], "::fleet_data::FleetData"))
        cadence_key = hashlib.sha256(json.dumps(DATA_CADENCE, sort_keys=True).encode()).hexdigest()[:12]
        self.step(f"data-cadence:{fleet['fleet']}:{cadence_key}", lambda: live.transaction(
            "set the evaluation fleet's health/batch/emergency cadence", "--move-call", call(dpkg, "fleet_data", "configure"), f"@{fleet['fleet']}",
            f"@{budget['budget']}", f"@{budget['cap']}", *(f"{DATA_CADENCE[k]}u64" for k in DATA_CADENCE), budget=15_000_000),
            lambda r: {"cadence": DATA_CADENCE}, lambda s: None)
        return dict(budget=budget["budget"], budget_cap=budget["cap"], storage=fleet_storage["storage"], fleet=fleet["fleet"])

    def swap(self, spkg, storage, budget, amount, label):
        """SUI→WAL at Walrus's pinned Testnet exchange, deposited into storage;
        aborts unless at least the posted fixed rate arrives."""
        rate = plain(self.live.object(EXCHANGE)["content"])["rate"]
        minimum = amount * int(rate["wal"]) // int(rate["sui"])
        return self.live.transaction(label, "--split-coins", "gas", f"[{amount}u64]", "--assign", "sui",
            "--move-call", f"{EXCHANGE_PACKAGE}::wal_exchange::exchange_all_for_wal", f"@{EXCHANGE}", "sui", "--assign", "wal",
            "--move-call", f"{spkg}::fleet_storage::deposit_wal_at_least", f"@{storage}", f"@{budget['budget']}", f"@{budget['cap']}",
            "wal", f"{minimum}u64", budget=20_000_000, funding=amount)

    def top_up(self, objects, storage_package):
        live, budget = self.live, {"budget": objects["budget"], "cap": objects["budget_cap"]}
        if self.args.top_up_sui:
            self.step(f"top-up-sui:{self.args.top_up_id}:{self.args.top_up_sui}", lambda: live.transaction("top up the evaluation fleet budget", "--split-coins", "gas", f"[{self.args.top_up_sui}u64]", "--assign", "coin",
                             "--move-call", f"{self.fleet['steps'][self.core_name]['package']}::fleet_budget::deposit", f"@{objects['budget']}",
                             f"@{objects['budget_cap']}", "coin", budget=15_000_000, funding=self.args.top_up_sui), lambda r: {}, lambda s: None)
        if self.args.top_up_wal_sui:
            self.step(f"top-up-wal:{self.args.top_up_id}:{self.args.top_up_wal_sui}",
                      lambda: self.swap(storage_package, objects["storage"], budget, self.args.top_up_wal_sui, "top up the evaluation fleet's WAL"),
                      lambda r: {}, lambda s: None)

    # ── Profile ──────────────────────────────────────────────────────────────

    def funding(self, objects):
        budget = plain(self.live.object(objects["budget"])["content"])
        storage = plain(self.live.object(objects["storage"])["content"])
        return {"budget_balance_mist": balance_value(budget["balance"]), "storage_wal_frost": balance_value(storage["wal"]),
                "operator_balance_mist": self.live.balance(), "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def write_profile(self, core, storage, data, objects):
        live = self.live
        manifest = dm.resolve(argparse.Namespace(chain_identifier=live.chain, rpc=self.base["rpc_url"], package=core["package"],
                                                 upgrade_cap=self.cap, registry=self.registry, network="testnet",
                                                 publisher=self.base["walrus"]["publisher_url"], aggregator=self.base["walrus"]["aggregator_url"]))
        missing = [name for name in dm.BUDGET_TYPES + dm.SETTINGS_TYPES if name not in manifest["type_origins"]]
        assert not missing, "upgraded package lacks " + ", ".join(missing)
        fleet_storage = {"packageId": storage["package"], "storageOrigin": storage["package"], "retentionOrigin": storage["package"]}
        fleet_data = dict(schema=1, chainIdentifier=live.chain, registryId=self.registry, packageId=data["package"], typeOrigin=data["package"],
                          fleetId=objects["fleet"], budgetId=objects["budget"], storageId=objects["storage"])
        compact = lambda value: json.dumps(value, separators=(",", ":"))
        pretty = lambda path, value: (self.profile / path).write_text(json.dumps(value, indent=2) + "\n")
        pretty("deployment.json", manifest)
        pretty("fleet-storage.json", fleet_storage)
        pretty("fleet-data.json", fleet_data)
        pretty("seal-settings.json", {key: self.seal[key] for key in ("keyServers", "threshold")})
        (self.profile / "frontend.env").write_text(
            "# Separate disposable Seal TESTNET evaluation fleet. Explicit selection only. No credentials.\n"
            "# Generated by scripts/live_evaluation_fleet.py from this profile's JSON files (SF-173).\n"
            f"NEXT_PUBLIC_DEPLOYMENT_MANIFEST='{compact(manifest)}'\n"
            f"NEXT_PUBLIC_SEAL_KEY_SERVERS='{compact(self.seal['keyServers'])}'\n"
            f"NEXT_PUBLIC_SEAL_THRESHOLD={self.seal['threshold']}\n"
            f"NEXT_PUBLIC_FLEET_STORAGE_PACKAGE={fleet_storage['packageId']}\n"
            f"NEXT_PUBLIC_FLEET_STORAGE_ORIGIN={fleet_storage['storageOrigin']}\n"
            f"NEXT_PUBLIC_FLEET_RETENTION_ORIGIN={fleet_storage['retentionOrigin']}\n"
            f"NEXT_PUBLIC_FLEET_DATA='{compact(fleet_data)}'\n")
        record = dict(schema=1, chain=live.chain, operator=live.operator, registry=self.registry, upgrade_cap=self.cap,
                      packages=dict(core=core, fleet_storage=storage, fleet_data=data), objects=objects,
                      configuration=dict(limits=LIMITS, low_balance_mist=LOW_BALANCE, quota=QUOTA, settings=SETTINGS, data_cadence=DATA_CADENCE),
                      funding=self.funding(objects), steps=self.fleet["steps"])
        pretty("evaluation-fleet.json", record)
        live.evidence.update(profile=record)
        live.check("profile deployment.json, fleet-storage.json, fleet-data.json, frontend.env and evaluation-fleet.json regenerated")

    def run(self):
        with tempfile.TemporaryDirectory(prefix="sf173-") as temporary:
            tree = Path(temporary) / "contracts"
            core = self.core(tree)
            if core is None:
                self.live.check("dry run: core upgrade planned; later steps depend on its package ID")
                return
            self.core_name = next(k for k, s in self.fleet["steps"].items() if s is core)
            storage, data = self.packages(tree, core)
            if storage is None or data is None:
                return
            objects = self.objects(core, storage, data)
            if objects is None:
                return
            if self.args.dry_run:
                self.live.evidence["funding"] = self.funding(objects)
                self.live.check("dry run: every step is complete and re-verified; nothing to do")
                return
            self.top_up(objects, storage["package"])
            self.write_profile(core, storage, data, objects)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wallet", required=True, help="dedicated Testnet wallet client.yaml")
    p.add_argument("--profile", type=Path, default=PROFILE)
    p.add_argument("--max-spend-mist", type=int, default=MAX_SPEND, help="strict non-replenishing run allowance")
    p.add_argument("--deposit-mist", type=int, default=DEPOSIT, help="initial fleet budget deposit")
    p.add_argument("--swap-mist", type=int, default=SWAP, help="SUI swapped to WAL for the initial storage funding")
    p.add_argument("--top-up-sui", type=int, default=0, metavar="MIST", help="deposit this much SUI into the existing fleet budget")
    p.add_argument("--top-up-wal-sui", type=int, default=0, metavar="MIST", help="swap this much SUI to WAL into the existing fleet storage")
    p.add_argument("--top-up-id", help="unique checkpoint label for this intentional top-up; reuse on retries")
    p.add_argument("--adopt", action="append", default=[], metavar="STEP=DIGEST", help="reconcile a pending step from its executed transaction")
    p.add_argument("--dry-run", action="store_true", help="read and verify only; build but never sign the upgrade")
    p.add_argument("--no-fund", action="store_true", help="never request the faucet")
    p.add_argument("--evidence", type=Path)
    args = p.parse_args(argv)
    args.adopt = dict(item.split("=", 1) for item in args.adopt)
    if (args.top_up_sui or args.top_up_wal_sui) and not args.top_up_id:
        p.error("top-ups require --top-up-id; reuse the same ID when resuming")
    if min(args.deposit_mist, args.swap_mist, args.top_up_sui, args.top_up_wal_sui) < 0:
        p.error("funding amounts cannot be negative")
    if not args.dry_run and not os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
        raise SystemExit("mutations need the network:suifleet-testnet lock token in SUIFLEET_TESTNET_LOCK_TOKEN")
    os.umask(0o077)
    live = EvaluationLive(args.wallet, "testnet", 1 if args.dry_run else args.max_spend_mist)
    live.preflight(auto_fund=not (args.dry_run or args.no_fund))
    try:
        Deployment(live, args).run()
    finally:
        live.finish(args.evidence)


if __name__ == "__main__":
    main()
