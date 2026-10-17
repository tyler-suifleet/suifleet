"""Offline tests for the SF-065 wallet-to-edge acceptance runner: profile
validation, the acceptance matrix and verdicts, locks, SSH host pinning and
command construction, evidence redaction, and full runs against a fake chain
and a fake device that executes the real SF-030 updater. No network, no keys,
no hardware."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment_manifest as dm  # noqa: E402
import edge_acceptance as ea  # noqa: E402
import test_component as component  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "manifests/fixture.localnet.json").read_text())
OPERATOR = dm.object_id("0xad")
FINGERPRINT = "SHA256:" + "A" * 43


def object_id(n):
    return dm.object_id(hex(n))


def fake_digest(n):
    """A well-formed (base58, 44-character) transaction digest for the nth fake transaction."""
    return ("D" + "".join("123456789A"[int(c)] for c in str(n))).ljust(44, "z")


class Workspace:
    """A temporary directory holding a manifest, wallet config and SSH key file."""

    def __init__(self, test, network="localnet"):
        self.dir = Path(tempfile.mkdtemp(prefix="suifleet-edge-acceptance-test-"))
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        manifest = dict(FIXTURE)
        if network == "testnet":
            manifest.update(network="testnet", chain_identifier=ea.TESTNET_CHAIN, rpc_url="https://fullnode.testnet.sui.io:443")
        (self.dir / "deployment.json").write_text(json.dumps(manifest))
        (self.dir / "client.yaml").write_text("keystore: {}\n")
        (self.dir / "id_ed25519").write_text("not a key; tests only check existence\n")
        (self.dir / "daemon").write_text("#!/bin/sh\n")

    def profile(self, **changes):
        document = {"schema": 1, "profile": "localnet", "manifest": "deployment.json",
                    "wallet": {"mode": "scripted", "address": OPERATOR, "client_config": "client.yaml"},
                    "walrus": {"mode": "stand-in", "bind": "127.0.0.1"},
                    "device": {"mode": "local", "daemon": "daemon"}, "artifact_profile": "public"}
        for key, value in changes.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        return document

    def ssh_device(self, **changes):
        device = {"mode": "ssh", "host": "192.168.1.50", "user": "operator", "host_key_sha256": FINGERPRINT,
                  "identity_file": "id_ed25519"}
        device.update(changes)
        return device

    def validate(self, document):
        return ea.validate_profile(document, self.dir)


class ProfileValidationTests(unittest.TestCase):
    def test_localnet_scripted_local_profile_is_accepted(self):
        w = Workspace(self)
        profile = w.validate(w.profile())
        self.assertEqual((profile["profile"], profile["device"]["mode"], profile["wallet"]["address"]), ("localnet", "local", OPERATOR))
        self.assertEqual(ea.needed_locks(profile), [])

    def test_refusals_name_the_problem(self):
        w = Workspace(self)
        cases = {
            "mainnet": (w.profile(profile="mainnet"), "mainnet"),
            "devnet": (w.profile(profile="devnet"), "refused"),
            "schema": (dict(w.profile(), schema=2), "schema"),
            "unknown field": (dict(w.profile(), extra=1), "unknown"),
            "private key field": (w.profile(wallet={"mode": "scripted", "address": OPERATOR, "client_config": "client.yaml",
                                                    "private_key": "x"}), "must not contain"),
            "key material value": (w.profile(device={"mode": "local", "daemon": "suiprivkey1qqqqqqqq"}), "private key"),
            "no manifest": (w.profile(manifest=None), "manifest"),
            "missing manifest": (w.profile(manifest="absent.json"), "manifest"),
            "manifest network": (w.profile(profile="testnet"), "manifest is for localnet"),
            "wallet mode": (w.profile(wallet={"mode": "browser", "address": OPERATOR}), "wallet.mode"),
            "default wallet": (w.profile(wallet={"mode": "scripted", "address": OPERATOR}), "explicit wallet.client_config"),
            "absent wallet": (w.profile(wallet={"mode": "scripted", "address": OPERATOR, "client_config": "nope.yaml"}), "does not exist"),
            "bad address": (w.profile(wallet={"mode": "scripted", "address": "alice", "client_config": "client.yaml"}), "wallet.address"),
            "extension without dapp": (w.profile(wallet={"mode": "extension", "address": OPERATOR, "client_config": "client.yaml"}), "dapp_url"),
            "remote faucet": (w.profile(faucet_url="https://faucet.testnet.sui.io/v2/gas"), "loopback"),
            "device mode": (w.profile(device={"mode": "usb"}), "device.mode"),
            "walrus mode": (w.profile(walrus={"mode": "magic"}), "walrus.mode"),
            "walrus lan for local": (w.profile(walrus={"mode": "stand-in", "bind": "192.168.1.2"}), "loopback Walrus"),
            "artifact profile": (w.profile(artifact_profile="secret-sauce"), "artifact_profile"),
            "budget": (w.profile(budget={"max_spend_mist": -1}), "max_spend_mist"),
        }
        for name, (document, message) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ea.ProfileError) as raised:
                    w.validate(document)
                self.assertIn(message, str(raised.exception))

    def test_localnet_needs_a_loopback_node(self):
        w = Workspace(self)
        manifest = dict(FIXTURE, rpc_url="https://fullnode.example.org:443")
        (w.dir / "deployment.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ea.ProfileError, "loopback"):
            w.validate(w.profile())

    def test_testnet_requires_dedicated_account_and_spend_ceiling(self):
        w = Workspace(self, "testnet")
        agent = ea.DEDICATED_TESTNET["agent"]
        wallet = {"mode": "scripted", "address": agent, "client_config": "client.yaml"}
        base = w.profile(profile="testnet", wallet=wallet, budget={"max_spend_mist": 100_000_000})
        profile = w.validate(base)
        self.assertEqual(ea.needed_locks(profile), [ea.TESTNET_LOCK])
        self.assertEqual(ea.needed_locks(profile, mutate=False), [])
        with self.assertRaisesRegex(ea.ProfileError, "dedicated"):
            w.validate(dict(base, wallet=dict(wallet, address=OPERATOR)))
        for budget in (None, {"max_spend_mist": 0}, {"max_spend_mist": ea.TESTNET_MAX_SPEND + 1}, {"max_spend_mist": "1"}):
            with self.subTest(budget=budget), self.assertRaisesRegex(ea.ProfileError, "max_spend_mist"):
                w.validate(dict(base, budget=budget) if budget else {k: v for k, v in base.items() if k != "budget"})
        with self.assertRaisesRegex(ea.ProfileError, "another chain|network label"):
            manifest = json.loads((w.dir / "deployment.json").read_text())
            (w.dir / "deployment.json").write_text(json.dumps(dict(manifest, chain_identifier="35834a8a")))
            w.validate(base)

    def test_ssh_device_needs_explicit_target_and_pinned_fingerprint(self):
        w = Workspace(self)
        walrus = {"mode": "stand-in", "bind": "192.168.1.10"}
        profile = w.validate(w.profile(device=w.ssh_device(), walrus=walrus))
        self.assertEqual(profile["device"]["key"], "/etc/suifleet/device.key")
        self.assertEqual(profile["device"]["expected_arch"], "aarch64")
        self.assertEqual(ea.needed_locks(profile), [ea.HARDWARE_LOCK])
        bad = {
            "guessed host": (w.ssh_device(host="auto"), "explicit"),
            "wildcard host": (w.ssh_device(host="192.168.1.*"), "explicit"),
            "loopback host": (w.ssh_device(host="127.0.0.1"), "Pi itself"),
            "user@host": (w.ssh_device(host="operator@pi"), "explicit"),
            "option injection": (w.ssh_device(host="-oProxyCommand=x"), "host name"),
            "no fingerprint": (w.ssh_device(host_key_sha256=None), "fingerprint"),
            "md5 fingerprint": (w.ssh_device(host_key_sha256="MD5:aa:bb"), "fingerprint"),
            "no identity": (w.ssh_device(identity_file=None), "identity_file"),
            "absent identity": (w.ssh_device(identity_file="absent"), "does not exist"),
            "bad user": (w.ssh_device(user="root; rm"), "device.user"),
            "bad port": (w.ssh_device(port=0), "port"),
            "moved key": (w.ssh_device(key="/tmp/device.key"), "SF-029 image"),
            "dotdot path": (w.ssh_device(config="/etc/suifleet/../shadow"), "absolute path"),
            "binary digest": (w.ssh_device(expected_binary_sha256="xyz"), "SHA-256"),
        }
        for name, (device, message) in bad.items():
            with self.subTest(name), self.assertRaises(ea.ProfileError) as raised:
                w.validate(w.profile(device=device, walrus=walrus))
            self.assertIn(message, str(raised.exception))
        with self.assertRaisesRegex(ea.ProfileError, "cannot reach a loopback"):
            w.validate(w.profile(device=w.ssh_device()))

    def test_http_walrus_must_match_the_manifest(self):
        w = Workspace(self)
        walrus = dict(FIXTURE["walrus"], mode="http")
        self.assertEqual(w.validate(w.profile(walrus=walrus))["walrus"]["epochs"], 1)
        with self.assertRaisesRegex(ea.ProfileError, "differs from the manifest"):
            w.validate(w.profile(walrus=dict(walrus, aggregator_url="https://other.example/")))
        with self.assertRaisesRegex(ea.ProfileError, "epochs"):
            w.validate(w.profile(walrus=dict(walrus, epochs=50)))


class MatrixTests(unittest.TestCase):
    def profile(self, device="local", wallet="scripted", artifacts="public"):
        return {"device": {"mode": device}, "wallet": {"mode": wallet}, "artifact_profile": artifacts}

    def test_stand_in_and_scripted_gaps_cannot_pass(self):
        gaps = ea.mode_gaps(self.profile())
        self.assertEqual(gaps["image-identity"][0], "skipped")
        self.assertEqual(gaps["extension-wallet"][0], "blocked")
        self.assertEqual(gaps["encrypted-delivery"][0], "blocked")
        self.assertNotIn("direct-v1", gaps)
        cases = {id: {"status": "passed"} for id in ea.CASE_IDS}
        for id, (status, _) in gaps.items():
            cases[id] = {"status": status}
        self.assertEqual(ea.verdict(cases), "incomplete")

    def test_extension_hardware_run_leaves_failure_paths_to_scripted(self):
        gaps = ea.mode_gaps(self.profile("ssh", "extension"))
        self.assertNotIn("image-identity", gaps)
        self.assertNotIn("extension-wallet", gaps)
        for id in ("recipe-tamper", "artifact-tamper", "report-retry", "unauthorized-target"):
            self.assertEqual(gaps[id][0], "skipped", id)

    def test_encrypted_profile_blocks_everything(self):
        gaps = ea.mode_gaps(self.profile(artifacts="sf022-encrypted"))
        self.assertEqual(set(gaps), set(ea.CASE_IDS))
        self.assertTrue(all(status in ("blocked", "skipped") for status, _ in gaps.values()))

    def test_verdict(self):
        all_passed = {id: {"status": "passed"} for id in ea.CASE_IDS}
        self.assertEqual(ea.verdict(all_passed), "passed")
        future = dict(all_passed, **{"supervised-lifecycle": {"status": "skipped"}})
        self.assertEqual(ea.verdict(future), "passed", "growth cases do not gate the baseline")
        self.assertEqual(ea.verdict(dict(all_passed, **{"direct-v1": {"status": "skipped"}})), "incomplete")
        self.assertEqual(ea.verdict(dict(all_passed, **{"cleanup": {"status": "failed"}})), "failed")

    def test_results_never_upgrade_a_failure(self):
        results = ea.Results()
        results.set("direct-v1", "failed", "no receipt")
        results.set("direct-v1", "passed")
        self.assertEqual(results.cases["direct-v1"]["status"], "failed")
        results.set("group-v2", "blocked", "x")
        results.set("group-v2", "passed")
        self.assertEqual(results.cases["group-v2"]["status"], "blocked")
        finished = results.finish("not reached")
        self.assertEqual(finished["preflight"], {"status": "blocked", "capability": finished["preflight"]["capability"], "detail": "not reached"})

    def test_summarize_counts_only_hardware_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            def write(name, mode, statuses):
                cases = {id: {"status": statuses.get(id, "skipped")} for id in ea.CASE_IDS}
                path = Path(directory) / name
                path.write_text(json.dumps({"device_mode": mode, "cases": cases}))
                return path
            baseline = [c.id for c in ea.CASES if not c.future]
            local = write("local.json", "local", {id: "passed" for id in baseline})
            summary = ea.summarize([local])
            self.assertEqual(summary["matrix"]["direct-v1"]["status"], "local-only")
            self.assertEqual(summary["sf031_verdict"], "incomplete")
            extension = write("ext.json", "ssh", {id: "passed" for id in baseline if id not in
                                                  ("report-retry", "recipe-tamper", "artifact-tamper", "unauthorized-target",
                                                   "updater-install-failure", "updater-health-failure", "offline-catch-up",
                                                   "encrypted-delivery")})
            scripted = write("scripted.json", "ssh", {id: "passed" for id in baseline if id not in ("extension-wallet", "encrypted-delivery")})
            summary = ea.summarize([extension, scripted])
            self.assertEqual(summary["matrix"]["encrypted-delivery"]["status"], "skipped")
            self.assertEqual(summary["sf031_verdict"], "incomplete", "SF-022 still missing")
            broken = write("broken.json", "ssh", {"direct-v1": "failed"})
            self.assertEqual(ea.summarize([extension, scripted, broken])["sf031_verdict"], "failed")


class Completed(SimpleNamespace):
    pass


class LockTests(unittest.TestCase):
    def runner(self, outputs=None, fail=()):
        calls = []

        def run(command, **kwargs):
            calls.append(command[3:])
            verb = command[3]
            if verb in fail:
                return Completed(returncode=1, stdout="", stderr=f"{verb} refused: held by Codex")
            return Completed(returncode=0, stdout=(outputs or {}).get(verb, ""), stderr="")
        return run, calls

    def test_acquire_assert_renew_release(self):
        now = [0.0]
        run, calls = self.runner({"lock": json.dumps({"lock_token": "LOCK-1"})})
        identity = {"agent": "Claude", "model": "m", "session": "s", "ticket": "SF-065"}
        lock = ea.ResourceLock(ea.HARDWARE_LOCK, identity, "purpose", runner=run, clock=lambda: now[0]).acquire()
        self.assertEqual(lock.token, "LOCK-1")
        self.assertEqual(calls[0][:2], ["lock", ea.HARDWARE_LOCK])
        self.assertIn("--ticket", calls[0])
        lock.assert_()
        self.assertEqual([c[0] for c in calls], ["lock", "lock-assert"])
        now[0] = ea.LOCK_RENEW_SECONDS + 1
        lock.assert_()
        self.assertEqual([c[0] for c in calls], ["lock", "lock-assert", "lock-assert", "lock-renew"])
        lock.release()
        self.assertEqual(calls[-1], ["unlock", ea.HARDWARE_LOCK, "--lock-token", "LOCK-1"])
        self.assertIsNone(lock.token)

    def test_plain_token_output_and_refusals(self):
        run, _ = self.runner({"lock": "acquired hardware:raspberrypi3bplus\nTOKEN-2\n"})
        identity = {"agent": "Claude", "model": "m", "session": "s"}
        self.assertEqual(ea.ResourceLock(ea.HARDWARE_LOCK, identity, "p", runner=run).acquire().token, "TOKEN-2")
        with self.assertRaisesRegex(ea.ProfileError, "--agent"):
            ea.ResourceLock(ea.HARDWARE_LOCK, None, "p", runner=run).acquire()
        busy, _ = self.runner(fail=("lock",))
        with self.assertRaisesRegex(ea.ProfileError, "held by Codex"):
            ea.ResourceLock(ea.HARDWARE_LOCK, identity, "p", runner=busy).acquire()
        lost, _ = self.runner({"lock": "T"}, fail=("lock-assert",))
        lock = ea.ResourceLock(ea.HARDWARE_LOCK, identity, "p", runner=lost).acquire()
        with self.assertRaises(ea.ProfileError):
            lock.assert_()

    def test_externally_held_token_is_asserted_not_released(self):
        run, calls = self.runner()
        lock = ea.ResourceLock(ea.TESTNET_LOCK, None, "p", runner=run, token="EXT").acquire()
        lock.release()
        self.assertEqual([c[0] for c in calls], ["lock-assert"])


class SshTests(unittest.TestCase):
    def device(self, scan, fingerprints):
        w = Workspace(self)
        profile = w.validate(w.profile(device=w.ssh_device(), walrus={"mode": "stand-in", "bind": "192.168.1.10"}))
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == "ssh-keyscan":
                return Completed(returncode=0, stdout=scan, stderr="")
            if command[0] == "ssh-keygen":
                line = kwargs["input"].strip()
                return Completed(returncode=0, stdout=f"256 {fingerprints.get(line, 'SHA256:other')} host (ED25519)\n", stderr="")
            return Completed(returncode=0, stdout="aarch64\n", stderr="")
        lock = SimpleNamespace(asserted=0)
        lock.assert_ = lambda: setattr(lock, "asserted", lock.asserted + 1)
        device = ea.SshDevice(profile, lock, runner=run)
        self.addCleanup(device.close)
        return device, calls, lock

    def test_only_the_pinned_host_key_is_trusted(self):
        good, other = "192.168.1.50 ssh-ed25519 AAAAgood", "192.168.1.50 ecdsa-sha2-nistp256 AAAAother"
        device, calls, lock = self.device(f"# banner\n{other}\n{good}\n", {good: FINGERPRINT})
        device.pin_host_key()
        self.assertEqual(device.known_hosts.read_text(), good + "\n")
        self.assertEqual(lock.asserted, 1)

    def test_mismatched_host_key_refuses_to_connect(self):
        device, calls, _ = self.device("192.168.1.50 ssh-ed25519 AAAAimpostor\n", {})
        with self.assertRaisesRegex(ea.ProfileError, "matches the pinned fingerprint"):
            device.pin_host_key()
        self.assertFalse(any(c[0][0] == "ssh" for c in calls))

    def test_commands_are_strict_quoted_and_locked(self):
        device, calls, lock = self.device("", {})
        device.ssh("cat", "/var/lib/suifleet/x; reboot")
        command = calls[-1][0]
        self.assertEqual(command[0], "ssh")
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", f"UserKnownHostsFile={device.known_hosts}",
                       "GlobalKnownHostsFile=/dev/null", "IdentitiesOnly=yes"):
            self.assertIn(option, command)
        self.assertEqual(command[-3:], ["operator@192.168.1.50", "--", "cat '/var/lib/suifleet/x; reboot'"])
        self.assertEqual(lock.asserted, 1)

    def test_enrollment_install_order_and_permissions(self):
        device, calls, _ = self.device("", {})
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "deployment.json").write_text("{}")
            (bundle / "config.toml").write_text("[device]\n")
            device.install(bundle, {"device": object_id(1), "cap": object_id(2)})
        remote = [c[0][-1] for c in calls]
        installs = [r for r in remote if " install " in f" {r} "]
        self.assertEqual([r.split()[-1] for r in installs],
                         ["/etc/suifleet/.deployment.json.new", "/etc/suifleet/.signer.toml.new", "/etc/suifleet/.config.toml.new"])
        self.assertTrue(all("-m 0640" in r and "-o root" in r for r in installs))
        self.assertIn("-g suifleet-signer", installs[1])
        self.assertFalse(any("rm " in r or "device.key" in r and "sha256sum" not in r for r in remote))
        signer = device.signer_toml({"device": object_id(1), "cap": object_id(2)})
        self.assertIn('keypair_path = "/etc/suifleet/device.key"', signer)
        self.assertIn('caller_unit = "suifleet.service"', signer)

    def test_template_uses_the_signing_service(self):
        device, _, _ = self.device("", {})
        template = device.template({"device": object_id(1), "cap": object_id(2)})
        self.assertIn('kind = "service"', template)
        self.assertNotIn("keypair_path", template)
        self.assertIn('command = "/usr/bin/suifleet-test-updater"', template)
        self.assertIn('state_dir = "/var/lib/suifleet"', template)


class EvidenceTests(unittest.TestCase):
    def test_redaction_and_refusal(self):
        self.assertEqual(ea.redact("key suiprivkey1abcdef here"), "key [REDACTED] here")
        self.assertEqual(ea.redact("x SECRET y", ["SECRET"]), "x [REDACTED] y")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e.json"
            with self.assertRaises(RuntimeError):
                ea.write_evidence(path, {"log": "suiprivkey1zzz"})
            with self.assertRaises(RuntimeError):
                ea.write_evidence(path, {"log": "abc0123"}, forbidden=["abc0123"])
            self.assertFalse(path.exists())
            ea.write_evidence(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})

    def test_prompt_blank_means_not_signed(self):
        out = io.StringIO()
        digest = "9" * 44
        self.assertEqual(ea.Prompt(io.StringIO(digest + "\n"), out)("step"), digest)
        self.assertIsNone(ea.Prompt(io.StringIO("\n"), out)("step"), "blank: nothing was signed")
        # Anything else goes to verify_extension, which keeps the reservation for an unverifiable answer.
        self.assertEqual(ea.Prompt(io.StringIO("rm -rf /\n"), out)("step"), "rm -rf /")
        self.assertIsNone(ea.Prompt(None, out)("step"), "no operator input means no digest")


# ── Fake fleet: a chain, Walrus and device daemon ─────────────────────────────

class FakeChain:
    def __init__(self):
        self.objects, self.receipts, self.balances = {}, {}, {OPERATOR: 10**12}
        self.deployments, self.counter, self.txs, self.calls = [], 0x1000, {}, []
        self.fail_calls = set()
        self.origins = dict(FIXTURE["type_origins"])  # Tests may substitute a foreign origin.

    def type_of(self, suffix):
        return f"{self.origins.get(suffix, FIXTURE['call_package_id'])}::{suffix}"

    def new(self, suffix, owner, **fields):
        id = object_id(self.counter)
        self.counter += 1
        self.objects[id] = {"type": suffix, "owner": owner, "fields": fields}
        return id

    def owner_json(self, owner):
        if owner == "shared":
            return {"Shared": {"initial_shared_version": "1"}}
        if owner == "custodied":
            return {"ObjectOwner": object_id(0xcf)}
        return {"AddressOwner": owner}

    def targets(self, id, device):
        fields = self.objects[id]["fields"]
        return device in fields["devices"] or any(
            device in self.objects.get(g, {"fields": {"device_ids": []}})["fields"]["device_ids"] for g in fields["groups"])


class FakeCli:
    def __init__(self, chain):
        self.chain = chain

    def active_address(self):
        return OPERATOR

    def balance(self, address=None):
        return self.chain.balances.get(address or OPERATOR, 0)

    def receipts(self, deployment):
        return dict(self.chain.receipts.get(dm.object_id(deployment), {}))

    def record_fields(self, deployment):
        fields = self.chain.objects[dm.object_id(deployment)]["fields"]
        origin = lambda key: self.chain.origins[f"deployment_manager::{key}"]
        return {"RecipeDigestKey": (origin("RecipeDigestKey"), fields["recipe_sha256"]),
                "TargetDevicesKey": (origin("TargetDevicesKey"), fields["devices"]),
                "TargetGroupsKey": (origin("TargetGroupsKey"), fields["groups"])}

    def transaction_block(self, digest):
        if digest not in self.chain.txs:
            raise RuntimeError(f"sui client tx-block failed: {digest} not found")
        return self.chain.txs[digest]

    def __call__(self, *args, json_output=True):
        verb = args[0]
        if verb == "envs":
            return [[{"alias": "t", "rpc": FIXTURE["rpc_url"]}], "t"]
        if verb == "object":
            id = dm.object_id(args[1])
            o = self.chain.objects[id]
            return {"objectId": id, "owner": self.chain.owner_json(o["owner"]), "objType": self.chain.type_of(o["type"]),
                    "content": {"fields": o["fields"]}}
        if verb == "ptb":
            return self.ptb(list(args[1:]))
        raise AssertionError(f"unexpected sui client {args}")

    def ptb(self, tokens):
        chain, env, changes = self.chain, {}, []
        coin = None
        strip = lambda value: value.strip('"')
        ref = lambda value: env[value] if value in env else dm.object_id(value.lstrip("@"))
        ids = lambda value: [dm.object_id(v.strip().lstrip("@")) for v in value[len("vector["):-1].split(",") if v.strip()]
        last = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "--gas-budget":
                i += 2
                continue
            if token == "--assign":
                env[tokens[i + 1]] = last
                if isinstance(last, tuple):
                    env[tokens[i + 1] + ".0"], env[tokens[i + 1] + ".1"] = last
                i += 2
                continue
            if token == "--split-coins":
                coin = int(tokens[i + 2].strip("[]"))
                last = "coin"
                i += 3
                continue
            if token == "--transfer-objects":
                item, to = tokens[i + 1].strip("[]"), dm.object_id(tokens[i + 2].lstrip("@"))
                if env.get(item) == "coin":
                    chain.balances[to] = chain.balances.get(to, 0) + coin
                else:
                    chain.objects[env[item]]["owner"] = to
                i += 3
                continue
            assert token == "--move-call", token
            target = tokens[i + 1]
            j = i + 2
            while j < len(tokens) and not tokens[j].startswith("--"):
                j += 1
            call, a = target.split("::", 1)[1], tokens[i + 2:j]
            chain.calls.append(call)
            if call in chain.fail_calls:
                return {"digest": fake_digest(len(chain.txs)), "effects": {"status": {"status": "failure", "error": "abort"},
                        "gasUsed": {"computationCost": "1000", "storageCost": "0", "storageRebate": "0"}}, "objectChanges": []}
            created = []
            if call == "device_registry::register_device":
                last = chain.new("device_registry::Device", OPERATOR, device_address=dm.object_id(a[2].lstrip("@")), name=strip(a[1]))
                created.append(last)
            elif call == "device_cap::issue_cap":
                device = ref(a[0])
                created.append(chain.new("device_cap::DeviceCap", chain.objects[device]["fields"]["device_address"], device_id=device))
            elif call == "device_registry::activate_device":
                device = ref(a[1])
                chain.objects[device]["owner"] = "custodied"
                last = chain.new("device_registry::DeviceOwnerCap", OPERATOR, device_id=device)
                created.append(last)
            elif call == "device_group::create_group":
                created.append(chain.new("device_group::DeviceGroup", "shared", device_ids=[]))
            elif call == "device_registry::borrow_device":
                last = (chain.objects[ref(a[1])]["fields"]["device_id"], "loan")
            elif call == "device_group::add_device":
                chain.objects[ref(a[0])]["fields"]["device_ids"].append(ref(a[1]))
            elif call == "deployment_manager::create_deployment_verified":
                record = chain.new("deployment_manager::DeploymentRecord", "shared", component_name=strip(a[0]),
                                   version=strip(a[1]), recipe_blob_id=strip(a[2]), artifact_sha256=strip(a[3]),
                                   recipe_sha256=strip(a[4]), groups=ids(a[5]), devices=ids(a[6]), creator=OPERATOR)
                chain.deployments.append(record)
                created.append(record)
            elif call in ("deployment_manager::delete_deployment", "device_group::delete_group"):
                chain.objects.pop(ref(a[0]))
            elif call == "device_group::remove_device":
                chain.objects[ref(a[0])]["fields"]["device_ids"].remove(ref(a[1]))
            elif call == "device_registry::deregister_device":
                chain.objects.pop(ref(a[1]))
            elif call != "device_registry::return_device":
                raise AssertionError(f"unexpected call {call}")
            changes += [{"type": "created", "objectType": f"{FIXTURE['call_package_id']}::{chain.objects[c]['type']}", "objectId": c}
                        for c in created]
            i = j
        digest = fake_digest(len(chain.txs))
        result = {"digest": digest, "effects": {"status": {"status": "success"},
                  "gasUsed": {"computationCost": "1000", "storageCost": "500", "storageRebate": "100"}}, "objectChanges": changes}
        chain.txs[digest] = result
        return result


class FakeWalrus:
    url = "http://127.0.0.1:1"

    def __init__(self):
        self.blobs = {}

    def store(self, name, data):
        self.blobs[name] = data
        return name

    def fetch(self, blob_id):
        return self.blobs[blob_id]

    def close(self):
        pass


class FakeDevice:
    """Emulates the daemon's protocol, executing the real SF-030 updater."""

    kind = "local-stand-in"

    def __init__(self, test, chain, walrus, faults=()):
        self.chain, self.walrus, self.faults = chain, walrus, set(faults)
        self.root = Path(tempfile.mkdtemp(prefix="suifleet-fake-device-"))
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.components = self.root / "test-components"
        self.address_value = object_id(0xd0d0)
        self.key = "5a" * 32
        self.config, self.running_flag, self.state = None, False, {}
        self.starts = 0

    def assert_lock(self):
        pass

    def address(self):
        return self.address_value

    def facts(self):
        return {"kind": self.kind, "arch": os.uname().machine, "binary_sha256": "0" * 64, "updater_sha256": "1" * 64,
                "address": self.address_value, "key_mode": "0o600"}

    def key_digest(self):
        if "rotate_key" in self.faults and self.starts > 1:
            return hashlib.sha256(b"other").hexdigest()
        return hashlib.sha256(self.key.encode()).hexdigest()

    def current_enrollment(self):
        return self.config

    def template(self, ids):
        return ea.device_template(self, ids, "updater", self.root, 1, keypair=self.root / "device.key")

    def install(self, bundle):
        self.config = ea.enrolled_ids((bundle / "config.toml").read_text())
        manifest = json.loads((bundle / "deployment.json").read_text())
        assert manifest["walrus"]["aggregator_url"] == FakeWalrus.url

    def start(self):
        self.running_flag = True
        self.starts += 1

    def stop(self):
        self.running_flag = False

    def restart(self):
        self.stop()
        self.start()
        if "replay_on_restart" in self.faults:
            self.state["cursor"] = 0

    def running(self):
        return self.running_flag

    def exited(self):
        return False

    def journal(self):
        return json.loads(json.dumps(self.state))

    def history(self):
        path = self.components / component.DEFAULT_COMPONENT / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def versions(self):
        path = self.components / component.DEFAULT_COMPONENT / "versions"
        return sorted(p.name for p in path.iterdir()) if path.exists() else []

    def logs(self):
        return "daemon log line\n"

    def close(self):
        pass

    def tick(self):
        if not self.running_flag or not self.config:
            return
        if not self.state.get("initialized"):
            start = 0 if "replay_history" in self.faults else len(self.chain.deployments)
            self.state = {"initialized": True, "cursor": start, "deployments": {}}
            return
        device = self.config["device_object_id"]
        while self.state["cursor"] < len(self.chain.deployments):
            record = self.chain.deployments[self.state["cursor"]]
            self.state["cursor"] += 1
            if record not in self.chain.objects:
                continue
            if record in self.state["deployments"] and "replay_on_restart" not in self.faults:
                continue  # The journal prevents reapplying redelivered work.
            if self.chain.targets(record, device) or "apply_untargeted" in self.faults:
                status, error = self.apply(self.chain.objects[record]["fields"])
                self.state["deployments"][record] = {"status": status, "reported": False, "error": error}
        if self.chain.balances.get(self.address_value, 0) > 0 or "report_without_gas" in self.faults:
            for record, outcome in self.state["deployments"].items():
                if not outcome["reported"] and "never_report" not in self.faults:
                    status = 1 if "report_failed_as_applied" in self.faults else outcome["status"]
                    self.chain.receipts[record] = {device: status}
                    outcome["reported"] = True

    def apply(self, fields):
        recipe = self.walrus.blobs[fields["recipe_blob_id"]]
        if hashlib.sha256(recipe).hexdigest() != fields["recipe_sha256"] and "ignore_recipe_digest" not in self.faults:
            return 2, "recipe SHA-256 mismatch"
        document = json.loads(recipe)
        artifact = self.walrus.blobs[document["artifactBlobId"]]
        if hashlib.sha256(artifact).hexdigest() != fields["artifact_sha256"] and "ignore_artifact_digest" not in self.faults:
            return 2, "SHA-256 mismatch"
        path = self.root / "artifact"
        path.write_bytes(artifact)
        result = subprocess.run(["/bin/sh", str(component.UPDATER), "--root", str(self.components), str(path)],
                                capture_output=True, text=True, timeout=30)
        return (1, None) if result.returncode == 0 else (2, f"apply command failed with exit code {result.returncode}")


