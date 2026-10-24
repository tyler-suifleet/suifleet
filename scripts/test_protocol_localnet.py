#!/usr/bin/env python3
"""Run SF-005 generated calls on an isolated pinned-toolchain localnet.

Run in a disposable dev container, preferably with --network none. This starts
only loopback services, uses temporary keys/state and never modifies wallets.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import time
from urllib.request import Request, urlopen


def main(script="scripts/test_protocol_live.cjs"):
    if not Path("/.dockerenv").exists() or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise SystemExit("Use a disposable Docker container with --network none; existing local/public chains are not permitted")
    for port in (9000, 9123):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Refusing existing loopback service on port {port}")
    with tempfile.TemporaryDirectory(prefix="protocol-localnet-") as directory:
        env = dict(os.environ, SUI_CONFIG_DIR=directory, PROTOCOL_TEST_DISPOSABLE="1",
                   PROTOCOL_TEST_RPC="http://127.0.0.1:9000", PROTOCOL_TEST_FAUCET="http://127.0.0.1:9123", FLEET_TEST_RPC="http://127.0.0.1:9000", FLEET_TEST_FAUCET="http://127.0.0.1:9123")
        with open(Path(directory) / "node.log", "w") as log:
            node_tool = "sui"
            if script in ("scripts/test_fleet_data_upgrade.cjs", "scripts/test_budget_enrollment_localnet.cjs"):
                import sui_toolchain
                node_tool = sui_toolchain.check_tools("sui", sui_toolchain.NETWORK_DEFAULT)["network"]["path"]
            node = subprocess.Popen([node_tool, "start", "--with-faucet", "--force-regenesis"], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for attempt in range(120):
                    if node.poll() is not None:
                        raise RuntimeError("disposable localnet exited; temporary node logs may contain secrets and are not printed")
                    try:
                        if script == "scripts/test_budget_enrollment_localnet.cjs":
                            probe = "const {appRequire}=require('./scripts/frontend_test_utils.cjs'); const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'); new SuiGrpcClient({network:'localnet',baseUrl:'http://127.0.0.1:9000'}).ledgerService.getServiceInfo({}).then(()=>process.exit(0),()=>process.exit(1));"
                            ready = subprocess.run(["node", "-e", probe], capture_output=True, timeout=3)
                            if ready.returncode == 0:
                                break
                            time.sleep(0.5)
                            continue
                        if script == "scripts/test_fleet_data_upgrade.cjs":
                            ready = subprocess.run(["suifleet-edge/target/debug/examples/chain_facts", "http://127.0.0.1:9000"], capture_output=True, timeout=2)
                            if ready.returncode == 0:
                                break
                            time.sleep(0.5)
                            continue
                        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"sui_getChainIdentifier","params":[]}).encode()
                        with urlopen(Request("http://127.0.0.1:9000", body, {"Content-Type":"application/json"}), timeout=1) as response:
                            if json.load(response).get("result"):
                                break
                    except Exception:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("localnet startup timeout")
                subprocess.run(["node", script], check=True, env=env)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    node.kill(); node.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handover-upgrade", action="store_true")
    parser.add_argument("--retention-keyed-upgrade", action="store_true", help="SF128 populated upgrade, expiry and device checks without repeating the scale population")
    parser.add_argument("--retention-scale-upgrade", action="store_true")
    parser.add_argument("--retention-upgrade", action="store_true")
    parser.add_argument("--device-retention-upgrade", action="store_true")
    parser.add_argument("--budget-upgrade", action="store_true", help="SF-083 fleet budget upgrade, attacks and daemon draws")
    parser.add_argument("--fleet-data-upgrade", action="store_true", help="SF-082 existing-object/data-package upgrade with real pinned Walrus dependencies")
    parser.add_argument("--budget-enrollment", action="store_true", help="SF-132 1000-device gas-account batching and measurements")
    args = parser.parse_args()
    if args.retention_keyed_upgrade: os.environ["SF128_UPGRADE_ONLY"] = "1"
    if args.retention_scale_upgrade or args.retention_keyed_upgrade: os.environ["SF128_SCALE_UPGRADE"] = "1"
    if args.device_retention_upgrade: os.environ["SF082_DEVICE_POLICY_UPGRADE"] = "1"
    main("scripts/test_budget_enrollment_localnet.cjs" if args.budget_enrollment else "scripts/test_fleet_data_upgrade.cjs" if args.fleet_data_upgrade else "scripts/test_retention_upgrade.cjs" if args.retention_upgrade or args.device_retention_upgrade or args.retention_scale_upgrade or args.retention_keyed_upgrade else "scripts/test_handover_upgrade.cjs" if args.handover_upgrade
         else "scripts/test_budget_upgrade.cjs" if args.budget_upgrade else "scripts/test_protocol_live.cjs")
