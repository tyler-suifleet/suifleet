#!/usr/bin/env python3
"""The SF-065 acceptance runner end to end on a disposable localnet.

Publishes the actual contracts, resolves a verified manifest, writes a localnet
profile with a scripted test account, the local stand-in device (the real
daemon and SF-030 updater on this host) and a loopback Walrus stand-in, then
runs scripts/edge_acceptance.py exactly as an operator would:

  1. a fresh enrollment: every case this mode can run must pass, and the
     verdict must stay "incomplete" because hardware and extension-wallet
     cases are not established by a stand-in;
  2. a second run reusing that enrollment, key and journal (the device is
     already funded and has a cursor, so report-retry and baseline-skip are
     skipped, not passed);
  3. the pending SF-022 encrypted profile: blocked, no transaction sent.

Run inside the dev image with a throwaway node and faucet:

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_edge_acceptance_localnet.py

It refuses non-loopback endpoints and never reads the normal wallet.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import deployment_manifest as dm
import edge_acceptance as acceptance

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/edge_acceptance.py"
MODE_GAPS = {"image-identity": "skipped", "extension-wallet": "blocked", "encrypted-delivery": "blocked"}


def run(rpc_url, faucet_url, keep=None):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    with tempfile.TemporaryDirectory(prefix="suifleet-edge-acceptance-localnet-") as temporary:
        root = Path(temporary)
        wallet = root / "wallet"
        wallet.mkdir()
        (wallet / "sui.keystore").write_text("[]")
        (wallet / "client.yaml").write_text(f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: acceptance\n"
                                            f"  rpc: {rpc_url}\nactive_env: acceptance\nactive_address: null\n")
        cli = acceptance.SuiCli(wallet / "client.yaml")
        # new-address may print a recovery phrase on stdout: parse it, never echo it.
        operator = dm.object_id(cli("new-address", "ed25519")["address"])
        cli("switch", "--address", operator, json_output=False)
        cli("faucet", "--url", faucet_url, json_output=False)
        deadline = time.monotonic() + 60
        while cli.balance() == 0:
            if time.monotonic() > deadline:
                raise AssertionError("local faucet supplied no gas")
            time.sleep(1)

        contracts = root / "contracts"
        shutil.copytree(ROOT / "contracts", contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
        published = cli("test-publish", contracts, "--build-env", "testnet", "--pubfile-path", root / "Pub.local.toml",
                        "--gas-budget", "500000000", "--silence-warnings")
        assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]
        package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
        registry = acceptance.created(published, "::device_registry::DeviceRegistry")[0]
        upgrade_cap = acceptance.created(published, "::package::UpgradeCap")[0]
        chain = dm.chain_facts(rpc_url)["chain_identifier"]
        # Walrus endpoints are replaced by the run's stand-in in the device's manifest.
        placeholder = "http://127.0.0.1:9"
        manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=rpc_url, package=package, upgrade_cap=upgrade_cap,
                                              registry=registry, network="localnet", publisher=placeholder, aggregator=placeholder))
        (root / "deployment.json").write_text(json.dumps(manifest, indent=2))
        print(f"published {package} on localnet {chain}; operator {operator}")

        device_root = root / "device"
        profile = {"schema": 1, "profile": "localnet", "manifest": "deployment.json",
                   "wallet": {"mode": "scripted", "address": operator, "client_config": "wallet/client.yaml"},
                   "walrus": {"mode": "stand-in", "bind": "127.0.0.1"},
                   "device": {"mode": "local", "daemon": str(acceptance.DAEMON), "root": str(device_root)},
                   "artifact_profile": "public", "faucet_url": faucet_url}

        def invoke(name, document, *extra):
            path = root / f"{name}.json"
            path.write_text(json.dumps(document))
            evidence = root / f"{name}-evidence.json"
            result = subprocess.run([sys.executable, str(SCRIPT), "run", "--profile", str(path), "--evidence", str(evidence), *extra],
                                    text=True, capture_output=True, timeout=1800)
            print(result.stdout[-6000:])
            if result.stderr.strip():
                print(result.stderr[-3000:], file=sys.stderr)
            data = json.loads(evidence.read_text()) if evidence.exists() else None
            if keep and data:
                Path(keep).mkdir(parents=True, exist_ok=True)
                (Path(keep) / evidence.name).write_text(json.dumps(data, indent=2))
            return result.returncode, data

        (root / "plan.json").write_text(json.dumps(profile))
        plan = subprocess.run([sys.executable, str(SCRIPT), "plan", "--profile", str(root / "plan.json")],
                              text=True, capture_output=True)
        assert plan.returncode == 0 and "skipped  image-identity" in plan.stdout, plan.stdout + plan.stderr

        def check(evidence, expect_passed):
            cases = evidence["cases"]
            for id, status in MODE_GAPS.items():
                assert cases[id]["status"] == status, (id, cases[id])
            for id in expect_passed:
                assert cases[id]["status"] == "passed", (id, cases[id])
            assert evidence["verdict"] == "incomplete", evidence["verdict"]
            assert all(t["status"] == "success" for t in evidence["transactions"]), evidence["transactions"]
            text = json.dumps(evidence)
            assert (device_root / "device.key").read_text().strip() not in text, "device key leaked into evidence"

        runnable = [c.id for c in acceptance.CASES if not c.future and c.id not in MODE_GAPS]
        # 1. Fresh enrollment, unfunded device: every runnable case, report retry included.
        code, first = invoke("fresh", profile)
        assert first is not None, "no evidence written"
        failed = {id: e for id, e in first["cases"].items() if e["status"] == "failed"}
        assert not failed, failed
        assert code == 3, f"incomplete runs must exit 3 without --allow-incomplete, got {code}"
        check(first, runnable)
        assert first["device"]["kind"] == "local-stand-in"
        history = [json.loads(line) for line in (device_root / "state/test-components/suifleet-test-app/history.jsonl").read_text().splitlines()]
        assert [r["version"] for r in history] == ["1.0.0", "2.0.0", "3.0.0", "3.1.0", "4.0.0"], history
        assert all(r["runId"] == first["run_id"] for r in history)
        print("PASS: fresh localnet run: every case a stand-in and scripted wallet can establish passed; "
              "hardware and extension cases stayed skipped/blocked; verdict incomplete (exit 3)")

        # 2. Reuse the enrollment, key and journal: no new identity, no replay of earlier work.
        enrollment = first["enrollment"]
        reuse = dict(profile, enrollment={"device_object_id": enrollment["device"], "device_cap_id": enrollment["cap"],
                                          "owner_cap_id": enrollment["owner_cap"]})
        code, second = invoke("reuse", reuse, "--allow-incomplete")
        assert code == 0, f"--allow-incomplete with every runnable case passed should exit 0, got {code}"
        check(second, [id for id in runnable if id not in ("report-retry", "baseline-skip")])
        for id in ("report-retry", "baseline-skip"):  # Funded device; existing cursor.
            assert second["cases"][id]["status"] == "skipped", second["cases"][id]
        assert second["enrollment"]["address"] == first["enrollment"]["address"]
        assert second["enrollment"]["source"] == "existing enrollment named by the profile"
        gas = second["device_gas"]  # Leftover report gas from the first run counts against this run's allowance.
        assert gas["status"] == "charged" and gas["held_mist"] > 0 and second["spent_mist"] >= gas["held_mist"], gas
        history = [json.loads(line) for line in (device_root / "state/test-components/suifleet-test-app/history.jsonl").read_text().splitlines()]
        assert [r["runId"] for r in history].count(first["run_id"]) == 5, "earlier run's work was replayed"
        print("PASS: second run reused the enrollment, key and journal; report-retry/baseline-skip skipped, no replay")

        # 3. The encrypted profile is pending SF-022: blocked, nothing sent.
        code, third = invoke("encrypted", dict(profile, artifact_profile="sf022-encrypted"))
        assert code == 3 and third["verdict"] == "incomplete", (code, third and third["verdict"])
        assert all(e["status"] in ("blocked", "skipped") for e in third["cases"].values()), third["cases"]
        assert third["transactions"] == [], third["transactions"]
        print("PASS: SF-022 encrypted profile is blocked without executing or spending")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    parser.add_argument("--keep-evidence", help="copy each run's evidence JSON here")
    args = parser.parse_args()
    run(args.rpc, args.faucet, args.keep_evidence)
