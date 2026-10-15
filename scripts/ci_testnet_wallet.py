#!/usr/bin/env python3
"""Keep the CI signing keystore in anonymous memory, never a disk tempfile."""
import json
import os
from pathlib import Path
import subprocess
import sys

ADDRESS = '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac'


def execute(root, state, base, keydata, run=subprocess.run):
    # CLI subprocesses open the parent's anonymous RAM fd through procfs.
    # CLOEXEC keeps the fd out of children; the parent survives until they exit.
    handle = os.memfd_create('suifleet-ci-keystore', os.MFD_CLOEXEC)
    try:
        os.fchmod(handle, 0o600)
        os.write(handle, keydata)
        keydata = None
        keystore = Path(root) / 'sui.keystore'
        keystore.symlink_to(f'/proc/{os.getpid()}/fd/{handle}')
        wallet = Path(root) / 'client.yaml'
        wallet.write_text(f'''keystore:
  File: {keystore}
envs:
- alias: testnet
  rpc: https://fullnode.testnet.sui.io:443
  ws: null
  basic_auth: null
active_env: testnet
active_address: '{ADDRESS}'
''')
        wallet.chmod(0o600)
        return run(['python3', 'scripts/live_package_upgrade.py', '--wallet', str(wallet),
                    '--base-ref', base, '--state', state, '--evidence', 'evidence/package-upgrade.json',
                    '--faucet-tool', os.environ.get('SUIFLEET_FAUCET_TOOL', '/opt/sui-faucet/sui')],
                   check=False).returncode
    finally:
        os.close(handle)


if __name__ == '__main__':
    os.umask(0o077)
    data = sys.stdin.buffer.read(8193)
    if len(data) > 8192:
        raise ValueError('oversized CI keystore input')
    sys.exit(execute(*sys.argv[1:], data))
