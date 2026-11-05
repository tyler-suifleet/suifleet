#!/usr/bin/env python3
"""Record the suifleet.io guide screencasts on Sui testnet (SF-175).

Drives the real console static export through every step of
site/guide/steps.json with site/guide/record_guide.cjs, against the
package-upgrade live gate's cached fixture for these contracts (no package is
published here; run `make live-gate-package-upgrade BASE_REF=HEAD` first if
`--check` reports none). Run with the pinned development toolchain and the
installed atomic ticket coordinator (including its shared database), holding the
`network:suifleet-testnet` lock (docs/TESTNET.md):

    python3 site/guide/record_guide.py --fresh-device \
      --wallet ~/.config/suifleet/testnet/agent/client.yaml \
      --runtime-dir /private/guide-run --evidence /private/guide-run/evidence.json

The default command also encodes and checks the local site; --capture-only
retains private frames for diagnosis. Follow site/guide/RECORDING.md.

Money: a reusable demo browser key is funded from the wallet (charged to the
run's allowance in full). Its seed stays 0600 beside the wallet, and its
existing balance is charged before any top-up on later runs. As registry administrator, the wallet
also grants the new customer a small device/upload entitlement, as SuiFleet
does for a subscriber. Keys never reach the browser, the frames or the logs.
"""
import argparse
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import deployment_manifest as dm  # noqa: E402
from live_common import DAEMON, Live, Spent, Walrus, pending_key, plain  # noqa: E402
from live_package_upgrade import contracts_digest, load_state  # noqa: E402
from live_wallet_gate import head_fixture  # noqa: E402

DAEMON = Path(os.environ.get("SUIFLEET_GUIDE_DAEMON", str(DAEMON)))

FUNDING = 220_000_000  # Target browser balance; existing gas is charged too.
ENTITLEMENT = ("20", "50000000", "1")  # devices, upload bytes, period


