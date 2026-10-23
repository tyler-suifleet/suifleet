#!/usr/bin/env python3
"""SF-021 on a real local Sui network: claim enrollment, delegated deployer
roles and a PKCS#11 (SoftHSM2) device signer, with the real daemon.

  - the operator creates an enrollment policy and adds claim keys in bulk,
    funding them in one transaction;
  - devices with NO SUI of their own enroll with `suifleet --enroll`: the
    device key sends, the claim key sponsors; the operator receives each
    DeviceOwnerCap and the device its signer-bound cap;
  - a reused, revoked or expired claim is refused before anything changes;
  - a delegate holding a DeployerRole for the operator's group creates a
    deployment that the enrolled device's daemon applies and reports
    (receipt Applied); after revocation, and outside its group, the
    delegate is refused on chain;
  - a device whose key lives in a SoftHSM2 token (P-256, Sui secp256r1)
    enrolls and reports Applied through the PKCS#11 signer.

Needs a disposable localnet (as for the other localnet tests), the daemon
built with `--features pkcs11`, and softhsm2 + opensc (pkcs11-tool) for the
PKCS#11 case (make test-enrollment-localnet).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import deployment_manifest as dm
import test_component as component

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "suifleet-edge/target/debug/suifleet"
CLOCK = "@0x6"
SOFTHSM = Path("/usr/lib/softhsm/libsofthsm2.so")


def wait_for(condition, message, process=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"daemon exited ({process.returncode}): {message}")
        time.sleep(0.5)
    raise AssertionError(message)


def run(rpc_url, faucet_url):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    blobs = {}

    class Walrus(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = blobs.get(self.path.removeprefix("/v1/blobs/"))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    walrus = ThreadingHTTPServer(("127.0.0.1", 0), Walrus)
    threading.Thread(target=walrus.serve_forever, daemon=True).start()
    aggregator = f"http://127.0.0.1:{walrus.server_port}"
    daemons = []
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-enrollment-") as temporary:
            root = Path(temporary)
            try:
                (root / "wallet").mkdir()

                def client(*args, json_output=True, check=True):
                    command = ["sui", "client", "--client.config", str(root / "wallet/client.yaml"), *map(str, args)]
                    if json_output:
                        command.append("--json")
                    result = subprocess.run(command, text=True, capture_output=True, timeout=300)
                    if result.returncode and check:
                        raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}")
                    if not json_output or result.returncode:
                        return result
                    out = result.stdout
                    return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

                client("-y", "active-address", json_output=False)
                client("new-env", "--alias", "enroll-test", "--rpc", rpc_url, json_output=False)
                client("switch", "--env", "enroll-test", json_output=False)
                operator = client("active-address", json_output=False).stdout.strip().split()[-1]

                def new_address():
                    out = client("new-address", "ed25519", json_output=False).stdout
                    return next(w for w in out.replace("│", " ").split() if w.startswith("0x") and len(w) == 66)
                delegate = new_address()

                def balance(address):
                    gas = client("gas", address)
                    coins = gas.get("gasCoins", []) if isinstance(gas, dict) else gas
                    return sum(int(c.get("mistBalance", 0)) for c in coins) + (int(gas.get("addressMistBalance") or 0) if isinstance(gas, dict) else 0)
                for address in (operator, delegate):
                    client("faucet", "--url", faucet_url, "--address", address, json_output=False)
                    wait_for(lambda: balance(address) > 0, f"{address} not funded")

                def use(address):
                    client("switch", "--address", address, json_output=False)

                def transaction(*ptb):
                    result = client("ptb", *ptb, "--gas-budget", "200000000")
                    assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
                    return result

                def fails(*ptb):
                    result = client("ptb", *ptb, "--gas-budget", "200000000", check=False)
                    if hasattr(result, "returncode"):
                        return result.returncode != 0, result.stderr + result.stdout
                    return result["effects"]["status"]["status"] != "success", json.dumps(result["effects"]["status"])

                def created(result, suffix):
                    return [c["objectId"] for c in result["objectChanges"] if c["type"] == "created" and c["objectType"].endswith(suffix)]

                contracts = root / "contracts"
                shutil.copytree(ROOT / "contracts", contracts, ignore=shutil.ignore_patterns("build", "Published.toml"))
                published = client("test-publish", contracts, "--build-env", "testnet", "--pubfile-path", root / "Pub.local.toml",
                                   "--gas-budget", "900000000", "--silence-warnings")
                assert published["effects"]["status"]["status"] == "success"
                package = next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")
                registry = created(published, "::device_registry::DeviceRegistry")[0]
                cap = created(published, "::package::UpgradeCap")[0]
                chain_id = dm.chain_facts(rpc_url)["chain_identifier"]
                manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package, upgrade_cap=cap,
                                                      registry=registry, network="localnet", publisher=aggregator, aggregator=aggregator))
                (root / "deployment.json").write_text(json.dumps(manifest))

                # ── Policy and bulk claims ───────────────────────────────────────
                use(operator)
                policy = created(transaction("--move-call", f"{package}::enrollment::create_policy", f"@{registry}", "5", "0"),
                                 "::enrollment::EnrollmentPolicy")[0]
                claims = {}
                for name in ("c1", "c2", "c3", "c4"):
                    key = root / f"claim-{name}.key"
                    claims[name] = (key, subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip())
                addresses = "[" + ", ".join(f"@{a}" for _, a in claims.values()) + "]"
                funding = ["--split-coins", "gas", "[" + ", ".join(["20000000"] * len(claims)) + "]", "--assign", "fees"]
                for index, (_, address) in enumerate(claims.values()):
                    funding += ["--transfer-objects", f"[fees.{index}]", f"@{address}"]
                transaction("--move-call", f"{package}::enrollment::add_claims", f"@{policy}", f"vector{addresses}", *funding)
                print("PASS: policy created; 4 claim keys added and funded in one transaction")

                def enroll(name, claim, *, device_key=None, signer=None):
                    config = root / f"enroll-{name}.toml"
                    lines = [f'manifest_path = "{root / "deployment.json"}"', f'policy_id = "{policy}"',
                             f'claim_key_path = "{claims[claim][0]}"', f'name = "{name}"', 'arch = "aarch64"']
                    if device_key:
                        lines.append(f'keypair_path = "{device_key}"')
                    config.write_text("\n".join(lines) + "\n" + (signer or ""))
                    return subprocess.run([str(DAEMON), "--enroll", str(config)], text=True, capture_output=True, timeout=180)

                devices = {}
                for name, claim in (("pi-1", "c1"), ("pi-2", "c2")):
                    key = root / f"{name}.key"
                    address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
                    assert balance(address) == 0
                    result = enroll(name, claim, device_key=key)
                    assert result.returncode == 0, result.stderr
                    devices[name] = dict(json.loads(result.stdout), key=key, address=address)
                    wait_for(lambda: client("object", devices[name]["owner_cap_id"], check=False).__class__ is dict,
                             "owner cap never became readable")
                    assert balance(address) == 0, "the device paid gas"
                    digest = devices[name]["digest"]
                    found = {}

                    def indexed():
                        try:
                            found["block"] = dm.rpc(rpc_url, "sui_getTransactionBlock", [digest, {"showInput": True, "showEffects": True}])
                            return True
                        except ValueError:
                            return False  # Reads can trail an executed transaction.
                    wait_for(indexed, f"enrollment {digest} never became readable")
                    block = found["block"]
                    assert block["effects"]["status"]["status"] == "success", block["effects"]["status"]
                    assert dm.object_id(block["transaction"]["data"]["sender"]) == dm.object_id(address)
                    gas_owner = block["transaction"]["data"]["gasData"]["owner"]
                    assert dm.object_id(gas_owner) == dm.object_id(claims[claim][1]), ("gas not paid by the claim key", gas_owner)
                owner_caps = [devices[n]["owner_cap_id"] for n in devices]
                for owner_cap in owner_caps:
                    assert dm.object_id(client("object", owner_cap)["owner"]["AddressOwner"]) == dm.object_id(operator)
                for name, device in devices.items():
                    device_cap = client("object", device["device_cap_id"])
                    assert dm.object_id(device_cap["owner"]["AddressOwner"]) == dm.object_id(device["address"])
                print("PASS: two devices with zero SUI enrolled via `suifleet --enroll` (claim-sponsored); owner caps went to the "
                      "operator, signer-bound caps to the devices")

                # ── Unauthorized enrollments ─────────────────────────────────────
                spare = root / "spare.key"
                subprocess.check_output([str(DAEMON), "--provision", str(spare)])
                reused = enroll("pi-reuse", "c1", device_key=spare)
                assert reused.returncode != 0 and "enrollment" in reused.stderr, reused.stderr
                transaction("--move-call", f"{package}::enrollment::revoke_claim", f"@{policy}", f"@{claims['c4'][1]}")
                revoked = enroll("pi-revoked", "c4", device_key=spare)
                assert revoked.returncode != 0, revoked.stderr
                transaction("--move-call", f"{package}::enrollment::set_limits", f"@{policy}", "5", "1")
                expired = enroll("pi-expired", "c3", device_key=spare)
                assert expired.returncode != 0, expired.stderr
                transaction("--move-call", f"{package}::enrollment::set_limits", f"@{policy}", "5", "0")
                print("PASS: reused, revoked and expired claims refused (simulation aborts; nothing executed)")

                # ── Group, delegate role and the real daemon ─────────────────────
                group = created(transaction("--move-call", f"{package}::device_group::create_group", '"fleet"', '"sf021"', CLOCK),
                                "::device_group::DeviceGroup")[0]
                other_group = created(transaction("--move-call", f"{package}::device_group::create_group", '"other"', '"sf021"', CLOCK),
                                      "::device_group::DeviceGroup")[0]
                for name in devices:
                    transaction("--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{devices[name]['owner_cap_id']}",
                                "--assign", "loan",
                                "--move-call", f"{package}::device_group::add_device", f"@{group}", "loan.0",
                                "--move-call", f"{package}::device_registry::return_device", f"@{registry}", "loan.0", "loan.1")
                role_tx = transaction("--move-call", f"{package}::roles::new_deployer_role", "--assign", "builder",
                                      "--move-call", f"{package}::roles::allow_group", "builder", f"@{group}",
                                      "--move-call", f"{package}::roles::grant", "builder", f"@{registry}", f"@{delegate}")
                role = created(role_tx, "::roles::DeployerRole")[0]
                # Report gas for the device that will run the daemon (enrollment needed none).
                transaction("--split-coins", "gas", "[30000000]", "--assign", "fee", "--transfer-objects", "[fee]", f"@{devices['pi-1']['address']}")
                run_id = "sf021-" + uuid.uuid4().hex[:8]

                def deployment_args(version, groups):
                    artifact = component.build(version, run_id)
                    tag = version.replace(".", "-")
                    artifact_blob, recipe_blob = f"artifact-{tag}", f"recipe-{tag}"
                    recipe = component.recipe(artifact, artifact_blob)
                    blobs[artifact_blob], blobs[recipe_blob] = artifact, recipe
                    ids = "[" + ", ".join(f"@{g}" for g in groups) + "]"
                    return [f'"{component.DEFAULT_COMPONENT}"', f'"{version}"', f'"{recipe_blob}"',
                            f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"', f"vector{ids}", CLOCK]

                def start_daemon(name, device_section):
                    state = root / f"state-{name}"
                    template = root / f"template-{name}.toml"
                    template.write_text(device_section + f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {state}/c")}\n'
                                        f'timeout_seconds = 30\n[runtime]\nstate_dir = {json.dumps(str(state))}\npoll_seconds = 1\nevent_scan_seconds = 0\n')
                    dm.generate(manifest, root / f"release-{name}", template)
                    log = (root / f"daemon-{name}.log").open("w")
                    process = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(root / f"release-{name}/config.toml")},
                                               stdout=log, stderr=log)
                    daemons.append(process)
                    wait_for(lambda: (state / "state.json").exists() and json.loads((state / "state.json").read_text()).get("initialized"),
                             f"{name} daemon did not enroll", process)
                    return process, state

                def receipts(deployment):
                    for field in client("dynamic-field", deployment)["dynamicFields"]:
                        value = client("object", field["fieldId"])
                        if "ReceiptsKey" in str(value.get("objType", "")):
                            content = value["content"]
                            value = content.get("fields", content)["value"]
                            value = value.get("fields", value)
                            statuses = value["statuses"].get("fields", value["statuses"])["contents"]
                            return {dm.object_id((e.get("fields", e))["key"]): int((e.get("fields", e))["value"]) for e in statuses}
                    return {}

                pi1 = devices["pi-1"]
                daemon, _ = start_daemon("pi-1", f'[device]\nkeypair_path = "{pi1["key"]}"\ndevice_object_id = "{pi1["device_object_id"]}"\n'
                                                 f'device_cap_id = "{pi1["device_cap_id"]}"\n')
                use(delegate)
                delegated = created(transaction("--move-call", f"{package}::roles::create_deployment_as", f"@{role}", f"@{registry}",
                                                *deployment_args("1.0.0", [group])), "::deployment_manager::DeploymentRecord")[0]
                record = client("object", delegated)["content"]
                creator = record.get("fields", record)["creator"]
                assert dm.object_id(creator) == dm.object_id(operator), "delegated record not attributed to the operator"
                wait_for(lambda: receipts(delegated).get(dm.object_id(pi1["device_object_id"])) == 1,
                         "enrolled device did not apply the delegate's deployment", daemon)
                print("PASS: delegate's role deployment (creator = operator) applied by the enrolled device's daemon; receipt Applied")
                failed, why = fails("--move-call", f"{package}::roles::create_deployment_as", f"@{role}", f"@{registry}",
                                    *deployment_args("1.1.0", [other_group]))
                assert failed, why
                use(operator)
                transaction("--move-call", f"{package}::roles::revoke", f"@{registry}", f"@{role}")
                use(delegate)
                failed, why = fails("--move-call", f"{package}::roles::create_deployment_as", f"@{role}", f"@{registry}",
                                    *deployment_args("1.2.0", [group]))
                assert failed, why
                use(operator)
                print("PASS: delegate refused outside its group and after revocation")

                # ── PKCS#11 (SoftHSM2) device signer ─────────────────────────────
                if not SOFTHSM.exists() or not shutil.which("pkcs11-tool"):
                    print("SKIP: softhsm2/opensc not installed; PKCS#11 signer case not run")
                    return
                tokens = root / "softhsm"
                tokens.mkdir()
                conf = root / "softhsm2.conf"
                conf.write_text(f"directories.tokendir = {tokens}\nobjectstore.backend = file\nlog.level = ERROR\n")
                os.environ["SOFTHSM2_CONF"] = str(conf)
                subprocess.run(["softhsm2-util", "--init-token", "--free", "--label", "suifleet", "--so-pin", "5678", "--pin", "1234"],
                               check=True, capture_output=True)
                subprocess.run(["pkcs11-tool", "--module", str(SOFTHSM), "--token-label", "suifleet", "--login", "--pin", "1234",
                                "--keypairgen", "--key-type", "EC:prime256v1", "--label", "device"], check=True, capture_output=True)
                pin = root / "pin"; pin.write_text("1234\n"); pin.chmod(0o600)
                signer = (f'[signer]\nkind = "pkcs11"\nmodule = "{SOFTHSM}"\ntoken_label = "suifleet"\nkey_label = "device"\n'
                          f'pin_file = "{pin}"\n')
                result = enroll("pi-hsm", "c3", signer=signer)
                assert result.returncode == 0, result.stderr
                hsm = json.loads(result.stdout)
                wait_for(lambda: client("object", hsm["owner_cap_id"], check=False).__class__ is dict, "owner cap never became readable")
                transaction("--split-coins", "gas", "[30000000]", "--assign", "fee", "--transfer-objects", "[fee]", f"@{hsm['device_address']}",
                            "--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{hsm['owner_cap_id']}", "--assign", "loan",
                            "--move-call", f"{package}::device_group::add_device", f"@{group}", "loan.0",
                            "--move-call", f"{package}::device_registry::return_device", f"@{registry}", "loan.0", "loan.1")
                hsm_daemon, _ = start_daemon("pi-hsm", f'[device]\ndevice_object_id = "{hsm["device_object_id"]}"\n'
                                                       f'device_cap_id = "{hsm["device_cap_id"]}"\n'
                                                       f'[device.signer]\nkind = "pkcs11"\nmodule = "{SOFTHSM}"\ntoken_label = "suifleet"\n'
                                                       f'key_label = "device"\npin_file = "{pin}"\n')
                via_operator = created(transaction("--move-call", f"{package}::deployment_manager::create_deployment_verified",
                                                   *deployment_args("2.0.0", [group])[:5], f"vector[@{group}]", "vector[]", CLOCK),
                                       "::deployment_manager::DeploymentRecord")[0]
                wait_for(lambda: receipts(via_operator).get(dm.object_id(hsm["device_object_id"])) == 1,
                         "SoftHSM-signed device did not report Applied", hsm_daemon)
                print(f"PASS: device key in a SoftHSM2 token (P-256, address {hsm['device_address'][:12]}…) enrolled and reported "
                      "Applied through the PKCS#11 signer")
            finally:
                for process in daemons:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                if any(p.returncode not in (0, None, -2) for p in daemons):
                    for log in Path(temporary).glob("daemon-*.log"):
                        print(log.name, log.read_text()[-2000:])
    finally:
        walrus.shutdown()
        walrus.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    args = parser.parse_args()
    run(args.rpc, args.faucet)
