#!/usr/bin/env python3
"""Run SF-011 catalog SDK and baseline-upgrade daemon acceptance on isolated Sui.

Run in a disposable dev container, preferably with --network none. This starts
only loopback services, uses temporary keys/state and never modifies wallets.
"""
import json
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import time
from urllib.request import Request, urlopen


def main():
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
            node = subprocess.Popen(["sui", "start", "--with-faucet", "--force-regenesis"], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for attempt in range(120):
                    if node.poll() is not None:
                        raise RuntimeError("disposable localnet exited; temporary node logs may contain secrets and are not printed")
                    try:
                        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"sui_getChainIdentifier","params":[]}).encode()
                        with urlopen(Request("http://127.0.0.1:9000", body, {"Content-Type":"application/json"}), timeout=1) as response:
                            if json.load(response).get("result"):
                                break
                    except Exception:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("localnet startup timeout")
                subprocess.run(["node", "scripts/test_catalog_live.cjs"], check=True, env=env)
                subprocess.run(["python3", "scripts/test_daemon_localnet.py", "--catalog-upgrade"], check=True, env=env)
            finally:
                node.terminate()
                try:
                    node.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    node.kill(); node.wait()


if __name__ == "__main__":
    main()
