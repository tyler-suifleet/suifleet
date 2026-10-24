"""Offline spend-accounting and fixture-reuse checks for the SF-010 live suites."""
import json
from contextlib import nullcontext
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_common
import live_upgrade_security
import live_wallet_gate
import sui_toolchain
from live_common import Live
from live_upgrade_security import fixture_valid
from testnet_budget import Budget, Spent

OPERATOR = "0x" + "0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965"
ABORT = ("MoveAbort(MoveLocation { module: ModuleId { address: ab, name: Identifier(\"device_cap\") }, function: 3, "
         "instruction: 9, function_name: Some(\"assert_signer\") }, 0) in command 0")


def effects(status="success", cost=1_000_000, storage=0, rebate=0, error=None, digest="Dig1111111111111111111111111111111111111111"):
    state = {"status": status, **({"error": error} if error else {})}
    return {"digest": digest, "objectChanges": [], "effects": {"status": state, "gasUsed": {
        "computationCost": str(cost), "storageCost": str(storage), "storageRebate": str(rebate)}}}


class Chain:
    """Scripted `sui client` responses; records every command and config."""

    def __init__(self, *responses, tx_blocks=None):
        self.responses, self.tx_blocks, self.calls = list(responses), tx_blocks or {}, []

    def __call__(self, *command, json_output=True, check=True, config=None):
        self.calls.append((command, config))
        if command[0] == "tx-block":
            return self.tx_blocks.get(command[1], CompletedProcess([], 1, "", "not found"))
        return self.responses.pop(0)


def live(chain, limit=100_000_000):
    run = Live("/nonexistent/client.yaml", "testnet", limit)
    run.client = chain
    run.operator, run.chain = OPERATOR, "4c78adac"  # Set by preflight.
    return run


def submitted_budget(chain):
    command, _ = chain.calls[0]
    return int(command[command.index("--gas-budget") + 1])