def assert_lease():
    token = os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN")
    if not token:
        raise RuntimeError("testnet mutation lease required")
    try:
        result = subprocess.run(["ticket", "--project", "suifleet", "lock-assert", "network:suifleet-testnet", "--lock-token", token],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("testnet mutation lease refused") from None
    if result.returncode:
        raise RuntimeError("testnet mutation lease refused")


def node(runtime, phase, manifest_file, seed_file, env_extra):
    env = {**os.environ, "GUIDE_MANIFEST_FILE": str(manifest_file), "GUIDE_SEED_FILE": str(seed_file),
           "GUIDE_EXPORT": str(ROOT / "dapp" / "out"), "GUIDE_OUT": str(runtime), "GUIDE_PHASE": phase,
           "GUIDE_STATE": str(runtime / "browser-state.json"), "GUIDE_DEVICE_NAME": "guide-linux-" + hashlib.sha256(str(runtime).encode()).hexdigest()[:6], "GUIDE_VERSION": "1.0.0-guide.r" + hashlib.sha256(str(runtime).encode()).hexdigest()[:12], **env_extra}
    result = subprocess.run(["node", str(ROOT / "site/guide/record_guide.cjs")], env=env, text=True, timeout=3600)
    evidence = json.loads((runtime / f"evidence-{phase}.json").read_text())
    return result.returncode, evidence


def created(live, digest, suffix):
    found = live.read_back(digest)
    assert found, f"transaction {digest} is not readable"
    return [c["objectId"] for c in found.get("objectChanges", []) if c["type"] == "created" and c["objectType"].endswith(suffix)]


def run_recorded_device(live, runtime, manifest_file, device_key, device_id, cap_id, deployment_id, persist):
    """A disposable Linux client, not physical-Pi acceptance. Funding is bounded."""
    binary = Path(os.environ.get("SUIFLEET_GUIDE_DAEMON", str(DAEMON)))
    if not binary.is_file():
        raise RuntimeError("build the current daemon before recording its result")
    address = subprocess.check_output([str(binary), "--derive-address", str(device_key)], text=True).strip()
    previous = live.evidence.get("recorded_device")
    if previous:
        if previous.get("deployment") != deployment_id or not previous.get("funding_digest"):
            raise RuntimeError("reconcile prior device funding before another attempt")
        if live.executed(previous["funding_digest"])["effects"]["status"]["status"] != "success":
            raise RuntimeError("device funding outcome is not verified")
    else:
        live.evidence["recorded_device"] = {"device": device_id, "address": address, "deployment": deployment_id, "funding_mist": 50_000_000}
        persist()
        result = live.transaction("fund the disposable recorded Linux device", "--split-coins", "gas", "[50000000]", "--assign", "funds", "--transfer-objects", "[funds.0]", "@" + address, budget=10_000_000, funding=50_000_000)
        live.evidence["recorded_device"]["funding_digest"] = result["digest"]
        persist()
    config = runtime / "device-config.toml"
    config.write_text('[sui]\nmanifest_path = ' + json.dumps(str(manifest_file)) + '\n[device]\nkeypair_path = ' + json.dumps(str(device_key))
        + '\ndevice_object_id = ' + json.dumps(device_id) + '\ndevice_cap_id = ' + json.dumps(cap_id)
        + '\n[apply]\ncommand = "/bin/sh"\ntimeout_seconds = 30\nallow_recipe_commands = true\n'
        + '[runtime]\nstate_dir = ' + json.dumps(str(runtime / "device-state")) + '\npoll_seconds = 86400\nevent_discovery_seconds = 1\nreplay_history = true\n')
    config.chmod(0o600)
    with (runtime / "device.log").open("w") as output:
        process = subprocess.Popen([str(binary)], env={"PATH": os.environ["PATH"], "HOME": str(runtime / "device-home"), "EDGE_CONFIG": str(config)}, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                live.assert_lock()
                if live.receipts(deployment_id).get(dm.object_id(device_id)) == 1:
                    live.evidence["recorded_device"].update(applied=True, remaining_balance_mist=live.balance(address))
                    persist(); return
                if process.poll() is not None:
                    raise RuntimeError("recorded device exited before a verified apply; inspect its private log")
                time.sleep(3)
            raise RuntimeError("recorded device has no verified Applied receipt; do not narrate success")
        finally:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()


def build_console(manifest, profile=None):
    """The console export the screencasts show, built for this manifest."""
    env = {**os.environ, "NEXT_TELEMETRY_DISABLED": "1", "NEXT_PUBLIC_DEPLOYMENT_MANIFEST": json.dumps(manifest)}
    for name in [k for k in env if k.startswith("NEXT_PUBLIC_") and k != "NEXT_PUBLIC_DEPLOYMENT_MANIFEST"]:
        del env[name]  # Only the manifest: no Google, Seal or fleet-data overrides from the host.
    if profile:
        for line in (Path(profile) / "frontend.env").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                if not name.startswith("NEXT_PUBLIC_"):
                    raise RuntimeError("recording profile contains a non-public environment setting")
                env[name] = shlex.split(value)[0]
        env["NEXT_PUBLIC_DEPLOYMENT_MANIFEST"] = json.dumps(manifest)
    subprocess.run([str(ROOT / "dapp/node_modules/.bin/next"), "build"], cwd=ROOT / "dapp", env=env, check=True,
                   stdout=subprocess.DEVNULL)


def run(args):
    runtime = Path(args.runtime_dir).resolve()
    if runtime == ROOT or ROOT in runtime.parents:
        raise RuntimeError("private recording runtime must be outside the checkout")
    resume = getattr(args, "resume", False)
    if getattr(args, "resume_new_build", False) and (not resume or not getattr(args, "resume_build_reason", "").strip()):
        raise RuntimeError("a source-change resume requires --resume and an explicit --resume-build-reason")
    if resume and args.faucet:
        raise RuntimeError("resume cannot request another faucet attempt")
    if runtime.exists() and any(runtime.iterdir()) and not resume:
        raise RuntimeError("runtime directory must be empty; reconcile prior evidence before another run")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    live = Live(args.wallet, "testnet", args.max_spend_mist)
    live.assert_lock = assert_lease
    evidence_path = Path(args.evidence) if args.evidence else runtime / "evidence.json"
    def persist():
        # Includes any in-flight full reservation, not a previous settled value.
        live.evidence["allowance_used_mist"] = live.ledger.used
        temporary = evidence_path.with_suffix(".json.tmp")
        temporary.touch(mode=0o600)
        temporary.chmod(0o600)
        with temporary.open("w") as output:
            json.dump(live.evidence, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(evidence_path)
    def finish():
        end = live.balance()
        live.evidence.update(end_balance_mist=end, balance_delta_mist=live.start_balance - end,
                             spent_mist=sum(t.get("gas_mist", t.get("gas_budget_mist", 0)) + t.get("funding_mist", 0) for t in live.evidence["transactions"]),
                             allowance_used_mist=live.ledger.used,
                             finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        persist()
        print(f"allowance used {live.ledger.used} MIST; evidence: {evidence_path}")
    original_signer = live.signer
    def journaled_signer(config, address):
        sign = original_signer(config, address)
        def journal_then_sign(tx_bytes):
            live.assert_lock()
            digest = live.decode(tx_bytes)["digest"]
            live.evidence.setdefault("signed_transactions", []).append({"digest": digest})
            persist()
            return sign(tx_bytes)
        return journal_then_sign
    live.signer = journaled_signer
    if not args.check:
        if not os.environ.get("SUIFLEET_TESTNET_LOCK_TOKEN"):
            raise RuntimeError("network:suifleet-testnet lease required")
        live.assert_lock()
    live.preflight(auto_fund=args.faucet and not args.check)
    prior = json.loads(evidence_path.read_text()) if resume else None
    if prior:
        if prior.get("chain") != live.chain or prior.get("operator") != live.operator or prior.get("max_spend_mist") != args.max_spend_mist:
            raise RuntimeError("resume identity or allowance differs from the original run")
        used = prior.get("allowance_used_mist")
        if not isinstance(used, int) or not 0 <= used <= args.max_spend_mist:
            raise RuntimeError("original run has no settled allowance; reconcile it explicitly")
        live.evidence = prior
        live.ledger.evidence = prior["transactions"]
        live.ledger.used = used
        live.start_balance = prior["start_balance_mist"]
    walrus = Walrus("testnet")
    if getattr(args, "profile", None):
        manifest = json.loads((Path(args.profile) / "deployment.json").read_text())
        dm.verify(manifest)
        if manifest["network"] != "testnet" or manifest["chain_identifier"] != live.chain:
            raise RuntimeError("recording profile is not this testnet")
        fixture = {"head_package": manifest["call_package_id"], "upgrade_cap": manifest["upgrade_cap_id"], "registry": manifest["shared_objects"]["registry"]["id"]}
        if not args.check:
            manifest["walrus"]["publisher_url"] = walrus.working_publisher()
    else:
        state = load_state(Path(args.state) if args.state else live.wallet.parent / "live-state.json", live.chain, live.operator)
        fixture = head_fixture(state, contracts_digest(ROOT / "contracts"))
        if not fixture or not fixture.get("owner_cap"):
            sys.exit("no activated package-upgrade fixture for these contracts; run make live-gate-package-upgrade first or select a verified owned --profile")
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url, package=fixture["head_package"],
                                              upgrade_cap=fixture["upgrade_cap"], registry=fixture["registry"], network="testnet",
                                              publisher=walrus.publisher if args.check else walrus.working_publisher(), aggregator=walrus.aggregator))
    dm.verify(manifest)
    facts = dm.chain_facts(live.rpc_url, fixture["upgrade_cap"])
    if dm.upgrade_cap(facts["objects"][fixture["upgrade_cap"]])[2] != live.operator:
        raise RuntimeError("fixture UpgradeCap is not owned by the dedicated operator")
    if args.check:
        print("PASS: cached fixture, chain, dedicated operator, UpgradeCap, registry and type origins verified; no transactions submitted")
        return
    for group in ("customer", "fleet budget", "catalog"):
        assert all(t in manifest["type_origins"] for t in dm.OPTIONAL_GROUPS[group]), f"fixture lacks {group} types"
    manifest_file = runtime / "deployment.json"
    if resume:
        original_manifest = json.loads(manifest_file.read_text())
        dm.verify(original_manifest)
        if original_manifest["call_package_id"] != fixture["head_package"]:
            raise RuntimeError("resume package differs from the original build")
        manifest = original_manifest
    manifest_file.write_text(json.dumps(manifest))
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if prior and subprocess.run(["git", "diff", "--quiet", prior["console_commit"], commit, "--", "dapp"], cwd=ROOT).returncode:
        if not getattr(args, "resume_new_build", False):
            raise RuntimeError("console changed since the interrupted recording")
        if any(tx.get("status") not in ("success", "failure", "not_submitted") for tx in prior.get("transactions", [])):
            raise RuntimeError("unresolved operator outcome forbids source-change resume")
        for phase in prior.get("browser", {}).get("phases", []):
            if any(tx.get("status") not in ("success", "failure") for tx in phase.get("transactions", [])):
                raise RuntimeError("unresolved browser outcome forbids source-change resume")
        live.evidence.setdefault("source_changes", []).append({"from": prior["console_commit"], "to": commit, "reason": args.resume_build_reason})
        for phase in prior.get("browser", {}).get("phases", []):
            for step in phase.get("steps", []):
                live.evidence.setdefault("video_sources", {}).setdefault(step["step"], {"console_commit": phase.get("console_commit", prior["console_commit"])})
    elif prior:
        commit = prior["console_commit"]
    os.environ["GUIDE_CONSOLE_COMMIT"] = commit
    live.evidence.update(package=fixture["head_package"], registry=fixture["registry"], console_commit=commit,
                         walrus_publisher=manifest["walrus"]["publisher_url"])
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--", "dapp"], cwd=ROOT, text=True).strip():
        raise RuntimeError("console has uncommitted changes; commit the chosen build before recording")
    if any(p.is_file() and not p.name.endswith((".example", ".sample")) for p in (ROOT / "dapp").glob(".env*")):
        raise RuntimeError("local Next.js env files can override reproducible configuration")
    build_console(manifest, getattr(args, "profile", None))
    package, registry = fixture["head_package"], fixture["registry"]

    pending = runtime / "guide-recorder-key.hex" if getattr(args, "fresh_customer", False) else live.wallet.parent / "guide-recorder-key.hex"
    demo_path = runtime / "guide-recorder-demo.json" if getattr(args, "fresh_customer", False) else live.wallet.parent / "guide-recorder-demo.json"
    demo = json.loads(demo_path.read_text()) if demo_path.exists() else {}
    if pending.exists():
        if not demo or demo.get("chain") != live.chain or demo.get("package") != package:
            raise RuntimeError("retained demo key has no matching fixture journal; reconcile explicitly")
        previous = json.loads(Path(demo["evidence"]).read_text())
        for tx in previous.get("signed_transactions", []):
            status = live.read_back(tx["digest"]).get("effects", {}).get("status", {}).get("status")
            if status not in ("success", "failure"):
                raise RuntimeError("prior operator transaction unresolved; reconcile before any new run")
        for phase in sorted(previous.get("browser", {}).get("phases", []), key=lambda p: p.get("phase") != "rest"):
            for tx in phase["transactions"]:
                status = live.read_back(tx["digest"]).get("effects", {}).get("status", {}).get("status")
                if status not in ("success", "failure"):
                    raise RuntimeError("prior demo transaction unresolved; no replay permitted")
                if status == "success" and tx.get("step") == "register-device":
                    ids = created(live, tx["digest"], "::device_registry::Device")
                    if len(ids) == 1:
                        demo["device"] = ids[0]
                if status == "success" and tx.get("step") == "access-policy" and not demo.get("access_policy"):
                    ids = created(live, tx["digest"], "::seal_policy::AccessPolicy")
                    if len(ids) == 1:
                        demo["access_policy"] = ids[0]
                if status == "success" and tx.get("step") == "deploy-group" and not demo.get("group"):
                    ids = created(live, tx["digest"], "::device_group::DeviceGroup")
                    if len(ids) == 1:
                        demo["group"] = ids[0]
                if status == "success" and tx.get("step") == "enroll-batch":
                    ids = created(live, tx["digest"], "::enrollment::EnrollmentPolicy")
                    if len(ids) == 1:
                        demo["policy"] = ids[0]
        seed = pending.read_text().strip()
    else:
        seed = os.urandom(32).hex()
        pending_key(pending, seed)
    seed_file = runtime / "browser.key"
    seed_file.touch(mode=0o600)
    seed_file.write_text(seed + "\n")
    browser_address = subprocess.check_output([str(DAEMON), "--derive-address", str(seed_file)], text=True).strip()
    fresh_device = bool(prior.get("fresh_device", False)) if prior else getattr(args, "fresh_device", False)
    live.evidence["fresh_device"] = fresh_device
    device_key = runtime / "device.key" if fresh_device else pending.with_name("guide-recorder-device.key")
    device_address = subprocess.check_output([str(DAEMON), "--derive-address" if device_key.exists() else "--provision", str(device_key)], text=True).strip()
    cached_gas = live.balance(browser_address)
    if not resume:
        live.ledger.grant(cached_gas)
        live.evidence["cached_demo_gas_mist"] = cached_gas
    funding = getattr(args, "resume_top_up_mist", 0) if resume else max(0, FUNDING - cached_gas)
    if funding < 0 or (not resume and getattr(args, "resume_top_up_mist", 0)):
        raise RuntimeError("an explicit resume top-up must be nonnegative and used with --resume")
    demo.update(chain=live.chain, package=package, evidence=args.evidence or str(runtime / "evidence.json"))
    demo_path.touch(mode=0o600)
    demo_path.write_text(json.dumps(demo))
    persist()
    try:
        if funding:
            live.transaction("top up the reusable guide browser key", "--split-coins", "gas", f"[{funding}]", "--assign", "fund",
                             "--transfer-objects", "[fund]", f"@{browser_address}", budget=10_000_000, funding=funding)
    except Exception:
        finish()
        raise
    browser = {"address": browser_address, "phases": []}
    live.evidence["browser"] = browser
    failure = None
    try:
        if resume and (runtime / "frames/sign-in/frames.json").exists():
            code, sign_in = 0, json.loads((runtime / "evidence-sign-in.json").read_text())
        else:
            code, sign_in = node(runtime, "sign-in", manifest_file, seed_file, {})
        browser["phases"].append(sign_in)
        assert code == 0, "sign-in phase failed"
        customers = created(live, sign_in["transactions"][-1]["digest"], "::customer::Customer") if sign_in["transactions"] else [demo["customer"]]
        assert len(customers) == 1, f"expected one new Customer, found {customers}"
        demo["customer"] = customers[0]
        demo_path.write_text(json.dumps(demo))
        live.check(f"verified customer {customers[0]}; {'reused' if not sign_in['transactions'] else 'created in this recording'}")

        if resume and not demo.get("policy"):
            policies = []
            for phase in [json.loads(path.read_text()) for path in runtime.glob("evidence-*.json")]:
                for transaction in phase.get("transactions", []):
                    if transaction.get("step") == "enroll-batch" and transaction.get("status") == "success":
                        policies += created(live, transaction["digest"], "::enrollment::EnrollmentPolicy")
            if len(set(policies)) == 1:
                demo["policy"] = policies[0]; demo_path.write_text(json.dumps(demo))

        rest_steps = ["capacity-plan", "fund", "register-device", "enroll-batch", "publish-version", "deploy-group", "access-policy", "fleet-settings"]
        remaining = [s for s in rest_steps if not (runtime / "frames" / s / "frames.json").exists()] if resume else rest_steps
        resume_phase = "resume-rest-" + str(len(list(runtime.glob("evidence-resume-rest*.json"))) + 1)
        if remaining:
            code, rest = node(runtime, resume_phase if resume else "rest", manifest_file, seed_file,
                              {"GUIDE_DEVICES": json.dumps([device_address]), "GUIDE_FRESH_DEVICE": "1" if fresh_device else "0", "GUIDE_DEVICE_NAME": "guide-linux-" + hashlib.sha256(str(runtime).encode()).hexdigest()[:6] if fresh_device else "guide-linux-001", "GUIDE_DEMO_POLICY": demo.get("policy", ""), "GUIDE_DEMO_GROUP": demo.get("group", ""), "GUIDE_ACCESS_POLICY": demo.get("access_policy", ""), "GUIDE_STEPS": ",".join(remaining)})
        else:
            code, rest = 0, {"transactions": []}
        browser["phases"].append(rest)
        assert code == 0, "recording phase failed"
        by_step = {}
        for phase in [json.loads(p.read_text()) for p in runtime.glob("evidence-*.json")]:
            if phase.get("historical_only"):
                continue
            for tx in phase["transactions"]:
                by_step.setdefault(tx["step"], []).append(tx["digest"])
        devices = created(live, by_step["register-device"][-1], "::device_registry::Device") if by_step.get("register-device") else [demo["device"]]
        versions = created(live, by_step["publish-version"][-1], "::component_catalog::ComponentVersion")
        assert len(devices) == 1 and len(versions) == 1, (devices, versions)
        demo["device"] = devices[0]
        demo_path.write_text(json.dumps(demo))
        device_caps = created(live, by_step["register-device"][-1], "::device_cap::DeviceCap")
        deployments = created(live, by_step["deploy-group"][-1], "::deployment_manager::DeploymentRecord")
        if len(device_caps) != 1 or len(deployments) != 1:
            raise RuntimeError("expected one device capability and one demonstrated deployment")
        run_recorded_device(live, runtime, manifest_file, device_key, devices[0], device_caps[0], deployments[0], persist)
        code, result_view = node(runtime, "device-result", manifest_file, seed_file,
            {"GUIDE_STEPS": "deploy-group", "GUIDE_APPEND_STEP": "deploy-group", "GUIDE_RESULT_ONLY": "1"})
        browser["phases"].append(result_view)
        assert code == 0, "verified device result was not captured in the console"
        fields = plain(live.object(versions[0])["content"])
        extra = {"recipe": fields["recipe_blob_ids"][0], "recipeSha256": fields["recipe_sha256s"][0],
                 "artifactSha256": fields["artifact_sha256s"][0], "cohorts": [devices[0]]}
        previous_rollouts = []
        for digest in by_step.get("staged-rollout", []):
            previous_rollouts += created(live, digest, "::deployment_manager::DeploymentRecord")
        if by_step.get("staged-rollout") and not previous_rollouts:
            raise RuntimeError("prior rollout transactions have no recognized record; reconcile before recording again")
        if previous_rollouts:
            extra["rollout"] = previous_rollouts[0]
        rollout_phase = "rollout-resume-" + str(len(list(runtime.glob("evidence-rollout*.json")))) if (runtime / "evidence-rollout.json").exists() else "rollout"
        code, rollout = node(runtime, rollout_phase, manifest_file, seed_file,
                             {"GUIDE_EXTRA": json.dumps(extra), "GUIDE_STEPS": "staged-rollout",
                              "GUIDE_APPEND_STEP": "staged-rollout" if previous_rollouts else ""})
        browser["phases"].append(rollout)
        assert code == 0, "rollout phase failed"
    except Exception as error:
        failure = error
    finally:
        # Recover phase evidence even if Node timed out or failed before returning.
        browser["phases"] = [json.loads(p.read_text()) for p in sorted(runtime.glob("evidence-*.json"))]
        for phase in browser["phases"]:
            for step in phase.get("steps", []):
                if phase.get("console_commit"):
                    live.evidence.setdefault("video_sources", {})[step["step"]] = {"console_commit": phase["console_commit"]}
        # Independent effects check of everything the browser key executed.
        for phase in browser["phases"]:
            for tx in phase["transactions"]:
                found = live.read_back(tx["digest"])
                status = (found.get("effects") or {}).get("status", {}).get("status")
                tx["effects_status"] = status
                if status == "success" and tx["step"] == "register-device":
                    ids = [c["objectId"] for c in found.get("objectChanges", []) if c["type"] == "created" and c["objectType"].endswith("::device_registry::Device")]
                    if len(ids) == 1:
                        demo["device"] = ids[0]
                        demo_path.write_text(json.dumps(demo))
                gas = (found.get("effects") or {}).get("gasUsed") or {}
                tx["net_gas_mist"] = (int(gas.get("computationCost", 0)) + int(gas.get("storageCost", 0))
                                      - int(gas.get("storageRebate", 0))) if gas else None
        # Retain the demo signer and objects for subsequent release recordings.
        # Keys remain private beside the wallet. Unknown outcomes never trigger a replay.
        seed_file.unlink(missing_ok=True)
        live.evidence["demo_reused"] = bool(cached_gas)
        live.evidence["browser"]["net_gas_mist"] = sum(tx.get("net_gas_mist") or 0 for p in browser["phases"] for tx in p["transactions"])
        live.evidence["network_fee_mist"] = sum(tx.get("gas_mist") or 0 for tx in live.evidence["transactions"]) + browser["net_gas_mist"]
        finish()
    if failure:
        raise SystemExit(f"recording failed: {failure}")
    bad = [tx["digest"] for p in browser["phases"] for tx in p["transactions"] if tx["effects_status"] != "success"]
    assert not bad, f"transactions without successful effects: {bad}"
    print(f"recorded; frames in {runtime / 'frames'}; encode with site/guide/encode_guide.py")
    if not args.capture_only:
        subprocess.run([sys.executable, str(ROOT / "site/guide/encode_guide.py"), "--runtime-dir", str(runtime), "--evidence", args.evidence or str(runtime / "evidence.json")], check=True)
        subprocess.run([sys.executable, str(ROOT / "site/build.py")], check=True)
        subprocess.run([sys.executable, str(ROOT / "site/check.py"), "--require-videos"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="client.yaml of the dedicated testnet wallet")
    parser.add_argument("--runtime-dir", required=True, help="private directory for the manifest, frames and evidence")
    parser.add_argument("--profile", type=Path, help="Explicit verified existing testnet deployment; does not fabricate an upgrade fixture")
    parser.add_argument("--fresh-customer", action="store_true", help="Use a new private recording identity and customer; existing recordings remain unchanged")
    parser.add_argument("--state", help="package-upgrade fixture cache (default: live-state.json beside the wallet)")
    parser.add_argument("--max-spend-mist", type=int, default=250_000_000)
    parser.add_argument("--faucet", action="store_true", help="request testnet SUI once if the balance is below the allowance")
    parser.add_argument("--check", action="store_true", help="read-only chain/fixture preflight; no funding, build or recording")
    parser.add_argument("--capture-only", action="store_true", help="retain private frames without encoding into the local site")
    parser.add_argument("--resume-new-build", action="store_true", help="Explicitly resume after a fixed UI defect; preserve per-video provenance and original allowance")
    parser.add_argument("--resume-build-reason", help="Required reason for an explicit changed-source resume; unresolved effects still refuse")
    parser.add_argument("--resume", action="store_true", help="resume an effects-reconciled interrupted run without topping up or resetting its allowance")
    parser.add_argument("--fresh-device", action="store_true", help="record an actual new demo device registration; reuse the other fleet objects")
    parser.add_argument("--resume-top-up-mist", type=int, default=0, help="explicit extra demo funding charged to the ORIGINAL run allowance; never raises its ceiling")
    parser.add_argument("--evidence", help="write evidence JSON here")
    try:
        run(parser.parse_args())
    except Spent as error:
        sys.exit(f"stopped: {error}")