def fake_facts(manifest):
    origins = [{"module": n.split("::")[0], "datatype": n.split("::")[1], "package": o} for n, o in manifest["type_origins"].items()]
    registry = manifest["shared_objects"]["registry"]
    objects = {
        dm.object_id(manifest["call_package_id"]): {"kind": "package", "version": manifest["package_version"], "type_origins": origins},
        dm.object_id(manifest["upgrade_cap_id"]): {"kind": "object", "type": "0x2::package::UpgradeCap", "owner": {"address": OPERATOR},
                                                   "upgrade_cap": {"package": manifest["call_package_id"], "version": manifest["package_version"]}},
        dm.object_id(registry["id"]): {"kind": "object", "owner": {"shared": registry["initial_shared_version"]},
                                       "type": manifest["type_origins"]["device_registry::DeviceRegistry"] + "::device_registry::DeviceRegistry"},
    }
    return lambda url, *ids: {"chain_identifier": manifest["chain_identifier"], "objects": objects}


class FleetRunTests(unittest.TestCase):
    def setup(self, faults=(), wallet="scripted", **profile_changes):
        w = Workspace(self)
        document = w.profile(**profile_changes)
        if wallet == "extension":
            document["wallet"] = {"mode": "extension", "address": OPERATOR, "client_config": "client.yaml",
                                  "dapp_url": "http://127.0.0.1:3000"}
        self.profile = w.validate(document)
        self.chain, self.walrus = FakeChain(), FakeWalrus()
        self.device = FakeDevice(self, self.chain, self.walrus, faults)
        self.cli = FakeCli(self.chain)
        self.args = SimpleNamespace(identity=None, allow_incomplete=False, settle_seconds=0)

    def wait(self, condition, message, device=None, timeout=None):
        for _ in range(20):
            self.device.tick()
            if condition():
                return
        raise ea.CaseFailure(message)

    def run_fleet(self, prompt=None, read_facts=None):
        return ea.execute(self.profile, self.args, device=self.device, cli=self.cli, walrus=self.walrus, prompt=prompt,
                          read_facts=read_facts or fake_facts(self.profile["manifest"]), wait=self.wait)

    def execute(self, faults=(), wallet="scripted", prompt=None, **changes):
        self.setup(faults, wallet, **changes)
        evidence, code = self.run_fleet(prompt)
        return evidence, code, self.chain, self.device

    def statuses(self, evidence):
        return {id: e["status"] for id, e in evidence["cases"].items()}

    def test_scripted_stand_in_run_passes_every_runnable_case_but_stays_incomplete(self):
        evidence, code, chain, device = self.execute()
        statuses = self.statuses(evidence)
        gaps = ea.mode_gaps({"device": {"mode": "local"}, "wallet": {"mode": "scripted"}, "artifact_profile": "public"})
        self.assertEqual({id: s for id, s in statuses.items() if id not in gaps}, {id: "passed" for id in statuses if id not in gaps})
        self.assertEqual((evidence["verdict"], code), ("incomplete", 3))
        self.assertEqual([r["version"] for r in device.history()], ["1.0.0", "2.0.0", "3.0.0", "3.1.0", "4.0.0"])
        self.assertTrue(all(t["status"] == "success" and t["source"] == "scripted" for t in evidence["transactions"]))
        self.assertGreater(evidence["spent_mist"], 0)
        # Cleanup removed every deployment, the group and the decoy; the device stays.
        self.assertFalse(any(o["type"].endswith(("DeploymentRecord", "DeviceGroup")) for o in chain.objects.values()))
        self.assertIn(dm.object_id(evidence["enrollment"]["device"]), chain.objects)
        self.assertNotIn(device.key, json.dumps(evidence))
        self.assertEqual(evidence["device_log_tail"], "daemon log line\n")

    def test_allow_incomplete_exits_zero_only_without_failures(self):
        self.setup()
        self.args.allow_incomplete = True
        evidence, code = self.run_fleet()
        self.assertEqual((evidence["verdict"], code), ("incomplete", 0))
        self.setup({"never_report"})
        self.args.allow_incomplete = True
        self.assertEqual(self.run_fleet()[1], 1)

    def assert_failed(self, faults, case, **kwargs):
        evidence, code, _, _ = self.execute(faults, **kwargs)
        self.assertEqual(evidence["cases"][case]["status"], "failed", evidence["cases"][case])
        self.assertEqual((evidence["verdict"], code), ("failed", 1))
        return evidence

    def test_tampered_recipe_that_runs_fails(self):
        self.assert_failed({"ignore_recipe_digest"}, "recipe-tamper")

    def test_tampered_artifact_that_runs_fails(self):
        self.assert_failed({"ignore_artifact_digest"}, "artifact-tamper")

    def test_failure_reported_as_applied_fails(self):
        evidence = self.assert_failed({"report_failed_as_applied"}, "updater-install-failure")
        self.assertEqual(evidence["cases"]["updater-health-failure"]["status"], "failed")

    def test_untargeted_deployment_that_runs_fails(self):
        self.assert_failed({"apply_untargeted"}, "unauthorized-target")

    def test_pre_enrollment_deployment_that_runs_fails(self):
        self.assert_failed({"replay_history"}, "baseline-skip")

    def test_report_without_gas_is_not_a_retry(self):
        self.assert_failed({"report_without_gas"}, "report-retry")

    def test_missing_report_fails_the_case(self):
        evidence = self.assert_failed({"never_report"}, "report-retry")
        self.assertEqual(evidence["cases"]["direct-v1"]["status"], "blocked")

    def test_replay_after_restart_fails(self):
        self.assert_failed({"replay_on_restart"}, "daemon-restart")

    def test_changed_key_fails(self):
        self.assert_failed({"rotate_key"}, "key-journal-preserved")

    def test_failed_execution_effects_stop_the_run(self):
        self.setup()
        self.chain.fail_calls.add("device_group::create_group")
        evidence, code = self.run_fleet()
        self.assertEqual(code, 1)
        self.assertEqual(evidence["cases"]["enrollment"]["status"], "failed")
        self.assertIn("execution effects", evidence["cases"]["enrollment"]["detail"])
        self.assertEqual([t["status"] for t in evidence["transactions"] if t["label"] == "create group"], ["failure"])

    def test_wrong_chain_or_account_fails_preflight(self):
        self.setup()
        evidence, code = self.run_fleet(read_facts=lambda url, *ids: {"chain_identifier": "deadbeef", "objects": {}})
        self.assertEqual((evidence["cases"]["preflight"]["status"], code), ("failed", 1))
        self.assertIn("deadbeef", evidence["cases"]["preflight"]["detail"])
        self.assertEqual(evidence["transactions"], [])
        self.setup()
        self.cli.active_address = lambda: object_id(0xbad)
        evidence, code = self.run_fleet()
        self.assertIn("active address", evidence["cases"]["preflight"]["detail"])
        self.assertEqual(evidence["transactions"], [])

    def test_interrupted_run_leaves_checkpointed_evidence_naming_its_objects(self):
        self.setup()
        self.chain.fail_calls.add("deployment_manager::delete_deployment")
        path = Path(tempfile.mkdtemp(prefix="suifleet-checkpoint-")) / "evidence.json"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        evidence, code = ea.execute(self.profile, self.args, device=self.device, cli=self.cli, walrus=self.walrus,
                                    read_facts=fake_facts(self.profile["manifest"]), wait=self.wait, evidence_path=path)
        self.assertEqual(evidence["cases"]["cleanup"]["status"], "failed")
        self.assertEqual(len(evidence["leftover_objects"]["deployments"]), 9)
        partial = json.loads(path.read_text())
        self.assertEqual(partial["verdict"], "in-progress")
        self.assertEqual(partial["test_objects"]["deployments"], evidence["leftover_objects"]["deployments"])
        self.assertTrue(partial["test_objects"]["group"] and partial["test_objects"]["decoy"])

    def test_spend_ceiling_is_enforced(self):
        evidence, code, chain, _ = self.execute(budget={"max_spend_mist": 3_000_000})
        self.assertEqual(code, 1)
        self.assertLessEqual(evidence["spent_mist"], 3_000_000)
        self.assertTrue(any("allowance" in e["detail"] for e in evidence["cases"].values()))

    def test_existing_foreign_enrollment_is_not_replaced(self):
        self.setup()
        self.device.config = {"device_object_id": object_id(0x999), "device_cap_id": object_id(0x998)}
        evidence, code = self.run_fleet()
        self.assertEqual(code, 3)
        self.assertIn("allow_replace_enrollment", evidence["cases"]["enrollment"]["detail"])
        self.assertEqual(self.device.config["device_object_id"], object_id(0x999))

    def test_encrypted_profile_executes_nothing(self):
        evidence, code, chain, device = self.execute(artifact_profile="sf022-encrypted")
        self.assertEqual((evidence["verdict"], code), ("incomplete", 3))
        self.assertEqual(evidence["transactions"], [])
        self.assertEqual(chain.calls, [])
        self.assertTrue(all(s in ("blocked", "skipped") for s in self.statuses(evidence).values()))

    def extension_prompt(self, chain, operator=OPERATOR, skip=None, alter=None, cost=None, answer=None):
        """Plays the operator: performs each dApp step on the fake chain, returns its digest.

        alter(step, call) may change a deployment call the way a wrong dApp action would;
        cost overrides the computation cost; answer replaces the digest for matching steps."""
        cli = FakeCli(chain)
        steps = []

        def prompt(step):
            steps.append(step)
            if skip and skip in step:
                return None
            if answer and answer[0] in step:
                return answer[1]
            step = step.split(". Keep the wallet's gas budget")[0]
            if "register device address" in step:
                address = step.split("address ")[1].split()[0]
                result = cli.ptb(["--move-call", "p::device_registry::register_device", "@r", '"n"', f"@{address}", '"a"', "@0x6",
                                  "--assign", "device", "--move-call", "p::device_cap::issue_cap", "device",
                                  "--move-call", "p::device_registry::activate_device", "@r", "device", "--assign", "owner",
                                  "--transfer-objects", "[owner]", f"@{OPERATOR}"])
            elif "create a group" in step:
                result = cli.ptb(["--move-call", "p::device_group::create_group", '"g"', '"d"', "@0x6"])
            elif step.startswith("Add device"):
                device, group = step.split()[2], step.split()[-1]
                owner_cap = next(i for i, o in chain.objects.items() if o["type"].endswith("DeviceOwnerCap"))
                result = cli.ptb(["--move-call", "p::device_registry::borrow_device", "@r", f"@{owner_cap}", "--assign", "loaned",
                                  "--move-call", "p::device_group::add_device", f"@{group}", "loaned.0"])
            else:
                version = step.split("version ")[1].split()[0]
                recipe_blob = step.split("recipe blob ")[1].split(",")[0]
                artifact = step.split("artifact SHA-256 ")[1].split(",")[0]
                recipe = step.split("recipe SHA-256 ")[1].strip()
                target = step.split(" to ")[1].split(":")[0].split()
                devices = f"vector[@{target[1]}]" if target[0] == "device" else "vector[]"
                groups = f"vector[@{target[1]}]" if target[0] == "group" else "vector[]"
                call = ["--move-call", "p::deployment_manager::create_deployment_verified", '"suifleet-test-app"',
                        f'"{version}"', f'"{recipe_blob}"', f'"{artifact}"', f'"{recipe}"', groups, devices, "@0x6"]
                result = cli.ptb(alter(step, call) if alter else call)
            if cost:
                result["effects"]["gasUsed"]["computationCost"] = str(cost)
            result.setdefault("transaction", {"data": {"sender": operator}})
            return result["digest"]
        prompt.steps = steps
        return prompt

    def test_extension_wallet_steps_are_verified_on_chain(self):
        self.setup(wallet="extension")
        prompt = self.extension_prompt(self.chain)
        evidence, code = self.run_fleet(prompt)
        statuses = self.statuses(evidence)
        for id in ("enrollment", "direct-v1", "group-v2", "baseline-skip", "daemon-restart", "extension-wallet"):
            self.assertEqual(statuses[id], "passed", (id, evidence["cases"][id]))
        for id in ("recipe-tamper", "artifact-tamper", "report-retry"):
            self.assertEqual(statuses[id], "skipped", id)
        self.assertEqual(statuses["cleanup"], "skipped", "extension deployments are deleted in the dApp")
        sources = {t["source"] for t in evidence["transactions"]}
        self.assertEqual(sources, {"extension-wallet", "scripted"}, "device gas comes from the test account")
        self.assertEqual(sum(t["source"] == "extension-wallet" for t in evidence["transactions"]), 6)
        self.assertEqual(evidence["verdict"], "incomplete")
        self.assertTrue(any("register device address" in step for step in prompt.steps))

    def test_missing_extension_digest_is_blocked_not_passed(self):
        self.setup(wallet="extension")
        evidence, code = self.run_fleet(self.extension_prompt(self.chain, skip="version 2.0.0"))
        self.assertEqual(evidence["cases"]["direct-v1"]["status"], "passed")
        self.assertEqual(evidence["cases"]["group-v2"]["status"], "blocked")
        self.assertEqual(evidence["cases"]["extension-wallet"]["status"], "blocked")
        self.assertEqual((evidence["verdict"], code), ("incomplete", 3))

    def test_extension_transactions_are_charged_to_the_allowance(self):
        self.setup(wallet="extension")
        prompt = self.extension_prompt(self.chain)
        evidence, code = self.run_fleet(prompt)
        extension = [t for t in evidence["transactions"] if t["source"] == "extension-wallet"]
        self.assertTrue(extension and all(t["gas_mist"] == 1400 and t["status"] == "success" for t in extension))
        charged = sum(t.get("gas_mist", 0) + t.get("funding_mist", 0) for t in evidence["transactions"])
        self.assertEqual(evidence["spent_mist"], charged)
        self.assertTrue(all(f"at or below {ea.EXTENSION_GAS} MIST" in step for step in prompt.steps))

    def test_extension_cost_beyond_its_reservation_is_charged_and_fails(self):
        self.setup(wallet="extension")
        evidence, code = self.run_fleet(self.extension_prompt(self.chain, cost=ea.EXTENSION_GAS + 5_000_000))
        self.assertEqual(code, 1)
        self.assertIn("exceeds", evidence["cases"]["enrollment"]["detail"])
        self.assertGreaterEqual(evidence["spent_mist"], ea.EXTENSION_GAS + 5_000_000)

    def test_exhausted_allowance_invites_no_further_extension_step(self):
        self.setup(wallet="extension", budget={"max_spend_mist": 3_000_000})
        prompt = self.extension_prompt(self.chain, cost=1_500_000)
        evidence, code = self.run_fleet(prompt)
        self.assertEqual(code, 1)
        self.assertEqual(len(prompt.steps), 1, "only the first step fitted the allowance")
        self.assertIn("allowance", evidence["cases"]["enrollment"]["detail"])
        self.assertLessEqual(evidence["spent_mist"], 3_000_000)

    def test_unverifiable_extension_answers_keep_their_reservation(self):
        for answer, expected in (("not a digest", "blocked"), ("9" * 44, "failed")):
            self.setup(wallet="extension")
            evidence, code = self.run_fleet(self.extension_prompt(self.chain, answer=("register device", answer)))
            self.assertEqual(evidence["cases"]["enrollment"]["status"], expected, answer)
            self.assertEqual(evidence["transactions"][0]["status"], "unknown")
            self.assertEqual(evidence["spent_mist"], ea.EXTENSION_GAS)
        # A blank answer means nothing was signed and releases the reservation.
        self.setup(wallet="extension")
        evidence, _ = self.run_fleet(self.extension_prompt(self.chain, skip="register device"))
        self.assertEqual((evidence["transactions"][0]["status"], evidence["spent_mist"]), ("not_submitted", 0))

    def test_extension_digest_reused_for_another_step_fails(self):
        self.setup(wallet="extension")
        evidence, code = self.run_fleet(self.extension_prompt(self.chain, answer=("create a group", fake_digest(0))))
        self.assertEqual(code, 1)
        self.assertIn("already used", evidence["cases"]["enrollment"]["detail"])

    def assert_extension_deployment_rejected(self, case, alter, detail):
        self.setup(wallet="extension")
        evidence, code = self.run_fleet(self.extension_prompt(self.chain, alter=alter))
        self.assertEqual((evidence["cases"][case]["status"], code), ("failed", 1), evidence["cases"][case])
        self.assertIn(detail, evidence["cases"][case]["detail"])
        self.assertNotEqual(evidence["cases"]["extension-wallet"]["status"], "passed")

    def test_direct_deployment_instead_of_the_group_fails(self):
        def direct(step, call):
            if "version 2.0.0" in step:  # The device itself instead of its group.
                device = next(i for i, o in self.chain.objects.items() if o["type"] == "device_registry::Device")
                call[7], call[8] = "vector[]", f"vector[@{device}]"
            return call
        self.assert_extension_deployment_rejected("group-v2", direct, "targets devices")

    def test_wrong_component_or_recipe_digest_fails(self):
        def component_name(step, call):
            if "version 1.0.0" in step:
                call[2] = '"another-app"'
            return call
        self.assert_extension_deployment_rejected("direct-v1", component_name, "component/version/recipe/artifact")

        def digest(step, call):
            if "version 1.0.0" in step:
                call[6] = '"' + "0" * 64 + '"'
            return call
        self.assert_extension_deployment_rejected("direct-v1", digest, "binds recipe digest")

    def test_foreign_type_origin_fails_for_either_wallet(self):
        for key in ("deployment_manager::DeploymentRecord", "deployment_manager::TargetGroupsKey"):
            for wallet in ("scripted", "extension"):
                self.setup(wallet=wallet)
                self.chain.origins[key] = object_id(0x77)
                prompt = self.extension_prompt(self.chain) if wallet == "extension" else None
                evidence, code = self.run_fleet(prompt)
                self.assertEqual(code, 1, (key, wallet))
                self.assertIn("type", evidence["cases"]["enrollment"]["detail"])
                if wallet == "scripted":  # A rejected record is still deleted.
                    self.assertFalse(any(o["type"].endswith("DeploymentRecord") for o in self.chain.objects.values()))
                else:
                    self.assertEqual(len(evidence["test_objects"]["deployments"]), 1)

    def test_extension_digest_from_another_signer_fails(self):
        self.setup(wallet="extension")
        evidence, code = self.run_fleet(self.extension_prompt(self.chain, operator=object_id(0xbad)))
        self.assertEqual(code, 1)
        self.assertIn("not the profile's wallet", evidence["cases"]["enrollment"]["detail"])


