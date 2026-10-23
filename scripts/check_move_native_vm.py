#!/usr/bin/env python3
"""Run native conformance fixtures on the pinned release Move VM, without keys."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="sf050-native-vm-") as directory:
        temporary = Path(directory)
        package = temporary / "fixtures"
        shutil.copytree(ROOT / "contracts/security-native-fixtures", package,
                        ignore=shutil.ignore_patterns("build", "Move.lock", "Published.toml"))
        config = temporary / "config"
        config.mkdir()
        (config / "sui.keystore").write_text("[]")
        (config / "client.yaml").write_text(
            f"keystore:\n  File: {config / 'sui.keystore'}\n"
            "envs:\n  - alias: testnet\n    rpc: https://fullnode.testnet.sui.io:443\n"
            "active_env: testnet\nactive_address: ~\n")
        env = dict(os.environ, SUI_CONFIG_DIR=str(config))
        version = subprocess.check_output(["sui", "--version"], text=True).strip()
        if version != "sui 1.81.1-e04cf9aa915c":
            raise SystemExit("Release compiler/VM version mismatch: " + version)
        subprocess.run(["sui", "move", "test", "--path", str(package)], env=env, check=True)


if __name__ == "__main__":
    main()
