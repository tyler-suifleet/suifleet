#!/usr/bin/env python3
"""SF-154 release rehearsal on a disposable localnet: the release planner and
the shared publication pipeline (scripts/release_plan.py, scripts/redeploy.sh)
on the real contracts, Sui CLI and both clients.

  1. plan a fresh publish (pinned bytecode digest) and render it for review;
  2. refuse a plan for another custody account and one that is not ready,
     before any transaction (nothing written, no gas);
  3. publish under the plan; record the transaction, effects and plan with the
     release; daemon and frontend verify the generated configs;
  4. refuse the replayed publish plan;
  5. plan a compatible upgrade adding a type, dry-run and execute it under the
     plan; the new type originates in the upgrade, existing origins, registry
     and UpgradeCap are kept, and the superseded release is rejected as stale;
  6. refuse a plan whose sources dropped a published type (static
     compatibility), and an edited (tampered) plan;
  7. run the old-package identity attacks (identity_lifecycle_localnet.py)
     from the historical pre-identity package (PRIOR_IDENTITY_REF, as in
     test_move_security_localnet.py) to the rehearsed head, on the same chain.

Start the node first (as for test-publish-localnet), then:
make test-release-rehearsal-localnet. Refuses non-loopback endpoints; uses its
own wallet, package copy and records. --evidence FILE writes the plans, digests,
effects and gas as JSON.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

import deployment_manifest as dm

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
FRONTEND = ROOT / "scripts/test_deployment_manifest.cjs"
ENV = "sf-rehearse"
MARKER = "release_rehearsal::RehearsalMarker"
# The pre-identity contracts the SF-008 old-package attacks start from (#82).
PRIOR_IDENTITY_REF = "d3acb1e"


def run(rpc_url, faucet_url, evidence_path, old_package_checks):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("rehearsal requires an isolated loopback node/faucet")
    chain = dm.chain_facts(rpc_url)["chain_identifier"]
    evidence = {"ticket": "SF-154", "revision": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                                                        text=True).strip(),
                "chain": chain, "rpc": rpc_url, "sui": subprocess.check_output(["sui", "--version"], text=True).strip(),
                "plans": {}, "transactions": {}, "refusals": [], "checks": []}

    def passed(message):
        evidence["checks"].append(message)
        print("PASS: " + message)

    with tempfile.TemporaryDirectory(prefix="suifleet-rehearsal-") as tmp:
        root = Path(tmp)
        wallet = root / "wallet"; wallet.mkdir()
        config = wallet / "client.yaml"

        def client(*args):
            result = subprocess.run(["sui", "client", "--client.config", str(config), *map(str, args)],
                                    text=True, capture_output=True, timeout=300)
            # Wallet creation prints a recovery phrase; never forward output.
            if result.returncode:
                raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}")
            return result.stdout

        # Written directly: interactive wallet creation would contact a public network.
        (wallet / "sui.keystore").write_text("[]")
        config.write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: {ENV}\n  rpc: {rpc_url}\n"
                          f"active_env: {ENV}\nactive_address: null\n")
        address = dm.object_id(json.loads(client("new-address", "ed25519", "rehearsal", "--json"))["address"])
        client("switch", "--address", address)

        def balance():
            gas = json.loads(client("gas", "--json"))
            coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
            return sum(int(c.get("mistBalance", 0)) for c in coins)

        for attempt in range(30):
            if balance() > 0:
                break
            try:
                client("faucet", "--url", faucet_url)
            except RuntimeError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("local faucet did not supply gas")

        # The real contracts as a committed checkout: the plan binds a revision.
        package = root / "contracts"
        shutil.copytree(ROOT / "contracts", package, ignore=shutil.ignore_patterns("build", "Published.toml"))
        with (package / "Move.toml").open("a") as manifest:
            manifest.write(f'{ENV} = "{chain}"\n')
        subprocess.run(["sui", "move", "build", "--path", str(package), "-e", ENV, "--silence-warnings"],
                       check=True, capture_output=True, timeout=600)

        def git(*args):
            subprocess.run(["git", "-C", str(package), "-c", "user.name=rehearsal", "-c",
                            "user.email=rehearsal@example.invalid", *args], check=True, capture_output=True)

        # Published.toml is the CLI's record of this disposable chain, not reviewed source.
        (package / ".gitignore").write_text("build/\nPublished.toml\n")
        git("init", "-q"); git("add", "-A"); git("commit", "-qm", "base: repository contracts")

        records, env_file = root / "deployments", root / ".env.local"
        env_file.write_text("NEXT_PUBLIC_WALRUS_EPOCHS=3\n")
        key = root / "device.key"; key.write_text("07" * 32); key.chmod(0o600)
        template = root / "device-template.toml"
        template.write_text(f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "0xde"\n'
                            'device_cap_id = "0xca"\n[apply]\ncommand = "/bin/true"\n')

        def plan(name, *extra, custody=address):
            out = root / f"{name}.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/release_plan.py"), "plan", "--network", "localnet", "--rpc", rpc_url,
                 "--custody", custody, "--env", ENV, "--package", str(package), "--records", str(records),
                 "--device-template", str(template), "--build-tool", "sui", "--out", str(out),
                 "--markdown", str(root / f"{name}.md"), *extra], text=True, capture_output=True, timeout=900)
            if result.returncode not in (0, 2) or not out.exists():
                raise RuntimeError(f"planning {name} failed:\n{result.stderr[-3000:]}")
            value = json.loads(out.read_text())
            assert (result.returncode == 0) == value["ready"], result.stdout
            evidence["plans"][name] = {"digest": value["plan_digest"], "ready": value["ready"], "action": value["action"],
                                       "bytecode": value["revision"]["bytecode"]["digest"],
                                       "findings": [f"{f['severity']}:{f['code']}" for f in value["findings"]],
                                       "new_types": value["compatibility"]["new_types"] if value["action"] == "upgrade"
                                       else len(value["compatibility"]["new_types"]),
                                       "removed_types": value["compatibility"]["removed_types"]}
            return out, value

        def publish(plan_path, *extra, expect=address):
            command = ["bash", str(ROOT / "scripts/redeploy.sh"), "--expect-address", expect, "--network", "localnet",
                       "--client-config", str(config), "--package", str(package), "--records", str(records),
                       "--frontend-env", str(env_file), "--device-template", str(template),
                       "--publisher", "http://127.0.0.1:31415", "--aggregator", "http://127.0.0.1:31416",
                       "--gas-budget", "1000000000", *(["--plan", str(plan_path)] if plan_path else []), *extra]
            return subprocess.run(command, text=True, capture_output=True, timeout=900)

        def state():
            files = {}
            for path in sorted(root.rglob("*")):
                if {"wallet", "build", ".git"} & set(path.parts) or path.suffix == ".md" or path.parent == root \
                        and path.suffix == ".json":
                    continue
                files[str(path.relative_to(root))] = (str(path.readlink()) if path.is_symlink()
                                                      else path.read_bytes() if path.is_file() else None)
            return files

        def refused(label, plan_path, pattern, *extra):
            before, funds = state(), balance()
            result = publish(plan_path, *extra)
            assert result.returncode == 1 and re.search(pattern, result.stderr), result.stdout + result.stderr
            assert state() == before, "a refused run must write nothing"
            assert balance() == funds, "a refused run must spend nothing"
            evidence["refusals"].append({"case": label, "error": result.stderr.strip().splitlines()[0][:300]})

        def verified_by_both_clients(release):
            subprocess.run([str(DAEMON), "--check-config", str(release / "config.toml")], check=True,
                           capture_output=True, text=True, timeout=120)
            subprocess.run(["node", str(FRONTEND), str(release / "deployment.json")], check=True,
                           capture_output=True, text=True, timeout=120)

        def executed(name, release, before):
            transaction = json.loads((release / "transaction.json").read_text())
            assert transaction["effects"]["status"] == {"status": "success"}, transaction
            gas = transaction["effects"]["gasUsed"]
            evidence["transactions"][name] = {
                "digest": transaction["digest"], "status": "success", "gas_used": gas,
                "net_gas_mist": int(gas["computationCost"]) + int(gas["storageCost"]) - int(gas["storageRebate"]),
                "balance_change_mist": balance() - before,
                "plan": json.loads((release / "release-plan.json").read_text())["plan_digest"]}

        # 1. Fresh-publish plan.
        fresh_path, fresh = plan("plan-publish")
        assert fresh["ready"] and fresh["action"] == "publish" and fresh["execution"]["allowed"], fresh["findings"]
        assert MARKER not in fresh["compatibility"]["new_types"]
        passed(f"fresh-publish plan ready ({fresh['plan_digest']}), bytecode {fresh['revision']['bytecode']['digest'][:16]}…")

        # 2. Refusals before any transaction.
        other_path, other = plan("plan-other-custody", custody="0x" + "ab" * 32)
        assert other["ready"]
        refused("plan for another custody account", other_path, "plan is for")
        budget_path, budget = plan("plan-over-budget", "--gas-budget", "999999999999")
        assert not budget["ready"]
        refused("plan exceeding the policy budget", budget_path, "not ready .*budget-exceeds-policy")
        refused("gas budget above the plan's", fresh_path, "exceeds the planned", "--gas-budget", "3000000000")
        passed("custody, readiness and budget mismatches refused before any transaction; nothing written, no gas")

        # 3. Publish under the plan.
        funds = balance()
        result = publish(fresh_path)
        assert result.returncode == 0, result.stderr
        first_release = (records / ENV / "current").resolve()
        first = json.loads((first_release / "deployment.json").read_text())
        assert first["chain_identifier"] == chain and first["package_version"] == "1"
        executed("publish", first_release, funds)
        verified_by_both_clients(first_release)
        passed(f"published {first['call_package_id']} under the plan; transaction {evidence['transactions']['publish']['digest']} "
               "succeeded; both clients verify the generated configs")

        # 4. Replay.
        refused("replayed publish plan", fresh_path, "plan is a publish, but this run would upgrade")
        passed("replayed publish plan refused once the deployment exists")

        # 5. Compatible upgrade adding a type.
        rehearsal_module = ("module sui_edge::release_rehearsal {\n    public struct RehearsalMarker has copy, drop {}\n\n"
                            "    public fun marker(): RehearsalMarker { RehearsalMarker {} }\n}\n")
        (package / "sources/release_rehearsal.move").write_text(rehearsal_module)
        git("add", "-A"); git("commit", "-qm", "head: compatible rehearsal module")
        head_copy = root / "head-contracts"
        shutil.copytree(package, head_copy, ignore=shutil.ignore_patterns("build", ".git", "Published.toml"))
        upgrade_path, upgrade = plan("plan-upgrade")
        assert upgrade["ready"] and upgrade["action"] == "upgrade", upgrade["findings"]
        assert upgrade["compatibility"]["new_types"] == [MARKER], upgrade["compatibility"]
        assert upgrade["deployment"]["upgrade_cap_owner"] == address and upgrade["clients"]["generated"]
        assert upgrade["revision"]["bytecode"]["digest"] != fresh["revision"]["bytecode"]["digest"]
        before, funds = state(), balance()
        dry = publish(upgrade_path, "--dry-run")
        assert dry.returncode == 0 and "dry run succeeded" in dry.stdout and "under plan" in dry.stdout, dry.stderr
        assert state() == before and balance() == funds
        result = publish(upgrade_path)
        assert result.returncode == 0, result.stderr
        second_release = (records / ENV / "current").resolve()
        second = json.loads((second_release / "deployment.json").read_text())
        executed("upgrade", second_release, funds)
        assert second["package_version"] == "2" and second["upgrade_cap_id"] == first["upgrade_cap_id"]
        assert second["type_origins"] == first["type_origins"] and second["shared_objects"] == first["shared_objects"]
        origins = dm.origins(dm.chain_facts(rpc_url, second["call_package_id"])["objects"][second["call_package_id"]])
        assert origins[MARKER] == second["call_package_id"], origins[MARKER]
        assert origins["device_registry::DeviceRegistry"] == first["call_package_id"]
        verified_by_both_clients(second_release)
        stale = subprocess.run([str(DAEMON), "--check-config", str(first_release / "config.toml")],
                               capture_output=True, text=True, timeout=120)
        assert stale.returncode != 0 and "UpgradeCap" in stale.stderr, stale.stderr
        passed(f"upgrade to {second['call_package_id']} (v2) under the plan; {MARKER} originates in v2, existing origins, "
               "registry and UpgradeCap kept; the v1 release is rejected as stale")

        # 6. Static compatibility and integrity refusals.
        (package / "sources/release_rehearsal.move").unlink()
        git("add", "-A"); git("commit", "-qm", "drop the rehearsal module")
        dropped_path, dropped = plan("plan-dropped-type")
        assert not dropped["ready"] and dropped["compatibility"]["removed_types"] == [MARKER]
        assert "release_rehearsal" in dropped["compatibility"]["removed_modules"]
        refused("plan whose sources dropped a published type", dropped_path, "not ready .*type-removed")
        (package / "sources/release_rehearsal.move").write_text(rehearsal_module)
        git("add", "-A"); git("commit", "-qm", "restore the rehearsal module")
        again_path, again = plan("plan-reupgrade")
        assert again["ready"] and "identical-bytecode" in {f["code"] for f in again["findings"]}, again["findings"]
        tampered = json.loads(again_path.read_text()); tampered["cost"]["gas_budget_mist"] *= 4
        tampered_path = root / "plan-tampered.json"; tampered_path.write_text(json.dumps(tampered))
        refused("edited plan", tampered_path, "digest does not match")
        passed("dropped-type plan and edited plan refused; identical bytecode to the current release is flagged")

        # 7. Old-package attacks against the rehearsed head, on the same chain.
        if old_package_checks:
            prior = root / "prior"; prior.mkdir()
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", PRIOR_IDENTITY_REF, "contracts"],
                                     check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(prior)], input=archive, check=True)
            attack = subprocess.run([sys.executable, str(ROOT / "scripts/identity_lifecycle_localnet.py"),
                                     "--prior", str(prior / "contracts"), "--new", str(head_copy)],
                                    env=dict(os.environ, SUI_CONFIG_DIR=str(wallet)), text=True, capture_output=True,
                                    timeout=1800)
            summary = re.search(r"(\d+)/(\d+) checks passed", attack.stdout)
            evidence["old_package_checks"] = summary and summary.group(0)
            assert attack.returncode == 0 and summary and summary[1] == summary[2], attack.stdout[-4000:] + attack.stderr[-2000:]
            passed(f"old-package identity attacks from {PRIOR_IDENTITY_REF} to the rehearsed head: {summary.group(0)}")

        evidence["records"] = {"v1": first["call_package_id"], "v2": second["call_package_id"],
                               "upgrade_cap": second["upgrade_cap_id"], "registry": second["shared_objects"]["registry"]["id"]}
        evidence["spent_mist"] = sum(t["net_gas_mist"] for t in evidence["transactions"].values())
        if evidence_path:
            Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence["records"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    parser.add_argument("--evidence", help="write rehearsal evidence JSON here")
    parser.add_argument("--skip-old-package-checks", action="store_true",
                        help="omit step 7 (identity_lifecycle_localnet.py)")
    args = parser.parse_args()
    run(args.rpc, args.faucet, args.evidence, not args.skip_old_package_checks)