class ExampleProfileTests(unittest.TestCase):
    def test_examples_validate_once_placeholders_are_filled(self):
        examples = sorted((ROOT / "tests/acceptance/profiles").glob("*.example.json"))
        self.assertEqual(len(examples), 4)
        for path in examples:
            with self.subTest(path.name):
                w = Workspace(self, json.loads(path.read_text())["profile"])
                text = path.read_text()
                for placeholder, value in (("0x<LOCALNET_TEST_ACCOUNT>", OPERATOR), ("<THIS_HOST_LAN_IP>", "192.168.1.10"),
                                           ("<PI_DHCP_ADDRESS>", "192.168.1.50"), ("SHA256:<FINGERPRINT_VERIFIED_ON_THE_PI_CONSOLE>", FINGERPRINT),
                                           ("/home/<you>/.ssh/id_ed25519_suifleet", str(w.dir / "id_ed25519")),
                                           ("<SHA256_OF_/usr/bin/suifleet_FROM_THE_IMAGE_MANIFEST>", "a" * 64)):
                    text = text.replace(placeholder, value)
                self.assertNotIn("<", text)
                document = json.loads(text)
                (w.dir / "wallet").mkdir()
                (w.dir / "wallet/client.yaml").write_text("keystore: {}\n")
                document["device"].pop("root", None)
                if document["device"]["mode"] == "local":
                    document["device"]["daemon"] = "daemon"
                profile = w.validate(document)
                self.assertEqual(profile["artifact_profile"], "public")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/edge_acceptance.py"), *map(str, args)],
                              capture_output=True, text=True, timeout=60)

    def test_plan_lists_every_case_with_its_gap(self):
        w = Workspace(self)
        (w.dir / "profile.json").write_text(json.dumps(w.profile()))
        result = self.run_cli("plan", "--profile", w.dir / "profile.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        for case in ea.CASES:
            self.assertIn(case.id, result.stdout)
        self.assertIn("run      direct-v1", result.stdout)
        self.assertIn("blocked  extension-wallet", result.stdout)
        self.assertIn("locks during run: none", result.stdout)

    def test_hardware_profile_needs_explicit_confirmation(self):
        w = Workspace(self)
        document = w.profile(device=w.ssh_device(), walrus={"mode": "stand-in", "bind": "192.168.1.10"})
        (w.dir / "profile.json").write_text(json.dumps(document))
        for command in ("preflight", "run"):
            extra = ["--evidence", w.dir / "e.json"] if command == "run" else []
            result = self.run_cli(command, "--profile", w.dir / "profile.json", *extra)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--hardware", result.stderr)
        self.assertFalse((w.dir / "e.json").exists())
        plan = self.run_cli("plan", "--profile", w.dir / "profile.json")
        self.assertIn(f"locks during run: {ea.HARDWARE_LOCK}", plan.stdout)

    def test_invalid_profile_exits_2(self):
        w = Workspace(self)
        (w.dir / "profile.json").write_text(json.dumps(w.profile(profile="mainnet")))
        result = self.run_cli("plan", "--profile", w.dir / "profile.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mainnet", result.stderr)


if __name__ == "__main__":
    unittest.main()
