#!/usr/bin/env python3
"""SF-091 bounded Testnet recovery confirmation from a clean operator state.

Restores the public configuration with `fleet_recovery.py restore` into a new
private temporary directory, then runs `fleet_recovery_check.cjs` against the
real fullnode/Walrus aggregator. With `--wallet`, the explicitly selected
wallet's account must hold the fleet's BudgetOwnerCap; the acceptance-only
native bridge signs only the five-minute Seal session personal message, so key
servers release keys under the owner policy. No transaction is built, signed
or submitted, so no gas is spent and no mutation lock is required.

Run inside the pinned dev image after `make dapp-install` and building the
`sign_seal_session` example (`cargo build --examples`).
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleet_recovery

V = ROOT / 'docs/validation'
DEFAULTS = dict(manifest=V / 'sf082-testnet-deployment.json', build_receipt=V / 'sf082-site-build-a.json',
                publication_receipt=V / 'sf082-site-publication.json', fleet_data=V / 'sf082-testnet-fleet.json',
                seal_settings=V / 'sf082-testnet-seal.json', fleet_storage=V / 'sf082-testnet-storage.json')


def wallet_account(wallet):
    text = wallet.read_text()
    paths = re.findall(r'^\s+File:\s*(\S.*?)\s*$', text, re.MULTILINE)
    address = re.findall(r'^active_address:\s*"?(0x[0-9a-fA-F]+)"?\s*$', text, re.MULTILINE)
    if len(paths) != 1 or len(address) != 1:
        raise SystemExit('explicit wallet must name one file keystore and one active address')
    keystore = json.loads(paths[0]) if paths[0].startswith('"') else paths[0]
    if not Path(keystore).is_absolute():
        raise SystemExit('explicit absolute file keystore required')
    return keystore, address[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--wallet', type=Path, help='explicit Testnet client.yaml whose account holds the BudgetOwnerCap')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='suifleet-clean-operator-') as clean:
        env_file = Path(clean) / 'restored.env'
        restore = fleet_recovery.main(['restore', *sum((['--' + k.replace('_', '-'), str(v)] for k, v in DEFAULTS.items()), []),
                                       '--env-out', str(env_file)])
        if restore != 0:
            raise SystemExit('public configuration restore refused')
        command = ['node', str(ROOT / 'scripts/fleet_recovery_check.cjs'), str(env_file), str(args.evidence)]
        keystore = owner = None
        if args.wallet:
            keystore, owner = wallet_account(args.wallet)
            command += ['--owner', owner]
        namespace = json.loads(DEFAULTS['fleet_data'].read_text())['typeOrigin']
        environment = {k: v for k, v in os.environ.items() if not k.startswith('NEXT_PUBLIC_')} | {'HOME': clean}
        node = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            for line in node.stdout:
                message = json.loads(line)
                if 'personal_message' in message and owner:
                    decoded = base64.b64decode(message['personal_message'], validate=True)
                    assert 0 < len(decoded) < 8192
                    signed = subprocess.run([str(ROOT / 'suifleet-edge/target/debug/examples/sign_seal_session')],
                        input=json.dumps(dict(wallet=str(args.wallet), keystore=keystore, expected_address=owner,
                                              namespace=namespace, message=message['personal_message'])),
                        capture_output=True, text=True, timeout=60)
                    if signed.returncode != 0:
                        raise SystemExit('native bridge refused the owner Seal session')
                    node.stdin.write(json.dumps({'signature': json.loads(signed.stdout)['signature']}) + '\n')
                    node.stdin.flush()
                elif 'summary' in message:
                    print(json.dumps(message['summary']))
                else:
                    raise SystemExit('unexpected recovery-check request')
            if node.wait(timeout=120) != 0:
                raise SystemExit('recovery check failed')
        finally:
            if node.poll() is None:
                node.kill()
                node.wait()


if __name__ == '__main__':
    main()
