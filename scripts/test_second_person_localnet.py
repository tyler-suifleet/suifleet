#!/usr/bin/env python3
"""Run SF-199 existing-object/old-package acceptance in a network-none container.

--base is an exported framework contracts directory, never an app deployment.
The node, faucet, wallet and fixture cache are temporary and destroyed on exit.
Only sanitized effects and public IDs are retained in --evidence.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import sui_toolchain as tc


def run(args):
    if not Path("/.dockerenv").exists() or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise RuntimeError("requires a disposable Docker container with --network none")
    tools = tc.check_tools("sui", tc.NETWORK_DEFAULT)
    with tempfile.TemporaryDirectory(prefix="sf199-local-") as directory:
        private = Path(directory)
        base = private / "base" / "contracts"
        shutil.copytree(args.base, base, ignore=shutil.ignore_patterns("build"))
        wallet = private / "client.yaml"
        (private / "sui.keystore").write_text("[]")
        wallet.write_text(f"keystore:\n  File: {private / 'sui.keystore'}\nenvs:\n- alias: testnet\n  rpc: http://127.0.0.1:9000\nactive_env: testnet\nactive_address: null\n")
        env = dict(os.environ, SUI_CONFIG_DIR=str(private), SUIFLEET_NETWORK_SUI=tools["network"]["path"])
        cli = [tools["network"]["path"], "client", "--client.config", str(wallet)]
        # new-address output contains a recovery phrase: never forward it.
        subprocess.run([*cli, "new-address", "ed25519"], capture_output=True, env=env, check=True)
        with (private / "node.log").open("w") as log:
            node = subprocess.Popen([tools["network"]["path"], "start", "--with-faucet", "--force-regenesis"], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(90):
                    if node.poll() is not None:
                        raise RuntimeError("disposable node exited; private logs withheld")
                    ready = subprocess.run([*cli, "chain-identifier"], capture_output=True, env=env, timeout=3)
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("disposable node startup timed out")
                # Node gRPC readiness precedes faucet HTTP readiness. One local
                # grant is enough; bounded retries only cover startup races.
                for attempt in range(15):
                    funded = subprocess.run([*cli, "faucet", "--url", "http://127.0.0.1:9123/v2/gas"], capture_output=True, env=env)
                    if funded.returncode == 0: break
                    time.sleep(1)
                else: raise RuntimeError("disposable faucet startup timed out")
                subprocess.run([sys.executable, "scripts/live_second_person.py", "--wallet", str(wallet), "--network", "localnet",
                                "--base", str(base), "--state", str(private / "state.json"), "--evidence", args.evidence], env=env, check=True)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    node.kill(); node.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--evidence", required=True)
    run(parser.parse_args())
