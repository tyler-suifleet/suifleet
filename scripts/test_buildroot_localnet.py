#!/usr/bin/env python3
"""Exercise the Buildroot package in a disposable VM against a real local Sui node.

Run the host side in the node's network namespace. QEMU user networking reaches
that namespace's loopback at 10.0.2.2. The guest side is run by a test-only init
hook, after the normal package init hook has run with an unconfigured image.
See docs/BUILDROOT_PACKAGING.md.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen

RPC = "http://127.0.0.1:9000"
FAUCET = "http://127.0.0.1:9123/v2/gas"
PORT = 18080
GUEST_HOST = "10.0.2.2"
GUEST_URL = f"http://{GUEST_HOST}:{PORT}"
INIT = "/etc/init.d/S95suifleet"


def host(claim_tool):
    import deployment_manifest as dm
    from edge_acceptance import SuiCli, created, wait_for
    import test_component

    blobs = {}
    posts = {}

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            blob = blobs.get(self.path)
            self.send_response(200 if blob is not None else 404)
            self.end_headers()
            if blob is not None:
                self.wfile.write(blob)

        def do_POST(self):
            if self.path not in ("/enrolled", "/installed"):
                self.send_error(404)
                return
            posts[self.path] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

    with tempfile.TemporaryDirectory(prefix="sf296-") as temporary, ThreadingHTTPServer(("127.0.0.1", PORT), Fixture) as server:
        root = Path(temporary)
        wallet = root / "wallet.yaml"
        keystore = root / "keystore"
        keystore.write_text("[]")
        wallet.write_text(f"keystore:\n  File: {keystore}\nenvs:\n- alias: local\n  rpc: {RPC}\nactive_env: local\nactive_address: null\n")
        cli = SuiCli(wallet)
        operator = cli("new-address", "ed25519")["address"]
        cli("switch", "--address", operator, json_output=False)
        cli("faucet", "--url", FAUCET, json_output=False)
        wait_for(lambda: cli.balance() > 0, "local operator not funded")

        def checked(result):
            assert result["effects"]["status"]["status"] == "success", result["effects"]["status"]
            return result

        def ptb(*args):
            return checked(cli("ptb", *args, "--gas-budget", "200000000"))

        contracts = root / "contracts"
        shutil.copytree(Path(__file__).resolve().parents[1] / "contracts", contracts,
                        ignore=shutil.ignore_patterns("build", "Published.toml"))
        published = checked(cli("test-publish", contracts, "--build-env", "testnet",
                                "--pubfile-path", root / "Pub.local.toml", "--gas-budget", "900000000"))
        package = next(change["packageId"] for change in published["objectChanges"] if change["type"] == "published")
        registry = created(published, "::device_registry::DeviceRegistry")[0]
        upgrade = created(published, "::package::UpgradeCap")[0]
        chain = dm.chain_facts(RPC)["chain_identifier"]
        manifest = dm.resolve(SimpleNamespace(chain_identifier=chain, rpc=RPC, package=package,
                                              upgrade_cap=upgrade, registry=registry, network="localnet",
                                              publisher=GUEST_URL, aggregator=GUEST_URL))
        manifest["rpc_url"] = f"http://{GUEST_HOST}:9000"
        manifest["rpc_urls"] = [manifest["rpc_url"]]
        policy = created(ptb("--move-call", f"{package}::enrollment::create_policy", f"@{registry}", "1", "0"),
                         "::enrollment::EnrollmentPolicy")[0]
        claim_path = root / "claim.key"
        claim_address = subprocess.check_output([claim_tool, "--provision", claim_path], text=True).strip()
        ptb("--move-call", f"{package}::enrollment::add_claims", f"@{policy}", f"vector[@{claim_address}]",
            "--split-coins", "gas", "[30000000]", "--assign", "fee",
            "--transfer-objects", "[fee]", f"@{claim_address}")
        blobs["/enroll.toml"] = (f'manifest_path = "/etc/suifleet/deployment.json"\npolicy_id = "{policy}"\n'
                                 'claim_key_path = "/run/sf296-claim.key"\nname = "buildroot-guest"\n'
                                 'keypair_path = "/etc/suifleet/device.key"\n').encode()
        blobs["/deployment.json"] = json.dumps(manifest).encode()
        blobs["/claim.key"] = claim_path.read_bytes()
        blobs["/updater"] = test_component.UPDATER.read_bytes()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print("SF296_HOST_READY", flush=True)
            wait_for(lambda: "/enrolled" in posts, "VM did not enroll", timeout=240)
            item = posts["/enrolled"]
            enrollment = checked(cli.transaction_block(item["digest"]))
            address = dm.object_id(item["device_address"])
            cap = cli("object", item["device_cap_id"])
            owner = cli("object", item["owner_cap_id"])
            assert dm.object_id(cap["owner"]["AddressOwner"]) == address
            assert dm.object_id(owner["owner"]["AddressOwner"]) == dm.object_id(operator)
            assert dm.object_id(enrollment["transaction"]["data"]["gasData"]["owner"]) == dm.object_id(claim_address)
            assert cli.balance(address) == 0
            ptb("--split-coins", "gas", "[50000000]", "--assign", "fee",
                "--transfer-objects", "[fee]", f"@{address}")

            run_id = chain
            artifact = test_component.build("1.0.0", run_id)
            recipe = test_component.recipe(artifact, "artifact")
            blobs["/v1/blobs/artifact"] = artifact
            blobs["/v1/blobs/recipe"] = recipe
            installed = ptb("--move-call", f"{package}::deployment_manager::create_deployment_verified",
                            f'"{test_component.DEFAULT_COMPONENT}"', '"1.0.0"', '"recipe"',
                            f'"{hashlib.sha256(artifact).hexdigest()}"', f'"{hashlib.sha256(recipe).hexdigest()}"',
                            "vector[]", f'vector[@{item["device_object_id"]}]', "@0x6")
            deployment = created(installed, "::deployment_manager::DeploymentRecord")[0]
            device = dm.object_id(item["device_object_id"])
            wait_for(lambda: cli.receipts(deployment).get(device) == 1, "no authoritative Applied receipt", timeout=180)
            blobs["/reported"] = b"Applied"
            wait_for(lambda: "/installed" in posts, "VM init lifecycle did not finish", timeout=60)
            assert posts["/installed"]["runId"] == run_id
            assert posts["/installed"]["version"] == "1.0.0"
            assert posts["/installed"]["artifactSha256"] == hashlib.sha256(artifact).hexdigest()
            assert posts["/installed"]["health"] == "passed"
            print(json.dumps({"chain": chain, "publish": published["digest"], "enroll": enrollment["digest"],
                              "deployment": deployment, "deploy": installed["digest"], "address": address,
                              "receipt": "Applied", "guest": posts["/installed"]}), flush=True)
        finally:
            server.shutdown()
            thread.join()


def guest():
    import os
    import pwd
    from urllib.error import HTTPError
    import stat
    import time

    assert "suifleet_sf296_test=1" in Path("/proc/cmdline").read_text().split()
    etc = Path("/etc/suifleet")
    state = Path("/var/lib/suifleet")
    key = etc / "device.key"
    pidfile = Path("/run/suifleet-portable/launcher.pid")
    socket = Path("/run/suifleet-portable/signer.sock")
    assert not key.exists() and not (etc / "config.toml").exists() and not pidfile.exists()
    assert not (state / "state.json").exists()
    os.umask(0o077)
    etc.mkdir(mode=0o755, exist_ok=True)
    etc.chmod(0o755)

    def fetch(name):
        with urlopen(f"{GUEST_URL}/{name}", timeout=15) as response:
            return response.read()

    def post(path, value):
        with urlopen(Request(f"{GUEST_URL}/{path}", data=json.dumps(value).encode()), timeout=15) as response:
            assert response.status == 204

    address = subprocess.check_output(["suifleet", "--provision", key], text=True).strip()
    assert subprocess.check_output(["suifleet", "--provision", key], text=True).strip() == address
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    manifest = etc / "deployment.json"
    manifest.write_bytes(fetch("deployment.json"))
    manifest.chmod(0o644)
    claim = Path("/run/sf296-claim.key")
    enroll = Path("/run/sf296-enroll.toml")
    claim.write_bytes(fetch("claim.key"))
    enroll.write_bytes(fetch("enroll.toml"))
    try:
        item = json.loads(subprocess.check_output(["suifleet", "--enroll", enroll]))
    finally:
        claim.unlink()
        enroll.unlink()
    assert item["device_address"] == address

    signer = pwd.getpwnam("suifleet-signer")
    daemon = pwd.getpwnam("suifleet")
    os.chown(key, signer.pw_uid, signer.pw_gid)
    signer_config = etc / "signer.toml"
    signer_config.write_text(f'manifest_path = "deployment.json"\ndevice_cap_id = "{item["device_cap_id"]}"\n'
                             f'keypair_path = "{key}"\nportable = true\n')
    os.chown(signer_config, 0, signer.pw_gid)
    signer_config.chmod(0o640)
    updater = Path("/run/sf296-updater")
    updater.write_bytes(fetch("updater"))
    updater.chmod(0o755)
    config = etc / "config.toml"
    config.write_text(f'[sui]\nmanifest_path = "deployment.json"\n[device]\n'
                      f'device_object_id = "{item["device_object_id"]}"\ndevice_cap_id = "{item["device_cap_id"]}"\n'
                      f'[device.signer]\nkind = "service"\nsocket = "{socket}"\n[walrus]\n'
                      f'aggregator_url = "{GUEST_URL}"\n[apply]\ncommand = "{updater}"\n[runtime]\n'
                      f'state_dir = "{state}"\npoll_seconds = 1\nevent_discovery_seconds = 1\n')
    os.chown(config, 0, daemon.pw_gid)
    config.chmod(0o640)

    def wait_for(condition):
        deadline = time.monotonic() + 180
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError("VM service condition timed out")
            time.sleep(0.25)

    try:
        subprocess.run([INIT, "start"], check=True)
        journal = state / "state.json"
        wait_for(lambda: socket.exists() and journal.exists() and json.loads(journal.read_text()).get("initialized"))
        starting_pid = pidfile.read_text()
        subprocess.run([INIT, "start"], check=True)
        assert pidfile.read_text() == starting_pid
        post("enrolled", item)
        receipt = state / "test-components/suifleet-test-app/receipt.json"
        wait_for(receipt.exists)
        proof = json.loads(receipt.read_text())

        def reported():
            try:
                return fetch("reported") == b"Applied"
            except HTTPError as error:
                if error.code != 404:
                    raise
                return False

        wait_for(reported)
        subprocess.run([INIT, "restart"], check=True)
        wait_for(socket.exists)
        subprocess.run([INIT, "stop"], check=True)
        assert not pidfile.exists() and not socket.exists()
        proof.update(kernel=os.uname().release, machine=os.uname().machine,
                     init=Path("/proc/1/comm").read_text().strip())
        post("installed", proof)
    finally:
        subprocess.run([INIT, "stop"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest", action="store_true")
    parser.add_argument("--claim-tool", type=Path, help="host SuiFleet binary for generating a disposable claim key")
    options = parser.parse_args()
    if options.guest:
        guest()
    elif options.claim_tool:
        host(options.claim_tool.resolve(strict=True))
    else:
        parser.error("provide --claim-tool on the host")
