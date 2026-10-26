"""Offline tests for scripts/release_plan.py and its admission gate in
scripts/publish.py (SF-154): a fake Sui CLI and fake gRPC chain facts, as in
test_publish.py. Nothing here touches a network, signs or spends gas."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deployment_manifest as dm
import publish
import release_plan as rp
import test_publish
from test_publish import CHAIN, ME, OTHER, Chain, Cli, oid, write_published

ROOT = Path(__file__).resolve().parents[2]
MAINNET, TESTNET = "35834a8a", "4c78adac"
AGENT = "0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965"


def module_sources(types):
    modules = {}
    for name in types:
        module, datatype = name.split("::")
        modules.setdefault(module, []).append(datatype)
    return {m: f"module sui_edge::{m} {{\n" + "".join(f"    public struct {t} has drop {{}}\n" for t in names) + "}\n"
            for m, names in modules.items()}


class Base(unittest.TestCase):
    env, chain_id = "dev", CHAIN

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="release-plan-test-"))
        self.package = self.tmp / "contracts"
        (self.package / "sources").mkdir(parents=True)
        declared = "" if self.env in ("testnet", "mainnet") else f'{self.env} = "{self.chain_id}"\n'
        (self.package / "Move.toml").write_text(f'[package]\nname = "sui_edge"\nedition = "2024.beta"\n\n[environments]\n{declared}')
        for module, text in module_sources(dm.REQUIRED_TYPES).items():
            (self.package / f"sources/{module}.move").write_text(text)
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "contracts")
        self.records = self.tmp / "deployments"
        self.env_file = self.tmp / ".env.local"
        self.env_file.write_text("NEXT_PUBLIC_WALRUS_EPOCHS=7\n")
        self.chain = Chain(self.chain_id)
        self.cli = Cli(self, env=self.env)
        self.now = self.publish_time = 1_800_000_000
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.package), *args], check=True, capture_output=True)

    def plan(self, *extra, network="localnet", custody=ME):
        args = rp.parser().parse_args([
            "plan", "--network", network, "--rpc", "http://127.0.0.1:9000", "--custody", custody, "--env", self.env,
            "--package", str(self.package), "--records", str(self.records), "--out", str(self.tmp / "unused"), *extra])
        return rp.plan(args, self.chain, now=lambda: self.now)

    def publish_args(self, plan=None, *extra, network="localnet"):
        options = ["--expect-address", ME, "--publisher", "http://127.0.0.1:31415", "--aggregator", "http://127.0.0.1:31416",
                   "--package", str(self.package), "--records", str(self.records), "--frontend-env", str(self.env_file),
                   "--device-template", str(ROOT / "suifleet-edge/config.example.toml"), *extra]
        if network not in ("testnet", "mainnet"):
            options += ["--network", network]
        if plan is not None:
            path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*')))}.json"
            path.write_text(json.dumps(plan))
            options += ["--plan", str(path)]
        return publish.parser().parse_args(options)

    def run_publish(self, plan=None, *extra, network="localnet"):
        with unittest.mock.patch.object(rp.time, "time", return_value=self.publish_time):
            return publish.run(self.publish_args(plan, *extra, network=network), self.cli, self.chain)

    def transactions(self):
        return [c for c in self.cli.calls if c[0] in ("publish", "upgrade")]

    def assert_refused(self, plan, pattern, *extra, network="localnet"):
        before = self.transactions()
        with self.assertRaisesRegex(publish.PublishError, pattern):
            self.run_publish(plan, *extra, network=network)
        self.assertEqual(self.transactions(), before, "no transaction may be attempted")

    def codes(self, plan, severity="blocking"):
        return {f["code"] for f in plan["findings"] if f["severity"] == severity}



class LocalPlanTests(Base):
    def test_fresh_publish_plan_is_ready_reviewable_and_admits_its_transaction(self):
        plan = self.plan()
        self.assertTrue(plan["ready"], plan["findings"])
        self.assertEqual((plan["action"], plan["chain_identifier"], plan["custody"]["address"]), ("publish", CHAIN, ME))
        self.assertTrue(plan["execution"]["allowed"])
        self.assertIn("--plan PLAN.json", plan["execution"]["commands"][1])
        self.assertEqual(plan["plan_digest"], rp.digest_of(plan))
        self.assertEqual(plan["revision"]["contracts_source_digest"], rp.source_digest(self.package))
        text = rp.markdown(plan)
        for heading in ("Abort and rollback boundaries", "Rollout", "Outstanding production checks", "Cost and exposure"):
            self.assertIn(heading, text)
        release = self.run_publish(plan)
        recorded = json.loads((release / "release-plan.json").read_text())
        self.assertEqual(recorded["plan_digest"], plan["plan_digest"])
        transaction = json.loads((release / "transaction.json").read_text())
        self.assertEqual((transaction["digest"], transaction["effects"]["status"]), ("Tx1", {"status": "success"}))

    def test_upgrade_plan_pins_deployment_custody_and_origins(self):
        self.run_publish(self.plan())
        plan = self.plan()
        self.assertTrue(plan["ready"], plan["findings"])
        deployment = plan["deployment"]
        self.assertEqual((plan["action"], deployment["call_package_id"], deployment["package_version"],
                          deployment["upgrade_cap_id"], deployment["upgrade_cap_owner"], deployment["registry_id"]),
                         ("upgrade", oid(0x100), 1, oid(0xc0), ME, oid(0xbe)))
        self.assertEqual(set(deployment["type_origins"]), set(dm.REQUIRED_TYPES))
        self.assertTrue(plan["clients"]["generated"])
        self.assertEqual(plan["clients"]["files"], ["config.toml", "deployment.json", "frontend.env"])
        self.assertEqual(plan["compatibility"]["new_types"], [])
        self.cli.calls.clear()
        self.run_publish(plan)
        self.assertEqual(self.transactions()[0][:3], ["upgrade", "--upgrade-capability", oid(0xc0)])

    def test_recovered_release_keeps_the_plan_that_admitted_it(self):
        plan = self.plan()
        self.cli.write_published = False
        with self.assertRaisesRegex(publish.PublishError, "EXECUTED"):
            self.run_publish(plan)
        pending = next((self.records / "dev").glob("pending-*.json"))
        self.assertEqual(json.loads(pending.read_text())["plan"]["plan_digest"], plan["plan_digest"])
        write_published(self.package, "dev", CHAIN, oid(0x100), oid(0x100), 1, oid(0xc0))
        release = publish.run(self.publish_args(None, "--recover", str(pending)), self.cli, self.chain)
        self.assertEqual(json.loads((release / "release-plan.json").read_text())["plan_digest"], plan["plan_digest"])

    def test_replayed_or_superseded_plan_is_refused(self):
        fresh = self.plan()
        self.run_publish(fresh)
        self.assert_refused(fresh, "plan is a publish, but this run would upgrade")
        upgrade = self.plan()
        self.run_publish(upgrade)
        self.assert_refused(upgrade, "deployment changed since planning")

    def test_tampered_expired_or_unready_plans_are_refused(self):
        plan = self.plan()
        edited = json.loads(json.dumps(plan)); edited["cost"]["gas_budget_mist"] *= 10
        self.assert_refused(edited, "digest does not match")
        self.now -= 10 * 86400
        self.assert_refused(self.plan(), "expired")
        self.now += 10 * 86400
        self.assert_refused(self.plan("--gas-budget", "10000000000"), "not ready .*budget-exceeds-policy")

    def test_sources_budget_or_account_differing_from_the_plan_are_refused(self):
        plan = self.plan("--gas-budget", "100")
        self.assert_refused(plan, "exceeds the planned 100", "--gas-budget", "101")
        self.assert_refused(self.plan(custody=OTHER), "plan is for")
        (self.package / "sources/roles.move").write_text("module sui_edge::roles { public struct Extra has drop {} }\n")
        self.assert_refused(plan, "contract sources differ")

    def test_dirty_sources_need_explicit_scratch_planning(self):
        (self.package / "sources/extra.move").write_text("module sui_edge::extra { public struct Extra has drop {} }\n")
        with self.assertRaisesRegex(rp.PlanError, "uncommitted contract sources"):
            self.plan()
        plan = self.plan("--allow-dirty")
        self.assertTrue(plan["ready"])
        self.assertIn("uncommitted-sources", self.codes(plan, "warning"))

    def test_compatibility_flags_removed_types_and_reviews_new_ones(self):
        self.run_publish(self.plan())
        text = (self.package / "sources/roles.move").read_text()
        (self.package / "sources/roles.move").write_text(text.replace("    public struct RoleGranted has drop {}\n", ""))
        (self.package / "sources/fleet_settings.move").write_text(module_sources(dm.SETTINGS_TYPES)["fleet_settings"])
        self.git("add", "-A"); self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "change")
        plan = self.plan()
        self.assertFalse(plan["ready"])
        self.assertIn("type-removed", self.codes(plan))
        self.assertEqual(plan["compatibility"]["removed_types"], ["roles::RoleGranted"])
        self.assertEqual(plan["compatibility"]["new_types"], sorted(dm.SETTINGS_TYPES))
        self.assertEqual(plan["compatibility"]["new_optional_groups"], ["fleet settings"])
        self.assertEqual(len(plan["compatibility"]["migrations"]), len(dm.SETTINGS_TYPES))
        self.assertIn("migration-review", self.codes(plan, "warning"))
        self.assert_refused(plan, "not ready .*type-removed")

    def test_custody_and_stale_capability_block_the_plan(self):
        self.run_publish(self.plan())
        self.chain.cap(oid(0xc0), oid(0x100), 1, owner=OTHER)
        self.assertIn("custody-mismatch", self.codes(self.plan()))
        self.chain.objects[oid(0xc0)]["owner"] = {"shared": 5}
        self.assertIn("cap-custody", self.codes(self.plan()))
        self.chain.cap(oid(0xc0), oid(0x999), 2)
        plan = self.plan()
        self.assertIn("cap-stale", self.codes(plan))
        self.assertIn("manifest-stale", self.codes(plan))

    def test_recorded_and_published_disagreement_and_orphans_block(self):
        self.run_publish(self.plan())
        (self.package / "Published.toml").unlink()
        self.assertIn("orphan", self.codes(self.plan()))

    def test_identical_bytecode_to_the_current_release_is_flagged(self):
        digest = "ab" * 32
        self.run_publish(self.plan("--bytecode-digest", digest, "--compiler", "sui 1.81.1"))
        self.assertIn("identical-bytecode", self.codes(self.plan("--bytecode-digest", digest), "warning"))
        self.assertNotIn("identical-bytecode", self.codes(self.plan("--bytecode-digest", "cd" * 32), "warning"))

    def fake_compiler(self, body):
        tool = self.tmp / "sui"
        tool.write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo 'sui 9.9.9-test'; exit 0; }\n" + body)
        tool.chmod(0o755)
        return str(tool)

    def test_bytecode_is_compiled_from_a_scratch_copy(self):
        tool = self.fake_compiler('touch "$4/written"; echo \'{"modules":["AA=="],"dependencies":["0x2"],"digest":['
                                  + ",".join(["171"] * 32) + ']}\'\n')
        plan = self.plan("--build-tool", tool)
        self.assertEqual(plan["revision"]["bytecode"]["digest"], "ab" * 32)
        self.assertEqual(plan["revision"]["bytecode"]["compiler"], "sui 9.9.9-test")
        self.assertFalse((self.package / "written").exists(), "the checkout is never written")

    def test_compiler_failures_and_missing_lock_pins_block(self):
        failed = self.plan("--build-tool", self.fake_compiler("echo boom >&2; exit 1\n"))
        self.assertIn("bytecode-build", self.codes(failed))
        (self.package / "Move.lock").write_text("[move]\n")
        self.git("add", "-A"); self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "lock")
        unpinned = self.plan("--build-tool", self.fake_compiler('echo pin >> "$4/Move.lock"; echo \'{"modules":["AA=="],'
                                                                '"dependencies":[],"digest":[' + ",".join(["1"] * 32) + ']}\'\n'))
        detail = [f["detail"] for f in unpinned["findings"] if f["code"] == "bytecode-build"]
        self.assertTrue(detail and "lacks dependency pins" in detail[0], unpinned["findings"])

    def test_public_chain_cannot_be_labelled_local(self):
        self.chain.chain = TESTNET
        with self.assertRaisesRegex(rp.PlanError, "public chain"):
            self.plan()

    def test_policy_keeps_mainnet_execution_forbidden_and_limits_coherent(self):
        policy = json.loads(rp.POLICY.read_text())
        rp.load_policy(rp.POLICY)
        for mutate, pattern in (
                (lambda p: p["networks"]["mainnet"].update(execution="rehearsal"), "mainnet execution must stay forbidden"),
                (lambda p: p["networks"]["testnet"].update(max_gas_budget_mist=10 ** 12), "max_gas_budget_mist <="),
                (lambda p: p["rollout"]["stages"].pop(), "end at the whole fleet"),
                (lambda p: p["networks"].pop("devnet"), "cover")):
            changed = json.loads(json.dumps(policy)); mutate(changed)
            path = self.tmp / "policy.json"; path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(rp.PlanError, pattern):
                rp.load_policy(path)

    def test_cli_writes_plan_and_markdown_and_checks_them(self):
        out, md = self.tmp / "p.json", self.tmp / "p.md"
        with unittest.mock.patch.object(rp.dm, "chain_facts", self.chain):
            code = rp.main(["plan", "--network", "localnet", "--rpc", "http://127.0.0.1:9000", "--custody", ME,
                            "--env", "dev", "--package", str(self.package), "--records", str(self.records),
                            "--out", str(out), "--markdown", str(md)])
        self.assertEqual(code, 0)
        self.assertIn("plan_digest", json.loads(out.read_text()))
        self.assertIn("# SuiFleet release plan: publish on localnet", md.read_text())
        self.assertEqual(rp.main(["check", str(out)]), 0)
        value = json.loads(out.read_text()); value["ready"] = False; out.write_text(json.dumps(value))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rp.main(["check", str(out)]), 1)


class TestnetPlanTests(Base):
    env, chain_id = "testnet", TESTNET

    def setUp(self):
        super().setUp()
        # The fake CLI's extra [published.testnet] section would collide with this environment's own.
        record = test_publish.write_published
        patcher = unittest.mock.patch.object(test_publish, "write_published", lambda *a: record(*a, other=False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_publication_requires_a_plan_but_a_dry_run_does_not(self):
        self.assert_refused(None, "needs a reviewed release plan", network="testnet")
        self.run_publish(None, "--dry-run", network="testnet")

    def test_bytecode_must_be_pinned_and_identical_cached_bytecode_reused(self):
        self.assertIn("bytecode-unpinned", self.codes(self.plan(network="testnet")))
        digest = "6e" * 32
        state = self.tmp / "live-state.json"
        state.write_text(json.dumps({"chain": TESTNET, "fixtures": {"a..b@1.81.1": {
            "base_package": oid(0x50), "head_package": oid(0x51), "upgrade_cap": oid(0x52), "registry": oid(0x53),
            "provenance": {"verified_transactions": {"upgrade": {"digest": digest}}}}}}))
        cached = self.plan("--bytecode-digest", digest, "--fixture-state", str(state), network="testnet")
        self.assertIn("identical-bytecode", self.codes(cached))
        self.assertEqual(cached["revision"]["cached_publication"]["package"], oid(0x51))
        fresh = self.plan("--bytecode-digest", "11" * 32, "--fixture-state", str(state), network="testnet")
        self.assertTrue(fresh["ready"], fresh["findings"])
        self.run_publish(fresh, network="testnet")

    def test_adopted_fixture_upgrade_plan_reads_only(self):
        self.chain.package(oid(0x51), 2, oid(0x50))
        self.chain.cap(oid(0x52), oid(0x51), 2)
        self.chain.registry(oid(0x53), oid(0x50))
        plan = self.plan("--bytecode-digest", "11" * 32, "--package-id", oid(0x51), "--upgrade-cap", oid(0x52),
                         "--registry", oid(0x53), network="testnet")
        self.assertTrue(plan["ready"], plan["findings"])
        self.assertIn("adopt", self.codes(plan, "warning"))
        self.assertFalse(plan["execution"]["allowed"])
        self.assertIn("planning only", plan["execution"]["reason"])
        with self.assertRaisesRegex(rp.PlanError, "forbids execution"):
            rp.admit(plan, network="testnet", env="testnet", chain=TESTNET, address=ME, published=None, registry=None,
                     package=self.package, gas_budget=1, now=lambda: self.now)
        self.assertEqual(self.cli.calls, [], "planning never uses the signing CLI")

    def test_wrong_chain_for_label_is_refused(self):
        self.chain.chain = MAINNET
        with self.assertRaisesRegex(rp.PlanError, "not testnet"):
            self.plan(network="testnet")


class MainnetPlanTests(Base):
    env, chain_id = "mainnet", MAINNET

    def test_mainnet_plans_are_planning_only(self):
        plan = self.plan("--bytecode-digest", "11" * 32, network="mainnet")
        self.assertTrue(plan["ready"], plan["findings"])
        self.assertFalse(plan["execution"]["allowed"])
        self.assertEqual(plan["execution"]["commands"], [])
        self.assertIn("SF-087/#159", plan["execution"]["reason"])
        self.assertIn("never performed", plan["custody"]["transfer"])
        with self.assertRaisesRegex(rp.PlanError, "forbids execution"):
            rp.admit(plan, network="mainnet", env="mainnet", chain=MAINNET, address=ME, published=None, registry=None,
                     package=self.package, gas_budget=1, now=lambda: self.now)
        # publish.py's own mainnet refusal stays first, with or without a plan.
        self.assert_refused(plan, "mainnet publication is not supported", network="mainnet")
        self.assert_refused(plan, "mainnet publication is not supported", "--dry-run", network="mainnet")
        self.assertEqual(self.cli.calls and [c[0] for c in self.cli.calls if c[0] not in ("envs", "active-address")], [])

    def test_development_wallets_cannot_be_mainnet_custody(self):
        plan = self.plan("--bytecode-digest", "11" * 32, network="mainnet", custody=AGENT)
        self.assertIn("custody-forbidden", self.codes(plan))
        self.assertIn("bytecode-unpinned", self.codes(self.plan(network="mainnet")))
        self.assertIn("custody-unset", self.codes(self.plan("--bytecode-digest", "11" * 32, network="mainnet", custody="0x0")))


if __name__ == "__main__":
    unittest.main()
