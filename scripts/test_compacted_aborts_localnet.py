#!/usr/bin/env python3
"""Real float-cap and compacted-base receipt refusals on a disposable local chain.

Run in a network-none dev container:
  python3 scripts/test_compacted_aborts_localnet.py --evidence /tmp/aborts.json
The harness creates and destroys the node and wallet; only effects are retained.
"""
import argparse
import secrets
import sys
import tempfile
from pathlib import Path
from live_common import Live, CLOCK, key_wallet
from abort_site import abort_site
from live_fleet_budget import DEPOSIT, FLOAT, ALLOWANCE, RESERVE, DRAW

def probe(args):
    if not Path('/.dockerenv').exists() or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
        raise RuntimeError('requires a disposable network-none container')
    live = Live(args.wallet, 'localnet', 3000000000)
    live.preflight()
    with tempfile.TemporaryDirectory() as d:
        r = live.publish('publish compacted base', Path('contracts'), Path(d) / 'pub.toml')
        package = live.published(r)
        registry = live.created(r, '::device_registry::DeviceRegistry')[0]
        call = lambda module, fn: f'{package}::{module}::{fn}'
        (config, address) = key_wallet(live, secrets.token_hex(32), Path(d), 'probe')
        r = live.transaction('register and activate device', '--move-call', call('device_registry', 'register_device'), f'@{registry}', '"float probe"', f'@{address}', '"aarch64"', CLOCK, '--assign', 'device', '--move-call', call('device_cap', 'issue_cap'), 'device', '--move-call', call('device_registry', 'activate_device'), f'@{registry}', 'device', '--assign', 'owner', '--transfer-objects', '[owner]', f'@{live.operator}', budget=40000000)
        cap = live.created(r, '::device_cap::DeviceCap')[0]
        owner = live.created(r, '::device_registry::DeviceOwnerCap')[0]
        device_id = live.created(r, '::device_registry::Device')[0]
        r = live.transaction('create float budget', '--split-coins', 'gas', f'[{DEPOSIT}u64]', '--assign', 'deposit', '--move-call', call('fleet_budget', 'new_limits'), f'{FLOAT}u64', '3600000u64', f'{ALLOWANCE}u64', '3u64', '0u64', '--assign', 'limits', '--move-call', call('fleet_budget', 'create'), f'@{registry}', 'deposit', 'limits', f'{DEPOSIT // 3}u64', '--assign', 'cap', '--transfer-objects', '[cap]', f'@{live.operator}', budget=30000000, funding=DEPOSIT)
        budget = live.created(r, '::fleet_budget::FleetBudget')[0]
        budget_cap = live.created(r, '::fleet_budget::BudgetOwnerCap')[0]
        r = live.transaction('enroll float probe', '--move-call', call('fleet_budget', 'enroll_device'), f'@{budget}', f'@{budget_cap}', f'@{registry}', f'@{owner}', f'{FLOAT}u64', f'{RESERVE}u64', CLOCK, budget=20000000)
        account = live.created(r, '::fleet_budget::GasAccount')[0]
        child = live.child(config, FLOAT + RESERVE)
        draw_args = lambda amount: ('--move-call', call('fleet_budget', 'draw_gas'), f'@{budget}', f'@{account}', f'@{registry}', f'@{cap}', f'{amount}u64', CLOCK)
        live.transaction('draw below cap', *draw_args(DRAW), budget=10000000, sender=child)
        live.attack('draw above the float cap', *draw_args(FLOAT + 1), module='fleet_budget', function=abort_site('contracts', 'fleet_budget', 'draw_gas', 8), code=8, budget=10000000, sender=child)
        r = live.transaction('create inline base record', '--move-call', call('deployment_manager', 'create_deployment_verified'), '"probe"', '"1"', '"blob"', '"' + 'ab' * 32 + '"', '"' + 'cd' * 32 + '"', 'vector[]', f'vector[@{device_id}]', CLOCK, budget=30000000)
        record = live.created(r, '::deployment_manager::DeploymentRecord')[0]
        live.transaction('bump record to sharded generation', '--move-call', call('deployment_manager', 'begin_sharded_receipts'), f'@{record}', budget=100000000)
        live.attack('compacted base inline writer refuses future receipts', '--move-call', call('deployment_manager', 'migrate'), f'@{record}', module='deployment_manager', function=abort_site('contracts', 'deployment_manager', 'receipts_mut', 5), code=5, budget=10000000)
        live.check('float-cap refusal and compacted-base future-receipt lockout match exact module/function/code')
        live.evidence['success'] = True
        live.finish(Path(args.evidence))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['wallet', 'network', 'base', 'state']:
        parser.add_argument('--' + name)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    if args.wallet:
        probe(args)
    else:
        from test_security_policy_localnet import run
        from types import SimpleNamespace
        run(SimpleNamespace(base='contracts', runner=str(Path(__file__).resolve()), suite='live_security_policy', evidence=args.evidence, actions=None, multisig=None, only_multisig=False))
if __name__ == '__main__':
    main()
