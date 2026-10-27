#!/usr/bin/env python3
"""SF165 automatic dedicated testnet acceptance with exact-byte signing and spend ledger."""
import json
import os
from pathlib import Path
import subprocess
import sys

from live_common import Live, ROOT, TESTNET_WALLETS, plain
import deployment_manifest as dm
import sui_toolchain as toolchain


def verify_step(data, label, operator, reserved, package, walrus_package=None, blob=None):
    if data['sender'] != operator or data['gas_owner'] != operator or int(data['gas_budget']) != reserved:
        raise toolchain.NotSubmitted('Unexpected transaction sender/gas')
    expected = {
        'create disposable catalog': (package, 'component_catalog', 'create_catalog'),
        'publish chosen-period version': (package, 'component_catalog', 'publish_version'),
        'in-app wallet storage extension': (walrus_package, 'system', 'extend_blob'),
    }.get(label)
    if not expected or not expected[0]:
        raise toolchain.NotSubmitted('Unknown acceptance step')
    extension = label == 'in-app wallet storage extension'
    calls = [c for c in data['commands'] if c['kind'] == 'MoveCall']
    wanted = [(dm.object_id(expected[0]), expected[1], expected[2])]
    if extension:
        wanted.append((dm.object_id('0x2'), 'coin', 'destroy_zero'))
    actual = [(dm.object_id(c['package']), c['module'], c['function']) for c in calls]
    if actual != wanted:
        raise toolchain.NotSubmitted('Unexpected transaction call')
    allowed = {'MoveCall', 'SplitCoins', 'MergeCoins'} if extension else {'MoveCall'}
    if any(c['kind'] not in allowed for c in data['commands']):
        raise toolchain.NotSubmitted('Unexpected transaction command')
    if extension:
        def object_argument(index):
            arg = calls[0]['arguments'][index]
            if arg.get('kind') != 'Input':
                raise toolchain.NotSubmitted('Unexpected extension object argument')
            return data['inputs'][arg['index']].get('objectId')
        if object_argument(0) != '0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af' or object_argument(1) != blob:
            raise toolchain.NotSubmitted('Extension does not use the pinned system and run blob')


