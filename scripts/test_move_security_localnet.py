#!/usr/bin/env python3
"""Run historical receipt and current identity upgrade attacks on disposable chains.

Requires the pinned dev container with --network none and prewarmed dependencies.
Each suite gets a fresh chain/wallet, never an existing developer/testnet wallet.
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from test_x86_catalog_localnet import wait_for_node

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--prior-identity", type=Path, required=True)
    args = parser.parse_args()
    if not Path("/.dockerenv").exists() or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise SystemExit("Requires disposable Docker --network none")
    suites = [
        ["upgrade_security_localnet.py", "--v1", str(args.legacy), "--v2", str(args.receipts),
         "--head", str(ROOT / "contracts")],
        ["identity_lifecycle_localnet.py", "--prior", str(args.prior_identity), "--new", str(ROOT / "contracts")],
    ]
    for suite in suites:
        for port in (9000, 9123):
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    raise SystemExit("Refusing existing localnet service")
        with tempfile.TemporaryDirectory(prefix="sf050-localnet-") as directory:
            private = Path(directory)
            node_dir, wallet = private / "node", private / "wallet"
            node_dir.mkdir()
            wallet.mkdir()
            (wallet / "sui.keystore").write_text("[]")
            (wallet / "client.yaml").write_text(
                f"keystore:\n  File: {wallet / 'sui.keystore'}\nenvs:\n- alias: local\n  rpc: http://127.0.0.1:9000\n"
                "active_env: local\nactive_address: null\n")
            with (private / "node.log").open("w") as log:
                node = subprocess.Popen(["sui", "start", "--with-faucet", "--force-regenesis"],
                                        env=dict(os.environ, SUI_CONFIG_DIR=str(node_dir)), stdout=log, stderr=log)
                try:
                    wait_for_node(node)
                    # JSON-RPC may answer before indexed gRPC has committed its
                    # first checkpoint. Wait for the transport used by the CLI.
                    env = dict(os.environ, SUI_CONFIG_DIR=str(wallet))
                    for _ in range(240):
                        ready = subprocess.run(["sui", "client", "chain-identifier"], env=env,
                                               capture_output=True, timeout=10)
                        if ready.returncode == 0:
                            break
                        if node.poll() is not None:
                            raise RuntimeError("Disposable node stopped before gRPC readiness")
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Indexed gRPC chain identity readiness timeout")
                    for _ in range(240):
                        with socket.socket() as faucet:
                            if faucet.connect_ex(("127.0.0.1", 9123)) == 0:
                                break
                        if node.poll() is not None:
                            raise RuntimeError("Disposable node stopped before faucet readiness")
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Disposable faucet readiness timeout")
                    bootstrap = subprocess.run(["sui", "client", "new-address", "ed25519", "bootstrap", "--json"],
                                               env=env, capture_output=True)
                    if bootstrap.returncode:
                        raise RuntimeError("Temporary wallet bootstrap failed (private output withheld)")
                    subprocess.run(["python3", str(ROOT / "scripts" / suite[0]), *suite[1:]],
                                   env=dict(os.environ, SUI_CONFIG_DIR=str(wallet)), check=True)
                finally:
                    node.terminate()
                    try:
                        node.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        node.kill()
                        node.wait()


if __name__ == "__main__":
    main()