class LiveSpendTests(unittest.TestCase):
    def test_reserves_only_the_remaining_allowance(self):
        chain = Chain(effects(cost=1_000_000))
        run = live(chain)
        run.ledger.used = 90_000_000
        run.transaction("near the ceiling", "--move-call", "x")
        self.assertEqual(submitted_budget(chain), 10_000_000)  # Not the default 200,000,000.
        self.assertEqual(run.ledger.used, 91_000_000)

    def test_exhausted_allowance_never_submits(self):
        for used in (99_000_000, 100_000_000):
            chain = Chain()
            run = live(chain)
            run.ledger.used = used
            with self.assertRaises(Spent):
                run.transaction("over the ceiling", "--move-call", "x")
            with self.assertRaises(Spent):
                run.publish("publish", "/tmp/package", "/tmp/pub.toml")
            self.assertEqual(chain.calls, [])

    def test_rebates_never_replenish(self):
        chain = Chain(effects(cost=1_000_000, rebate=500_000_000), effects(cost=1_000_000))
        run = live(chain)
        run.transaction("delete with a large rebate", "--move-call", "x")
        self.assertEqual(run.ledger.used, 0)  # Net cost clamps at zero, never below.
        run.transaction("next", "--move-call", "x")
        self.assertEqual(run.ledger.remaining, 99_000_000)

    def test_failed_execution_is_charged_and_read_back_by_digest(self):
        digest = "Fazz111111111111111111111111111111111111111"
        failed = effects("failure", cost=2_000_000, error="InsufficientGas", digest=digest)
        chain = Chain(CompletedProcess([], 1, "", f"Error executing transaction '{digest}': failure"), tx_blocks={digest: failed})
        run = live(chain)
        with self.assertRaises(RuntimeError):
            run.transaction("fails on chain", "--move-call", "x")
        self.assertEqual(run.ledger.used, 2_000_000)
        self.assertEqual(run.evidence["transactions"][-1]["status"], "failure")

    def test_unknown_outcome_keeps_the_full_reservation(self):
        chain = Chain(CompletedProcess([], 1, "", "connection reset"))
        run = live(chain)
        with self.assertRaises(RuntimeError):
            run.transaction("unknown", "--move-call", "x", budget=30_000_000)
        self.assertEqual(run.ledger.used, 30_000_000)
        self.assertEqual(run.evidence["transactions"][-1]["status"], "unknown")

    def test_attack_charges_gas_and_reads_the_abort_from_effects(self):
        digest = "Abrt111111111111111111111111111111111111111"
        chain = Chain(CompletedProcess([], 1, "", f"transaction '{digest}' failed"),
                      tx_blocks={digest: effects("failure", cost=1_500_000, error=ABORT, digest=digest)})
        run = live(chain)
        entry = run.attack("impersonation", "--move-call", "x", module="device_cap", function="assert_signer", code=0)
        self.assertEqual(entry["abort"], ("device_cap", "assert_signer", "0"))
        self.assertEqual(entry["gas_mist"], 1_500_000)
        self.assertEqual(run.ledger.used, 1_500_000)

    def test_attack_that_succeeds_or_aborts_differently_fails(self):
        run = live(Chain(effects("success")))
        with self.assertRaises(RuntimeError):
            run.attack("should abort", "--move-call", "x", module="device_cap", function="assert_signer", code=0)
        run = live(Chain(effects("failure", error=ABORT.replace("}, 0)", "}, 1)"))))
        with self.assertRaises(AssertionError):
            run.attack("wrong code", "--move-call", "x", module="device_cap", function="assert_signer", code=0)

    def test_child_account_funding_is_charged_and_bounds_its_gas(self):
        chain = Chain(effects(cost=1_000_000), effects(cost=1_000_000))
        run = live(chain)
        run.transaction("fund the device", "--split-coins", "gas", "[40000000]", funding=40_000_000, budget=10_000_000)
        self.assertEqual(run.ledger.used, 41_000_000)
        device = run.child("/tmp/device.yaml", 40_000_000)
        run.transaction("device report", "--move-call", "x", budget=100_000_000, sender=device)
        command, config = chain.calls[1]
        self.assertEqual(config, "/tmp/device.yaml")
        self.assertEqual(int(command[command.index("--gas-budget") + 1]), 40_000_000)  # Capped by the child's funding.
        self.assertEqual(run.ledger.used, 41_000_000)  # Already charged as funding.
        device[1].used = 39_000_000
        with self.assertRaises(Spent):
            run.transaction("device over its funding", "--move-call", "x", sender=device)

    def test_funding_beyond_the_allowance_is_refused(self):
        chain = Chain()
        run = live(chain, limit=50_000_000)
        with self.assertRaises(Spent):
            run.transaction("fund", "--split-coins", "gas", "[100000000]", funding=100_000_000)
        self.assertEqual(chain.calls, [])

    def test_mutations_assert_the_testnet_lock_when_held(self):
        chain = Chain(effects())
        run = live(chain)
        with patch.dict("os.environ", {"SUIFLEET_TESTNET_LOCK_TOKEN": "t"}), \
                patch.object(live_common.subprocess, "run") as lock:
            run.transaction("locked", "--move-call", "x")
        self.assertIn("lock-assert", lock.call_args.args[0])


