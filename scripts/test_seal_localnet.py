#!/usr/bin/env python3
"""Confidential delivery (SF-022) end to end on a disposable localnet.

Publishes the actual contracts and a test stand-in for Seal's key server
objects, starts three test key servers (suifleet-edge/examples/
seal_key_server.rs: Seal's protocol, approvals simulated on this chain),
encrypts with the dapp's own module and the real @mysten/seal SDK, serves the
ciphertext from a local Walrus stand-in, and runs the real daemon with
`[seal] mode = "required"`. The device key lives in the separate signing
service, which certifies Seal sessions with it; the daemon never holds it.

Judged from on-chain receipts and the daemon's journal:

  - device outside the policy -> Failed, nothing decrypted
  - device granted -> encrypted recipe+artifact decrypted, applied, Applied
  - group grant (device grant revoked) -> Applied; device removed -> Failed
  - unencrypted deployment under mode=required -> Failed (no plaintext path)
  - tampered ciphertext -> Failed (authenticated encryption)
  - ciphertext for another package (wrong network/namespace) -> Failed
  - key epoch rotated -> old release Failed; a fresh release Applied
  - one of three key servers down (threshold 2) -> Applied; two down -> Failed
  - a key server refusing the session lifetime -> its keys withheld
  - accounts: an ungranted account is refused by key servers, a granted one
    decrypts with its own key through the official SDK
  - no confidential plaintext in the offline component cache

With --upgrade-from REF, the contracts at REF (without seal_policy) are
published first and its registry is reused after a compatible upgrade to this
tree: Seal's namespace is then the original package ID while approvals call
the upgraded package, as on public networks.

Run inside the dev image with a throwaway node and faucet, no other network:

    SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
    python3 scripts/test_seal_localnet.py [--upgrade-from main]

It refuses non-loopback endpoints and never reads the normal wallet.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
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
KEY_SERVER = ROOT / "suifleet-edge/target/debug/examples/seal_key_server"
CLOCK = "@0x6"


def wait_for(condition, message, process=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise AssertionError(f"process exited ({process.returncode}): {message}")
        time.sleep(0.5)
    raise AssertionError(message)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seal_tool(request):
    result = subprocess.run(["node", str(ROOT / "scripts/seal_client_tool.cjs")], input=json.dumps(request),
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"seal_client_tool failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def run(rpc_url, faucet_url, upgrade_from=None):
    for url in (rpc_url, faucet_url):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this test requires an isolated loopback node and faucet")
    blobs, downloads = {}, []

    class Walrus(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            downloads.append(self.path)
            body = blobs.get(self.path.removeprefix("/v1/blobs/"))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    walrus = ThreadingHTTPServer(("127.0.0.1", 0), Walrus)
    threading.Thread(target=walrus.serve_forever, daemon=True).start()
    aggregator = f"http://127.0.0.1:{walrus.server_port}"
    processes = {}
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-seal-localnet-") as temporary:
            root = Path(temporary)
            config_dir = root / "wallet"
            config_dir.mkdir()

            def client(*args, json_output=True):
                command = ["sui", "client", "--client.config", str(config_dir / "client.yaml"), *map(str, args)]
                if json_output:
                    command.append("--json")
                result = subprocess.run(command, text=True, capture_output=True, timeout=300)
                # Wallet initialization may print a recovery phrase: never forward it.
                if result.returncode:
                    raise RuntimeError(f"sui client {args[0]} failed:\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}")
                if not json_output:
                    return result.stdout
                out = result.stdout
                return json.loads(out[min(i for i in (out.find("{"), out.find("[")) if i >= 0):])

            (config_dir / "sui.keystore").write_text("[]")
            (config_dir / "client.yaml").write_text(f"keystore:\n  File: {config_dir / 'sui.keystore'}\nenvs:\n- alias: seal-test\n  rpc: {rpc_url}\nactive_env: seal-test\nactive_address: null\n")
            operator = client("new-address", "ed25519")["address"]
            client("switch", "--address", operator, json_output=False)
            client("faucet", "--url", faucet_url, json_output=False)

            def funded():
                gas = client("gas")
                if isinstance(gas, dict):
                    return bool(gas.get("gasCoins")) or int(gas.get("addressMistBalance") or 0) > 0
                return bool(gas)
            wait_for(funded, "local faucet did not supply gas")
            chain_id = dm.chain_facts(rpc_url)["chain_identifier"]

            def transaction(*ptb, expect_success=True):
                result = client("ptb", *ptb, "--gas-budget", "200000000")
                if expect_success:
                    assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
                return result

            def created(result, suffix):
                return [c["objectId"] for c in result["objectChanges"]
                        if c["type"] == "created" and c["objectType"].endswith(suffix)]

            def publish(source, name):
                copy = root / name
                shutil.copytree(source, copy, ignore=shutil.ignore_patterns("build", "Published.toml"))
                published = client("test-publish", copy, "--build-env", "testnet", "--pubfile-path",
                                   root / f"Pub.{name}.toml", "--gas-budget", "500000000", "--silence-warnings")
                assert published["effects"]["status"]["status"] == "success", published["effects"]["status"]
                return published, next(c["packageId"] for c in published["objectChanges"] if c["type"] == "published")

            if upgrade_from:
                base = root / "base-contracts"
                shutil.copytree(ROOT / "contracts", base, ignore=shutil.ignore_patterns("build", "Published.toml", "sources", "tests"))
                (base / "sources").mkdir()
                names = subprocess.check_output(["git", "ls-tree", "--name-only", f"{upgrade_from}:contracts/sources"], cwd=ROOT, text=True).split()
                assert "seal_policy.move" not in names, "the base must predate seal_policy"
                for name in names:
                    (base / "sources" / name).write_bytes(subprocess.check_output(["git", "show", f"{upgrade_from}:contracts/sources/{name}"], cwd=ROOT))
                published, original = publish(base, "contracts")
                registry = created(published, "::device_registry::DeviceRegistry")[0]
                upgrade_cap = created(published, "::package::UpgradeCap")[0]
                head = root / "contracts"
                shutil.rmtree(head / "sources")
                shutil.copytree(ROOT / "contracts/sources", head / "sources")
                upgraded = client("test-upgrade", head, "--build-env", "testnet", "--pubfile-path", root / "Pub.contracts.toml",
                                  "--upgrade-capability", upgrade_cap, "--gas-budget", "500000000", "--silence-warnings")
                assert upgraded["effects"]["status"]["status"] == "success", upgraded["effects"]["status"]
                package = next(c["packageId"] for c in upgraded["objectChanges"] if c["type"] == "published")
                assert dm.object_id(package) != dm.object_id(original)
            else:
                published, package = publish(ROOT / "contracts", "contracts")
                registry = created(published, "::device_registry::DeviceRegistry")[0]
                upgrade_cap = created(published, "::package::UpgradeCap")[0]
            _, key_server_package = publish(ROOT / "tests/seal-key-server", "key-server")
            manifest = dm.resolve(SimpleNamespace(chain_identifier=chain_id, rpc=rpc_url, package=package,
                                                  upgrade_cap=upgrade_cap, registry=registry, network="localnet",
                                                  publisher=aggregator, aggregator=aggregator))
            assert all(name in manifest["type_origins"] for name in dm.SEAL_POLICY_TYPES), "seal_policy origins missing"
            namespace = manifest["type_origins"]["device_registry::DeviceRegistry"]
            if upgrade_from:
                # Existing types keep the base package's origin; seal_policy is new.
                assert dm.object_id(namespace) == dm.object_id(original)
                assert all(dm.object_id(manifest["type_origins"][n]) == dm.object_id(package) for n in dm.SEAL_POLICY_TYPES)
                print(f"PASS: compatible upgrade from {upgrade_from}: registry reused, seal_policy introduced by the upgrade, "
                      "Seal namespace is the original package")

            # The device's identity is generated locally and held by the signing service.
            key = root / "device.key"
            device_address = subprocess.check_output([str(DAEMON), "--provision", str(key)], text=True).strip()
            client("faucet", "--url", faucet_url, "--address", device_address, json_output=False)
            registered = transaction(
                "--move-call", f"{package}::device_registry::register_device", f"@{registry}", '"pi"',
                f"@{device_address}", '"x86_64"', CLOCK, "--assign", "device",
                "--move-call", f"{package}::device_cap::issue_cap", "device",
                "--move-call", f"{package}::device_registry::activate_device", f"@{registry}", "device",
                "--assign", "owner", "--transfer-objects", "[owner]", f"@{operator}")
            device = created(registered, "::device_registry::Device")[0]
            cap = created(registered, "::device_cap::DeviceCap")[0]
            owner_cap = created(registered, "::device_registry::DeviceOwnerCap")[0]
            group = created(transaction("--move-call", f"{package}::device_group::create_group", '"fleet"', '"seal"', CLOCK),
                            "::device_group::DeviceGroup")[0]
            transaction("--move-call", f"{package}::device_registry::borrow_device", f"@{registry}", f"@{owner_cap}",
                        "--assign", "loaned", "--move-call", f"{package}::device_group::add_device", f"@{group}", "loaned.0",
                        "--move-call", f"{package}::device_registry::return_device", f"@{registry}", "loaned.0", "loaned.1")

            # Three key servers, registered on chain with their URLs and public keys.
            servers = []
            for index in range(3):
                seed = hashlib.sha256(f"seal-test-{uuid.uuid4()}".encode()).hexdigest()
                public_key = subprocess.check_output([str(KEY_SERVER), "public-key", seed], text=True).strip()
                port = free_port()
                result = transaction("--move-call", f"{key_server_package}::key_server::register", f'"test-{index}"',
                                     f'"http://127.0.0.1:{port}"', "vector[" + ",".join(f"{b}u8" for b in bytes.fromhex(public_key)) + "]")
                servers.append(dict(seed=seed, port=port, object_id=created(result, "::key_server::KeyServer")[0], name=f"test-{index}"))
            key_server_log = (root / "key-servers.log").open("w")

            def start_server(server, *extra):
                process = subprocess.Popen([str(KEY_SERVER), "serve", rpc_url, namespace, server["object_id"], server["seed"],
                                            str(server["port"]), *extra], stdout=subprocess.PIPE, stderr=key_server_log, text=True)
                assert process.stdout.readline().startswith("listening"), "key server did not start"
                processes[server["name"]] = process

            def stop_server(server):
                process = processes.pop(server["name"])
                process.kill()
                process.wait()

            for server in servers:
                start_server(server)
            seal_servers = [{"objectId": s["object_id"], "weight": 1} for s in servers]

            # The access policy, administered by the operator's cap.
            policy_tx = transaction("--move-call", f"{package}::seal_policy::create_policy", '"releases"', "--assign", "cap",
                                    "--transfer-objects", "[cap]", f"@{operator}")
            policy = created(policy_tx, "::seal_policy::AccessPolicy")[0]
            admin_cap = created(policy_tx, "::seal_policy::AccessAdminCap")[0]
            epoch = 0

            def policy_call(function, *args, expect_success=True):
                return transaction("--move-call", f"{package}::seal_policy::{function}", f"@{policy}", f"@{admin_cap}", *args,
                                   expect_success=expect_success)

            run_id = "seal-" + uuid.uuid4().hex[:10]
            counter = [0]

            def release(version, *, confidential=True, tamper=False, namespace_override=None, key_epoch=None, targets=None,
                        before_deploy=None):
                """Upload one release (artifact + recipe) and create its deployment;
                `before_deploy` runs between the upload and the deployment."""
                counter[0] += 1
                artifact = component.build(version, run_id)
                tag = f"{version.replace('.', '-')}-{counter[0]}"
                artifact_blob, recipe_blob = f"artifact-{tag}", f"recipe-{tag}"
                recipe = component.recipe(artifact, artifact_blob)
                if confidential:
                    request = {"op": "encrypt", "manifest": manifest, "servers": seal_servers, "threshold": 2,
                               "policyId": policy, "keyEpoch": str(epoch if key_epoch is None else key_epoch),
                               "items": [{"kind": "artifact", "hex": artifact.hex()}, {"kind": "recipe", "hex": recipe.hex()}]}
                    if namespace_override:
                        request["namespace"] = namespace_override
                    sealed = seal_tool(request)
                    artifact_blob_bytes, recipe_blob_bytes = (bytes.fromhex(e) for e in sealed["envelopes"])
                    assert artifact not in artifact_blob_bytes and artifact[:64] not in artifact_blob_bytes
                    if tamper:
                        # Inside the AES-GCM ciphertext/tag: before the trailing
                        # Some(aad) (tag, length, 90 bytes of AAD).
                        tampered = bytearray(artifact_blob_bytes)
                        tampered[-(92 + 8)] ^= 1
                        artifact_blob_bytes = bytes(tampered)
                else:
                    artifact_blob_bytes, recipe_blob_bytes = artifact, recipe
                blobs[artifact_blob], blobs[recipe_blob] = artifact_blob_bytes, recipe_blob_bytes
                if before_deploy:
                    before_deploy()
                devices, groups = (targets or ([device], []))
                ids = lambda values: "[" + ", ".join(f"@{v}" for v in values) + "]"
                result = transaction("--move-call", f"{package}::deployment_manager::create_deployment_verified",
                                     f'"{component.DEFAULT_COMPONENT}"', f'"{version}"', f'"{recipe_blob}"',
                                     f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"',
                                     f"vector{ids(groups)}", f"vector{ids(devices)}", CLOCK)
                return created(result, "::deployment_manager::DeploymentRecord")[0], artifact, recipe_blob_bytes

            # Signing service: holds the device key, certifies Seal sessions.
            socket_path = root / "sign.sock"
            (root / "deployment.json").write_text(json.dumps(manifest))
            (root / "signer.toml").write_text(
                f'manifest_path = "deployment.json"\ndevice_cap_id = "{cap}"\nkeypair_path = {json.dumps(str(key))}\n'
                f'socket = {json.dumps(str(socket_path))}\ncaller_uid = {os.getuid()}\n[seal]\nsession_ttl_minutes = 10\n')
            log = (root / "daemon.log").open("w")
            processes["signer"] = subprocess.Popen([str(DAEMON), "--signer-service", str(root / "signer.toml")], stdout=log, stderr=log)
            wait_for(socket_path.exists, "signing service did not start", processes["signer"])

            components_dir = root / "state" / "test-components"
            template = root / "device-template.toml"
            servers_toml = ", ".join(f'{{ object_id = "{s["object_id"]}" }}' for s in servers)
            template.write_text(
                f'[device]\ndevice_object_id = "{device}"\ndevice_cap_id = "{cap}"\n'
                f'[device.signer]\nkind = "service"\nsocket = {json.dumps(str(socket_path))}\n'
                f'[apply]\ncommand = {json.dumps(f"/bin/sh {component.UPDATER} --root {components_dir}")}\n'
                f'timeout_seconds = 30\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n'
                f'[seal]\nmode = "required"\nkey_server_package = "{key_server_package}"\nkey_servers = [{servers_toml}]\n'
                f'min_threshold = 2\nkey_cache_seconds = 0\nrequest_timeout_seconds = 5\n')
            bundle = root / "release"
            dm.generate(manifest, bundle, template)
            config = bundle / "config.toml"
            # The offline candidate cache is enabled so its contents can be checked.
            config.write_text(config.read_text() + '[components]\noffline_candidates = "cached"\n')
            journal_path = root / "state" / "state.json"

            def journal():
                return json.loads(journal_path.read_text()) if journal_path.exists() else {}

            def outcome(id):
                for key_, value in journal().get("deployments", {}).items():
                    if dm.object_id(key_) == dm.object_id(id):
                        return value
                return {}

            def receipts(deployment):
                for entry in dm.rpc(rpc_url, "suix_getDynamicFields", [deployment, None, 50])["data"]:
                    if entry["name"]["type"].endswith("::deployment_manager::ReceiptsKey"):
                        field = dm.rpc(rpc_url, "sui_getObject", [entry["objectId"], {"showContent": True}])
                        contents = field["data"]["content"]["fields"]["value"]["fields"]["statuses"]["fields"]["contents"]
                        return {dm.object_id(e["fields"]["key"]): int(e["fields"]["value"]) for e in contents}
                return {}

            def settles(deployment, status, error=None):
                process = processes["daemon"]
                wait_for(lambda: outcome(deployment).get("status") == status and outcome(deployment).get("reported"),
                         f"{deployment} did not settle as {status}: {outcome(deployment)}", process, timeout=120)
                wait_for(lambda: receipts(deployment) == {dm.object_id(device): status},
                         f"on-chain receipt for {deployment} never became {status}", timeout=60)
                if error:
                    assert error in (outcome(deployment).get("error") or ""), outcome(deployment)

            def installed(artifact):
                history = components_dir / component.DEFAULT_COMPONENT / "history.jsonl"
                return history.exists() and component.header(artifact)["version"] in history.read_text()

            processes["daemon"] = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)
            wait_for(lambda: journal().get("initialized"), "daemon did not initialize", processes["daemon"])
            try:
                outside, _, _ = release("1.0.0")
                settles(outside, 2, "outside the access policy's scope")
                print("PASS: device outside the policy: deployment Failed with an on-chain receipt, nothing decrypted")

                policy_call("grant_device", f"@{registry}", f"@{device}")
                granted, artifact, _ = release("1.1.0")
                settles(granted, 1)
                assert installed(artifact)
                print("PASS: granted device: dapp-encrypted recipe and artifact fetched, decrypted with its own "
                      "signing-service session, verified and applied; Applied on chain")

                plain, _, _ = release("1.2.0", confidential=False)
                settles(plain, 2, "unencrypted recipe refused")
                tampered, _, _ = release("1.3.0", tamper=True)
                settles(tampered, 2, "failed authentication")
                foreign, _, _ = release("1.4.0", namespace_override=key_server_package)
                settles(foreign, 2, "another package or network")
                print("PASS: required mode refuses plaintext; tampered ciphertext and another namespace Failed")

                policy_call("revoke_device", f"@{device}")
                policy_call("grant_group", f"@{group}")
                via_group, artifact, _ = release("2.0.0", targets=([], [group]))
                settles(via_group, 1)
                assert installed(artifact)
                transaction("--move-call", f"{package}::device_group::remove_device", f"@{group}", f"@{device}")
                removed, _, _ = release("2.1.0")
                settles(removed, 2, "outside the access policy's scope")
                print("PASS: group grant follows membership: Applied through the group, Failed after removal")

                policy_call("grant_device", f"@{registry}", f"@{device}")
                policy_call("rotate_key_epoch")
                epoch = 1
                stale, _, _ = release("3.1.0", key_epoch=0)
                settles(stale, 2, "rotated")
                fresh, artifact, _ = release("3.2.0")
                settles(fresh, 1)
                print("PASS: key epoch rotation retires earlier identities; a fresh release under the new epoch Applied")

                # Uploaders verify every key server, so encrypt first, then fail servers.
                one_down, _, _ = release("4.0.0", before_deploy=lambda: stop_server(servers[0]))
                settles(one_down, 1)
                start_server(servers[0])
                two_down, _, _ = release("4.1.0", before_deploy=lambda: (stop_server(servers[0]), stop_server(servers[1])))
                settles(two_down, 2, "Seal keys unavailable")
                start_server(servers[0])
                start_server(servers[1])

                def short_lifetimes():
                    stop_server(servers[1])
                    start_server(servers[1], "--max-ttl-minutes", "5")
                    stop_server(servers[2])
                short_session, _, _ = release("4.2.0", before_deploy=short_lifetimes)
                settles(short_session, 2, "InvalidCertificate")
                stop_server(servers[1])
                start_server(servers[1])
                start_server(servers[2])
                recovered, _, _ = release("4.3.0")
                settles(recovered, 1)

                def restart_with_a_server_down():
                    daemon = processes.pop("daemon")
                    daemon.send_signal(signal.SIGINT)
                    daemon.wait(timeout=30)
                    stop_server(servers[0])
                    processes["daemon"] = subprocess.Popen([str(DAEMON)], env={**os.environ, "EDGE_CONFIG": str(config)},
                                                           stdout=log, stderr=log)
                restarted, _, _ = release("4.4.0", before_deploy=restart_with_a_server_down)
                settles(restarted, 1)
                start_server(servers[0])
                print("PASS: threshold 2 of 3: one key server down Applied, two down Failed; a server refusing the "
                      "session lifetime withholds its keys; recovery Applied; a daemon restarted while a key server "
                      "is down still meets the threshold")

                cached = list((root / "state" / "components").rglob("*")) if (root / "state" / "components").exists() else []
                cached_files = [p for p in cached if p.is_file()]
                for path in cached_files:
                    data = path.read_bytes()
                    assert b"suifleet-test-app" not in data or path.suffix == ".json", f"confidential content cached: {path}"
                assert not any("artifacts" in p.parts for p in cached_files), cached_files
                daemon_log = (root / "daemon.log").read_text()
                assert key.read_text().strip() not in daemon_log, "device key in the log"
                print("PASS: no confidential recipe or artifact in the offline cache, no key material in the log")

                # Accounts decrypt with their own keys through the official SDK.
                _, _, recipe_blob = release("5.0.0")
                # A reader account with its own key; approvals are simulated, so it needs no gas.
                secret = os.urandom(32).hex()
                reader = seal_tool({"op": "address", "manifest": manifest, "servers": seal_servers, "secretKey": secret})["address"]
                ask = {"op": "decrypt", "manifest": manifest, "servers": seal_servers, "threshold": 2, "secretKey": secret,
                       "policyId": policy, "envelope": recipe_blob.hex()}
                refused = seal_tool(ask)
                assert "error" in refused and "NoAccess" in (refused["error"] + refused.get("name", "")), refused
                policy_call("grant_account", f"@{reader}")
                time.sleep(2)
                decrypted = seal_tool(ask)
                assert "plaintext" in decrypted, decrypted
                assert json.loads(bytes.fromhex(decrypted["plaintext"]))["componentVersion"] == "5.0.0", decrypted
                print("PASS: an ungranted account is refused by the key servers; once granted it decrypts with its "
                      "own key through the official @mysten/seal SDK")
            except BaseException:
                log.flush()
                key_server_log.flush()
                print((root / "daemon.log").read_text()[-6000:])
                print((root / "key-servers.log").read_text()[-3000:])
                raise
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.kill()
                process.wait()
        walrus.shutdown()
        walrus.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default="http://127.0.0.1:9000")
    parser.add_argument("--faucet", default="http://127.0.0.1:9123/v2/gas")
    parser.add_argument("--upgrade-from", help="git ref whose contracts are published first, then upgraded to this tree")
    args = parser.parse_args()
    run(args.rpc, args.faucet, args.upgrade_from)