def run(wallet, evidence, build_sui, network_sui):
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('Acquire network:suifleet-testnet before this run')
    live = Live(wallet, 'testnet', 150_000_000, build_sui=build_sui, network_sui=network_sui)
    live.tools = toolchain.check_tools(build_sui, network_sui)
    envs, active = live.client('envs')
    live.rpc_url = next(e['rpc'] for e in envs if e['alias'] == active)
    live.operator = dm.object_id(live.client('active-address', json_output=False).stdout.strip().split()[-1])
    assert live.operator in TESTNET_WALLETS
    manifest = json.loads((ROOT / 'profiles/seal-testnet/deployment.json').read_text())
    env = {**os.environ, 'NEXT_PUBLIC_DEPLOYMENT_MANIFEST': json.dumps(manifest)}
    cmd = ['node', str(ROOT / 'scripts/live_release_storage.cjs')]
    facts = json.loads(subprocess.check_output([*cmd, 'facts', live.rpc_url], env=env, text=True, timeout=120))
    previous = json.loads(Path(evidence).read_text()) if Path(evidence).exists() else None
    if previous:
        assert previous['operator'] == live.operator and previous['chain'] == facts['chain_identifier']
        assert not any(t['status'] == 'unknown' for t in previous['transactions']), 'Reconcile unknown outcomes before resuming'
        live.evidence = previous
        live.ledger.evidence = live.evidence['transactions']
        live.ledger.used = previous['allowance_used_mist']
    live.evidence['preflight'] = toolchain.preflight(live.tools, facts, '4c78adac')
    live.chain = facts['chain_identifier']
    balance = live.balance()
    live.start_balance = previous['start_balance_mist'] if previous else balance
    assert balance >= live.max_spend - live.ledger.used, 'Dedicated wallet needs funding before this bounded run'
    live.evidence.update(chain=live.chain, operator=live.operator, max_spend_mist=live.max_spend, start_balance_mist=live.start_balance,
                         model='gpt-6.1-sol', toolchain=live.tools)
    # Convert a small bounded amount into reusable test WAL; the SUI grant also
    # consumes the non-replenishing run allowance. No faucet or other wallet.
    def checkpoint():
        live.evidence['allowance_used_mist'] = live.ledger.used
        tmp = Path(evidence + '.tmp')
        tmp.write_text(json.dumps(live.evidence, indent=2) + '\n')
        tmp.replace(evidence)
    if not any(t['label'] == 'acquire extension WAL' and t['status'] == 'success' for t in live.evidence['transactions']):
        swap = live.transaction('acquire extension WAL', '--split-coins', 'gas', '[1000000u64]', '--assign', 'sui',
        '--move-call', '0x82593828ed3fcb8c6a235eac9abd0adbe9c5f9bbffa9b1e7a45cdd884481ef9f::wal_exchange::exchange_all_for_wal',
        '@0xf4d164ea2def5fe07dc573992a029e010dba09b1a8dcbc44c5c2e79567f39073', 'sui', '--assign', 'wal',
        '--transfer-objects', '[wal]', f'@{live.operator}', budget=15_000_000, funding=1_000_000)
    checkpoint()
    env['SUIFLEET_RELEASE_STORAGE_EVIDENCE'] = evidence
    env['SUIFLEET_RELEASE_STORAGE_STATE'] = evidence + '.state'
    node = subprocess.Popen([*cmd, 'run', live.rpc_url, live.operator, '30000000'], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        while line := node.stdout.readline():
            step = json.loads(line)
            if 'upload' in step:
                uploads = live.evidence.setdefault('uploads', [])
                if not any(u['blobObjectId'] == step['upload']['blobObjectId'] for u in uploads):
                    uploads.append(step['upload'])
                checkpoint(); continue
            if 'result' in step:
                live.evidence['release_storage'] = step['result']
                break
            tx = step['tx']
            def submit(reserved):
                data = toolchain.decode(tx)
                allowed_calls = {
                    'create disposable catalog': (manifest['call_package_id'], 'component_catalog', 'create_catalog'),
                    'publish chosen-period version': (manifest['call_package_id'], 'component_catalog', 'publish_version'),
                }
                is_extension = step['label'] == 'in-app wallet storage extension'
                expected_blob = None
                if is_extension:
                    system_id = '0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af'
                    system = live.object(system_id)
                    assert system['type'].endswith('::system::System') and 'Shared' in system['owner']
                    package = plain(system['content'])['package_id']
                    wanted_call = (package, 'system', 'extend_blob')
                    expected_blob = live.evidence['uploads'][0]['blobObjectId']
                    if step['expected'].get('blob') != expected_blob:
                        raise toolchain.NotSubmitted('Extension does not name this run artifact')
                else:
                    wanted_call = allowed_calls.get(step['label'])
                    if not wanted_call:
                        raise toolchain.NotSubmitted('Unknown acceptance step')
                verify_step(data, step['label'], live.operator, reserved, manifest['call_package_id'], wanted_call[0] if is_extension else None, expected_blob)
                live.evidence['pending_digest'] = data['digest']; checkpoint()
                return toolchain.execute_exact(live.executor(), live.signer(None, live.operator), tx, data['digest'])
            result = live.execute(step['label'], submit, gas=30_000_000, require_full=True)
            live.evidence.pop('pending_digest', None); checkpoint()
            node.stdin.write(json.dumps({'digest': result['digest']}) + '\n'); node.stdin.flush()
        node.stdin.close()
        if node.wait(timeout=120) or 'release_storage' not in live.evidence:
            raise RuntimeError('Acceptance did not finish')
    finally:
        if node.poll() is None:
            node.kill(); node.wait()
        live.finish(evidence)


if __name__ == '__main__':
    run(*sys.argv[1:])
