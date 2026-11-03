#!/usr/bin/env python3
"""Run SF-196 existing-object/old-package acceptance in a network-none container.

--base is an exported pre-framework contracts directory, never an app deployment.
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
from urllib.request import urlopen
from urllib.error import HTTPError

import sui_toolchain as tc


def run(args):
    if not Path("/.dockerenv").exists() or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise RuntimeError("requires a disposable Docker container with --network none")
    if args.actions and (args.only_multisig or args.suite != "live_security_policy" or args.runner):
        raise ValueError("--actions requires the default security-policy fixture")
    if args.only_multisig and not args.multisig:
        raise ValueError("--only-multisig requires --multisig evidence path")
    tools = tc.check_tools("sui", tc.NETWORK_DEFAULT)
    with tempfile.TemporaryDirectory(prefix="sf196-local-") as directory:
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
                for _ in range(60):
                    try:
                        with urlopen("http://127.0.0.1:9123/", timeout=2): pass
                        break
                    except HTTPError as error:
                        if error.code < 500: break
                        time.sleep(1)
                    except OSError:
                        time.sleep(1)
                for _ in range(3):
                    subprocess.run([*cli, "faucet", "--url", "http://127.0.0.1:9123/v2/gas"], capture_output=True, env=env, check=True)
                if not args.only_multisig:
                    subprocess.run([sys.executable, args.runner or "scripts/" + args.suite + ".py", "--wallet", str(wallet), "--network", "localnet",
                                    "--base", str(base), "--state", str(private / "state.json"), "--evidence", args.evidence,
                                    *(["--max-spend-mist", "3000000000"] if not args.runner else [])], env=env, check=True)
                if args.actions:
                    subprocess.run([sys.executable, "scripts/live_security_actions.py", "--wallet", str(wallet),
                                    "--network", "localnet", "--fixture-receipt", args.evidence,
                                    "--evidence", args.actions], env=env, check=True)
                if args.multisig:
                    subprocess.run([sys.executable, "scripts/live_multisig_admin.py", "--wallet", str(wallet), "--network", "localnet",
                                    "--state", str(private / "multisig"), "--evidence", args.multisig], env=env, check=True)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    node.kill(); node.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", choices=["scripts/live_security_policy.py", "scripts/live_passkey_factor.py"])
    parser.add_argument("--suite", choices=["live_security_policy", "live_security_delay"], default="live_security_policy")
    parser.add_argument("--base", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--actions", help="also run canonical/tampered/cancelled Action acceptance using the policy fixture")
    parser.add_argument("--multisig", help="also execute native quorum acceptance; sanitized evidence path")
    parser.add_argument("--only-multisig", action="store_true", help="run only the native quorum fixture (requires --multisig)")
    run(parser.parse_args())