class PackageTransactionTests(unittest.TestCase):
    """Publish/upgrade go through sui_toolchain's verify-before-sign submitter."""

    def submitter(self, calls, refuse=False):
        def make(client, build_tool, package_dir, common, *, sender, upgrade=None, record=None):
            calls.append(dict(build_tool=build_tool, package_dir=package_dir, sender=sender, upgrade=upgrade))

            def submit(budget):
                if refuse:
                    raise sui_toolchain.NotSubmitted("bytecode differs")
                record.update(kind="upgrade" if upgrade else "publish")
                return client("serialized-tx", "VERIFIEDBYTES")
            return submit
        return make

    def published(self):
        result = effects(cost=5_000_000, storage=80_000_000)
        result["objectChanges"] = [{"type": "published", "packageId": "0x" + "a" * 64},
                                   {"type": "created", "objectType": "0x2::package::UpgradeCap", "objectId": "0xcap"}]
        return result

    def test_publish_signs_verified_bytes_and_records_the_pubfile(self):
        calls, chain = [], Chain(self.published())
        run = live(chain, limit=500_000_000)
        with patch.object(live_common.toolchain, "package_submitter", self.submitter(calls)), \
                patch.object(live_common.toolchain, "write_pubfile") as pubfile:
            run.publish("publish base", "/tmp/package", "/tmp/pub.toml")
        self.assertEqual(chain.calls[0][0][:2], ("serialized-tx", "VERIFIEDBYTES"))
        self.assertEqual(calls[0]["sender"], OPERATOR)
        pubfile.assert_called_once_with("/tmp/pub.toml", "4c78adac", "/tmp/package", "0x" + "a" * 64, "0xcap")
        self.assertEqual(run.ledger.used, 85_000_000)
        self.assertEqual(run.evidence["verified_package_transactions"]["publish base"], {"kind": "publish"})

    def test_upgrade_targets_the_cap_and_package_without_rewriting_the_pubfile(self):
        calls, chain = [], Chain(self.published())
        run = live(chain, limit=500_000_000)
        upgrade = {"cap": "0xcap", "package": "0xold"}
        with patch.object(live_common.toolchain, "package_submitter", self.submitter(calls)), \
                patch.object(live_common.toolchain, "write_pubfile") as pubfile:
            run.publish("upgrade to head", "/tmp/package", "/tmp/pub.toml", upgrade=upgrade)
        self.assertEqual(calls[0]["upgrade"], upgrade)
        pubfile.assert_not_called()

    def test_refused_package_transaction_is_never_submitted_or_charged(self):
        calls, chain = [], Chain()
        run = live(chain, limit=500_000_000)
        with patch.object(live_common.toolchain, "package_submitter", self.submitter(calls, refuse=True)), \
                patch.object(live_common.toolchain, "write_pubfile") as pubfile:
            with self.assertRaises(sui_toolchain.NotSubmitted):
                run.publish("publish base", "/tmp/package", "/tmp/pub.toml")
        self.assertEqual(chain.calls, [])
        self.assertEqual(run.ledger.used, 0)
        self.assertEqual(run.evidence["transactions"][-1]["status"], "not_submitted")
        pubfile.assert_not_called()

    def test_preflight_refuses_an_unpinned_transaction_cli_before_any_read(self):
        chain = Chain()
        run = live(chain)
        with patch.object(live_common.toolchain, "check_tools", side_effect=sui_toolchain.Incompatible("PATH sui")):
            with self.assertRaises(sui_toolchain.Incompatible):
                run.preflight()
        self.assertEqual(chain.calls, [])

    def test_client_runs_the_network_cli(self):
        run = Live("/nonexistent/client.yaml", "testnet", 1, network_sui="/opt/sui-network/sui")
        with patch.object(live_common.subprocess, "run", return_value=CompletedProcess([], 0, "{}", "")) as ran:
            run.client("gas")
        self.assertEqual(ran.call_args.args[0][:2], ["/opt/sui-network/sui", "client"])


class FleetPreflightFundingTests(unittest.TestCase):
    def test_one_pinned_faucet_credit_preserves_used_allowance(self):
        run = Live("/nonexistent/client.yaml", "testnet", 100)
        run.ledger.used = 7
        run.client = Mock(side_effect=[([{"alias": "testnet", "rpc": "https://example.test"}], "testnet"), CompletedProcess([], 0, OPERATOR, "")])
        run.balance = Mock(side_effect=[1, 200])
        run.assert_lock = Mock()
        tools = {"build": {"path": "/compiler", "version": "build"}, "network": {"path": "/pinned", "version": "network"}}
        with patch.object(live_common.toolchain, "check_tools", return_value=tools), patch.object(live_common.toolchain, "preflight", return_value={}), \
             patch.object(live_common.dm, "chain_facts", return_value={"chain_identifier": "4c78adac"}), \
             patch.object(live_common.subprocess, "run", return_value=CompletedProcess([], 0)), \
             patch.object(live_common.subprocess, "check_output", return_value="head"):
            run.preflight(auto_fund=True)
        run.assert_lock.assert_called_once()
        self.assertEqual(run.ledger.faucet_attempts, 1)
        self.assertEqual(run.ledger.used, 7)
        self.assertEqual(run.evidence["faucet"]["balance_after_mist"], 200)
        with self.assertRaises(Spent):
            run.ledger.faucet(lambda: self.fail("second faucet request"))

    def test_wrong_chain_cannot_request_funding(self):
        run = Live("/nonexistent/client.yaml", "testnet", 100)
        run.client = Mock(side_effect=[([{"alias": "testnet", "rpc": "https://example.test"}], "testnet"), CompletedProcess([], 0, OPERATOR, "")])
        with patch.object(live_common.toolchain, "check_tools", return_value={"build": {"path": "/compiler"}, "network": {"path": "/pinned"}}), \
             patch.object(live_common.dm, "chain_facts", return_value={"chain_identifier": "mainnet"}), \
             patch.object(live_common.subprocess, "run") as execute:
            with self.assertRaises(AssertionError): run.preflight(auto_fund=True)
        execute.assert_not_called()
        self.assertEqual(run.ledger.faucet_attempts, 0)


