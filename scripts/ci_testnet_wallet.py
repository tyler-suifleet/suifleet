#!/usr/bin/env python3
"""Keep the CI signing keystore in anonymous memory, never a disk tempfile."""
import json
import os
from pathlib import Path
import subprocess
import sys

ADDRESS = '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac'
# SF-010 suites after the package-upgrade gate, each with its own strict
# allowance (run ceiling: 0.6 + 0.5 + 0.15 SUI). They spend real gas, so
# they run only when the head changes what they test, or on a manual
# dispatch (the `wallet` live gate); scheduled main runs skip them.
SUITES = (('upgrade-security', 'scripts/live_upgrade_security.py', 700_000_000),
          ('wallet', 'scripts/live_wallet_gate.py', 150_000_000))
SUITE_PATHS = ('contracts/', 'dapp/', 'interfaces/', 'suifleet-edge/', 'scripts/browser/', 'scripts/live_',
               'scripts/ci_testnet', 'scripts/deployment_manifest.py', 'scripts/testnet_budget.py',
               'scripts/sui_toolchain.py', 'scripts/sui_tx_inspect.cjs', 'Dockerfile')
# Funded test keys a suite keeps beside the wallet until their sweep succeeds.
PENDING_KEYS = ('live-browser-key.hex', 'live-security-device.key')


def suites_relevant(base, git=subprocess.run):
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        return True
    changed = git(['git', 'diff', '--name-only', f'{base}...HEAD'], capture_output=True, text=True, check=True).stdout
    return any(name.startswith(SUITE_PATHS) for name in changed.split())


def restore_pending(root, state):
    """The CI wallet directory is temporary; unswept test keys live in the
    (encrypted) fixture state between runs."""
    try:
        pending = json.loads(Path(state).read_text()).get('pending_keys') or {}
    except (OSError, ValueError):
        return
    for name, seed in pending.items():
        if name in PENDING_KEYS:
            path = Path(root) / name
            path.touch(mode=0o600)
            path.write_text(seed + '\n')


def save_pending(root, state):
    path = Path(state)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data['pending_keys'] = {name: (Path(root) / name).read_text().strip()
                            for name in PENDING_KEYS if (Path(root) / name).exists()}
    temporary = path.with_suffix('.pending.tmp')
    temporary.touch(mode=0o600)
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def execute(root, state, base, keydata, run=subprocess.run, relevant=suites_relevant):
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
        code = run(['python3', 'scripts/live_package_upgrade.py', '--wallet', str(wallet),
                    '--base-ref', base, '--state', state, '--evidence', 'evidence/package-upgrade.json',
                    '--faucet-tool', os.environ.get('SUIFLEET_FAUCET_TOOL', '/opt/sui-network/sui')],
                   check=False).returncode
        if code or not relevant(base):
            if not code:
                print('SF-010 upgrade-security/wallet suites skipped: head changes none of their inputs')
            return code
        restore_pending(root, state)
        try:
            for name, script, ceiling in SUITES:
                print(f'SF-010 {name} suite (allowance {ceiling} MIST)', flush=True)
                code = run(['python3', script, '--wallet', str(wallet), '--state', state,
                            '--max-spend-mist', str(ceiling), '--evidence', f'evidence/{name}.json'],
                           check=False).returncode
                if code:
                    return code
            return 0
        finally:
            save_pending(root, state)
    finally:
        os.close(handle)


if __name__ == '__main__':
    os.umask(0o077)
    data = sys.stdin.buffer.read(8193)
    if len(data) > 8192:
        raise ValueError('oversized CI keystore input')
    sys.exit(execute(*sys.argv[1:], data))
