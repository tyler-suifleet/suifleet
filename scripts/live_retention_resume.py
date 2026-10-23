#!/usr/bin/env python3
"""Reconcile one SF092 live fixture's durable renewal after a stopped runner.

Requires the original private runtime directory and public failed-run evidence.
Uses only the original signed bytes, the dedicated agent and existing fixture;
never publishes, deletes an intent, creates a replacement transaction or faucets.
"""
import argparse
import json
import os
from pathlib import Path
import time

import deployment_manifest as dm
from live_common import Live, plain, wait_for
from live_fleet_budget import SYSTEM, WALRUS_PACKAGE, event, net_gas
from retention_worker import Worker


def run(args):
    assert os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'), 'shared mutation lock required'
    os.umask(0o077)
    original = json.loads(Path(args.original_evidence).read_text())
    directory = Path(args.state_dir).resolve()
    root = directory.parent
    live = Live(args.wallet, 'testnet', args.max_spend_mist,
                build_sui=args.build_sui, network_sui=args.network_sui)
    live.preflight()
    assert original['chain'] == live.chain and original['operator'] == live.operator
    assert original['rpc'] == live.rpc_url
    fixture = original['fixture']
    assert dm.object_id(live.owner(fixture['storage_upgrade_cap'])) == live.operator
    assert dm.object_id(live.owner(original['budget_cap'])) == live.operator
    package, storage = fixture['storage_package'], original['storage']['storage']
    config = {'chain': live.chain, 'network': 'testnet', 'rpc': live.rpc_url,
              'storage': storage, 'package': package, 'storage_origin': package,
              'retention_origin': package, 'executor': original['device_address'],
              'executor_key_file': str(root / 'device/test.key'), 'system': SYSTEM,
              'walrus_package': WALRUS_PACKAGE, 'gas_budget': '10000000', 'spend_ceiling': '10000000'}
    worker = Worker(config, directory)
    expected = {key: value['digest'] for key, value in worker.state['pending'].items()}
    assert len(expected) == 1, 'exactly one original pending renewal is required'
    balance = live.balance(original['device_address'])
    live.ledger.grant(balance)  # Charge all existing executor gas, without replenishment.
    device = live.child(root / 'device/client.yaml', balance)
    live.evidence.update(original_head=original['head'], fixture=fixture, storage=storage,
                         original_pending=expected, executor=original['device_address'])
    try:
        def transport(action, value=None):
            if action != 'submit':
                return worker.call(action, value)
            assert value['digest'] in expected.values(), 'replacement intent refused'
            def submit(reserved):
                assert reserved == 10_000_000, 'insufficient reserved executor allowance'
                worker.call('submit', value)  # Actual transport verifies and submits original bytes.
                found = {}
                wait_for(lambda: found.update(live.executed(value['digest'], device[0])) or bool(found),
                         'original renewal effects not readable', timeout=60)
                return found
            result = live.execute('resume original restricted renewal', submit, gas=10_000_000, sender=device)
            renewal = event(result, '::fleet_storage::RenewalCompleted')
            live.evidence['retention_renewal'] = dict(renewal, digest=result['digest'], net_gas_mist=net_gas(result))
            return {'status': 'submitted'}
        worker.transport = transport
        # Honor existing backoff, never clear/reset the attempt or spend journal.
        due = max((value['next'] for value in worker.state['attempts'].values()), default=0)
        assert due <= time.time() + 120, 'inspect an unexpectedly distant retry deadline'
        while time.time() < due:
            time.sleep(min(1, due - time.time()))
        worker.tick()
        def confirmed():
            worker.tick()
            return all(worker.state['last_success'].get(key, {}).get('digest') == digest for key, digest in expected.items())
        wait_for(confirmed, 'original effects and new expiry not reconciled', timeout=60)
        actual = {key: int(plain(live.object(key)['content'])['storage']['end_epoch']) for key in expected}
        assert all(actual[key] == worker.state['last_success'][key]['end'] for key in expected)
        worker.lock.close()
        worker = Worker(config, directory)
        worker.tick()
        assert not worker.state['pending']
        assert len(live.evidence['transactions']) == 1, 'restart issued another transaction'
        live.evidence.update(actual_expiry=actual, restart_duplicate_transactions=0, passed=True)
        live.check('original signed renewal confirmed; actual new expiry and restart deduplication verified')
    finally:
        worker.lock.close()
        live.finish(args.evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-evidence', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--build-sui', required=True)
    parser.add_argument('--network-sui', required=True)
    parser.add_argument('--max-spend-mist', type=int, default=100_000_000)
    parser.add_argument('--evidence', required=True)
    run(parser.parse_args())