class WalrusUploadTests(unittest.TestCase):
    STORED = json.dumps({"newlyCreated": {"blobObject": {"blobId": "blob1"}}}).encode()

    def upload(self, *outcomes):
        import io
        import urllib.error
        attempts = []

        def urlopen(request, timeout):
            attempts.append(request.get_method())
            outcome = outcomes[len(attempts) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return nullcontext(io.BytesIO(outcome))
        walrus = live_common.Walrus("testnet")
        with patch.object(live_common.urllib.request, "urlopen", urlopen), \
                patch.object(live_common.time, "sleep"), patch.object(walrus, "readable", return_value=True):
            try:
                return walrus.put("recipe", b"body"), attempts
            except (OSError, urllib.error.HTTPError) as error:
                return error, attempts

    def test_transient_publisher_failures_are_retried(self):
        import urllib.error
        server = urllib.error.HTTPError("u", 503, "busy", {}, None)
        blob, attempts = self.upload(TimeoutError("read timed out"), server, self.STORED)
        self.assertEqual((blob, attempts), ("blob1", ["PUT"] * 3))

    def test_retries_are_bounded_and_client_errors_are_not_retried(self):
        import urllib.error
        error, attempts = self.upload(*[TimeoutError("read timed out")] * 3, self.STORED)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(len(attempts), live_common.WALRUS_ATTEMPTS)
        error, attempts = self.upload(urllib.error.HTTPError("u", 413, "too large", {}, None), self.STORED)
        self.assertEqual((error.code, len(attempts)), (413, 1))


class WalletFixtureTests(unittest.TestCase):
    def test_head_fixture_is_keyed_by_release_compiler(self):
        compiler = sui_toolchain.BUILD["version"].split()[1]
        same, pair = {"owner_cap": "0x1"}, {"owner_cap": "0x2"}
        self.assertIs(live_wallet_gate.head_fixture({"fixtures": {f"t@{compiler}": same}}, "t"), same)
        self.assertIs(live_wallet_gate.head_fixture({"fixtures": {f"b..t@{compiler}": pair}}, "t"), pair)
        # Pre-SF-056 keys (no compiler) and other compilers are never reused.
        self.assertIsNone(live_wallet_gate.head_fixture({"fixtures": {"t": same, "b..t": pair, "t@0.0.0": same}}, "t"))


class SecurityFixtureTests(unittest.TestCase):
    FIXTURE = {"upgrade_cap": "0xc", "registry": "0xr", "head_package": "0x" + "2" * 64}

    def reads(self, owner=OPERATOR, package=None, version="2", registry_owner=None):
        objects = {"0xc": {"owner": {"AddressOwner": owner}, "content": {"package": package or self.FIXTURE["head_package"],
                                                                        "version": version}},
                   "0xr": {"owner": registry_owner or {"Shared": {"initial_shared_version": 3}}},
                   self.FIXTURE["head_package"]: {}}
        run = live(Chain())
        run.operator = OPERATOR

        def read(object_id):
            if object_id not in objects:
                raise RuntimeError("missing")
            return json.loads(json.dumps(objects[object_id]))
        run.object = read
        return run

    def test_valid_lineage_is_reused(self):
        self.assertTrue(fixture_valid(self.reads(), self.FIXTURE))

    def test_changed_lineage_or_ownership_is_not_reused(self):
        self.assertFalse(fixture_valid(self.reads(version="3"), self.FIXTURE))  # Upgraded again on top.
        self.assertFalse(fixture_valid(self.reads(package="0x" + "9" * 64), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(owner="0x" + "1" * 64), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(registry_owner={"AddressOwner": OPERATOR}), self.FIXTURE))
        self.assertFalse(fixture_valid(self.reads(), {**self.FIXTURE, "head_package": "0x" + "4" * 64}))


class SweepTests(unittest.TestCase):
    """Sweeping a throwaway test key (browser or device) is charged like any mutation."""

    def sweep(self, chain, ledger, held=(100_000_000, 0), limit=100_000_000):
        run = live(chain, limit)
        run.operator = OPERATOR
        balances = list(held)
        run.balance = lambda address=None: balances.pop(0)
        with patch.object(live_common, "key_wallet", return_value=("/tmp/sweep.yaml", "0xkey")), \
                patch.object(live_common.time, "sleep"):
            return run, live_common.sweep(run, "00" * 32, Path("/tmp"), ledger(run))

    def test_recovering_a_previous_key_reserves_this_runs_allowance(self):
        chain = Chain(effects(cost=1_000_000))
        run, swept = self.sweep(chain, lambda run: run.ledger)
        self.assertEqual(swept, 100_000_000)
        command, config = chain.calls[0]
        self.assertEqual(config, "/tmp/sweep.yaml")
        self.assertEqual(int(command[command.index("--gas-budget") + 1]), live_common.SWEEP_GAS)
        self.assertEqual(run.ledger.used, 1_000_000)  # Charged; the returned 0.1 SUI is never credited.

    def test_recovery_with_an_exhausted_allowance_never_submits(self):
        for limit in (1, 1_999_999):
            chain = Chain()
            with self.assertRaises(Spent):
                self.sweep(chain, lambda run: run.ledger, limit=limit)
            self.assertEqual(chain.calls, [])

    def test_failed_recovery_is_charged_and_unknown_keeps_its_reservation(self):
        digest = "Swep111111111111111111111111111111111111111"
        chain = Chain(CompletedProcess([], 1, "", f"transaction '{digest}' failed"),
                      tx_blocks={digest: effects("failure", cost=1_200_000, digest=digest)})
        run, swept = self.sweep(chain, lambda run: run.ledger)
        self.assertEqual((swept, run.ledger.used), (0, 1_200_000))
        chain = Chain(CompletedProcess([], 1, "", "connection reset"))
        run, swept = self.sweep(chain, lambda run: run.ledger)
        self.assertEqual((swept, run.ledger.used), (0, live_common.SWEEP_GAS))
        with self.assertRaises(AssertionError):  # Still funded after a failure: the run fails.
            self.sweep(Chain(CompletedProcess([], 1, "", "connection reset")), lambda run: run.ledger,
                       held=(100_000_000, 100_000_000))

    def test_this_runs_sweep_is_bounded_by_its_charged_funding(self):
        chain = Chain(effects(cost=1_000_000))
        funding = {}
        run, _ = self.sweep(chain, lambda run: funding.setdefault("ledger", Budget(100_000_000, run.evidence["transactions"])))
        self.assertEqual(run.ledger.used, 0)  # The funding transfer already charged it.
        self.assertEqual(funding["ledger"].used, 1_000_000)
        self.assertEqual(run.evidence["transactions"][-1]["label"], "sweep the test key")

    def test_nothing_to_sweep_submits_nothing(self):
        chain = Chain()
        run, swept = self.sweep(chain, lambda run: run.ledger, held=(4_000_000,))
        self.assertEqual((swept, chain.calls, run.ledger.used), (0, [], 0))


    def test_recovery_charges_this_run_and_keeps_the_key_until_swept(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "live-security-device.key"
            live_common.pending_key(pending, "00" * 32)
            self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
            run = live(Chain(effects(cost=1_000_000)))
            run.operator = OPERATOR
            run.balance = lambda address=None: 30_000_000
            with patch.object(live_common, "key_wallet", return_value=("/tmp/sweep.yaml", "0xkey")), \
                    patch.object(live_common.time, "sleep"):
                self.assertEqual(live_common.recover_key(run, pending, Path(temporary)), 30_000_000)
                self.assertFalse(pending.exists())
                self.assertEqual(run.ledger.used, 1_000_000)
                live_common.pending_key(pending, "00" * 32)
                run = live(Chain(), limit=1)
                run.operator = OPERATOR
                run.balance = lambda address=None: 30_000_000
                with self.assertRaises(Spent):
                    live_common.recover_key(run, pending, Path(temporary))
                self.assertTrue(pending.exists())  # Kept for a later run.


class TeardownTests(unittest.TestCase):
    """The upgrade-security suite deletes its objects and sweeps its funded
    device key, after a failure too; the key stays pending until swept."""

    def teardown(self, chain, made, held=(30_000_000, 0)):
        run = live(chain)
        # These legacy cleanup fixtures have no sharded directory. Separate
        # decoder/retirement cases below cover v3 storage and failure handling.
        run.receipt_directory = lambda record: None
        run.operator = OPERATOR
        balances = list(held)
        run.balance = lambda address=None: balances.pop(0)
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "live-security-device.key"
            live_common.pending_key(pending, "00" * 32)
            made = {"base_records": [], "head_records": [], "pending": pending, "seed": "00" * 32, "root": Path(temporary),
                    "device_ledger": Budget(40_000_000, run.evidence["transactions"]), **made}
            with patch.object(live_common, "key_wallet", return_value=("/tmp/device.yaml", "0xkey")), \
                    patch.object(live_common.time, "sleep"):
                failure = live_upgrade_security.teardown(run, made)
            return run, made, failure, pending.exists()

    @staticmethod
    def calls(chain):
        return [(" ".join(command), config) for command, config in chain.calls]

    def test_success_deletes_everything_and_sweeps_the_device(self):
        chain = Chain(*(effects(cost=1_000_000) for _ in range(4)))
        run, made, failure, kept = self.teardown(chain, dict(
            package="0xhead", base_package="0xbase", registry="0xreg", device="0xdev", group="0xgrp", owner_cap="0xown",
            base_records=["0xd0"], head_records=["0xd1", "0xd2"]))
        self.assertIsNone(failure)
        self.assertFalse(kept)
        calls = self.calls(chain)
        self.assertIn("0xhead::deployment_manager::delete_deployment @0xd1 --move-call 0xhead::deployment_manager::delete_deployment @0xd2",
                      calls[0][0])
        self.assertIn("0xbase::deployment_manager::delete_deployment @0xd0", calls[1][0])
        self.assertIn("0xhead::device_group::delete_group @0xgrp --move-call "
                      "0xhead::device_registry::deregister_activated_device @0xreg @0xown", calls[2][0])
        self.assertIn("--transfer-objects [gas] @" + OPERATOR, calls[3][0])
        self.assertEqual(calls[3][1], "/tmp/device.yaml")
        self.assertEqual(run.ledger.used, 3_000_000)
        self.assertTrue(all(f"--gas-budget {live_upgrade_security.CLEANUP_GAS}" in c for c, _ in calls[:3]))
        self.assertEqual(made["device_ledger"].used, 1_000_000)  # The device pays its own sweep from its charged funding.
        self.assertEqual(run.evidence["device_sweep_mist"], 30_000_000)

    def test_a_failed_step_never_skips_the_rest_and_keeps_an_unswept_key(self):
        # Before activation, with an unknown deletion outcome and a sweep that
        # leaves the key funded: every step is attempted and the key is kept.
        chain = Chain(CompletedProcess([], 1, "", "connection reset"), effects(cost=1_000_000),
                      CompletedProcess([], 1, "", "connection reset"))
        run, made, failure, kept = self.teardown(chain, dict(
            package="0xbase", base_package="0xbase", registry="0xreg", device="0xdev", group="0xgrp",
            base_records=["0xd0"]), held=(30_000_000, 30_000_000))
        self.assertIsInstance(failure, RuntimeError)  # The first failure; the sweep's own is recorded too.
        self.assertTrue(kept)
        calls = self.calls(chain)
        self.assertEqual(len(calls), 3)
        self.assertIn("0xbase::device_registry::deregister_device @0xreg @0xdev", calls[1][0])
        self.assertEqual(len(run.evidence["cleanup_failures"]), 2)
        self.assertEqual(run.ledger.used, live_upgrade_security.CLEANUP_GAS + 1_000_000)  # Unknown: reservation kept.
        self.assertEqual(made["device_ledger"].used, live_common.SWEEP_GAS)

    def test_nothing_created_submits_nothing_but_releases_an_empty_key(self):
        chain = Chain()
        run, _, failure, kept = self.teardown(chain, {}, held=(0,))
        self.assertEqual((failure, chain.calls, kept, run.ledger.used), (None, [], False, 0))


class DaemonStartupTests(unittest.TestCase):
    """A daemon whose startup fails is killed and reaped before the error
    reaches the caller, which never receives its handle."""

    def start(self, journal=None, failure=None):
        self.process = Mock()
        self.process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if journal is not None:
                (root / "state-legacy").mkdir()
                (root / "state-legacy" / "state.json").write_text(journal)
            with patch.object(live_common.dm, "generate"), \
                    patch.object(live_common.subprocess, "Popen", return_value=self.process), \
                    patch.object(live_common, "wait_for", side_effect=failure) if failure else nullcontext():
                return live_common.start_daemon(root, "legacy", {}, "")

    def assert_reaped(self):
        self.process.kill.assert_called_once()
        self.process.wait.assert_called_once()

    def test_a_startup_timeout_kills_and_reaps_the_daemon(self):
        with self.assertRaisesRegex(AssertionError, "initialization timeout"):
            self.start(failure=AssertionError("initialization timeout"))
        self.assert_reaped()

    def test_an_unreadable_journal_kills_and_reaps_the_daemon(self):
        with self.assertRaises(json.JSONDecodeError):
            self.start(journal="{not json")
        self.assert_reaped()

    def test_an_exited_daemon_is_still_reaped(self):
        with self.assertRaisesRegex(AssertionError, "daemon exited"):
            self.start(failure=AssertionError("daemon exited (1): legacy daemon did not enroll"))
        self.process.wait.assert_called_once()

    def test_an_enrolled_daemon_is_returned_running(self):
        process, _ = self.start(journal=json.dumps({"initialized": True}))
        self.assertIs(process, self.process)
        process.kill.assert_not_called()
        process.wait.assert_not_called()


class ShardedReceiptViewTests(unittest.TestCase):
    def fixture(self):
        run = Live('/nonexistent/client.yaml', 'localnet', 1)
        roots = [hex(100 + i) for i in range(16)]
        directory = dict(roots=roots, ready=True, retiring=False, migrated=16)
        objects = {
            'receipt': {'objType': '0x1::dynamic_field::Field<0x2::deployment_manager::ReceiptsKey, X>', 'content': {'value': {'version':3, 'statuses':{'contents':[]}}}},
            'directory': {'objType': '0x1::dynamic_field::Field<0x2::deployment_manager::ReceiptShardKey, X>', 'content': {'value':directory}},
            'entry': {'objType':'0x1::dynamic_field::Field<0x2::deployment_manager::ReceiptKey, X>', 'content': {'name': {'device':'0x10'}, 'value':{'status':1}}},
        }
        for index, root in enumerate(roots):
            objects[root] = {'content': {'deployment':'0xd', 'index':index, 'count':int(index==0)}}
        def client(command, parent):
            if command == 'object': return objects[parent]
            self.assertEqual(command, 'dynamic-field')
            return {'dynamicFields': [{'fieldId':v} for v in (['receipt','directory'] if parent=='0xd' else ['entry'] if parent==roots[0] else [])]}
        run.client = client
        return run, objects, directory, roots

    def test_reads_bound_v3_receipts_and_never_promotes_partial_migration(self):
        run, _, directory, _ = self.fixture()
        self.assertEqual(run.receipts('0xd'), {live_common.dm.object_id('0x10'):1})
        directory['ready'] = False
        self.assertEqual(run.receipts('0xd'), {})
        directory['ready'] = True; directory['retiring'] = True
        self.assertEqual(run.receipts('0xd'), {})

    def test_foreign_root_and_truncated_cli_view_are_refused(self):
        run, objects, _, roots = self.fixture()
        objects[roots[0]]['content']['deployment'] = '0xee'
        with self.assertRaises(AssertionError): run.receipts('0xd')
        objects[roots[0]]['content']['deployment'] = '0xd'
        objects[roots[0]]['content']['count'] = 2
        with self.assertRaisesRegex(AssertionError, 'truncated'): run.receipts('0xd')


if __name__ == "__main__":
    unittest.main()
