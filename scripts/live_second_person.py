#!/usr/bin/env python3
"""SF-199 existing-object upgrade and second-person effects, localnet/testnet.

Only dedicated testnet wallets or disposable localnet. Private hash-keyed package
fixtures journal ambiguous publications; no identical republish on reruns.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import deployment_manifest as dm
import sui_toolchain as tc
from live_common import Live, CLOCK
from live_package_upgrade import contracts_digest


def record_effects_gas(live):
    """Read every digest independently; preserve signed fees, separate from the
    non-replenishing ledger's intentionally nonnegative charged gas."""
    for entry in live.evidence['transactions']:
        if not entry.get('digest'): continue
        result = live.executed(entry['digest'])
        assert result and result['effects']['status']['status'] == entry['status'], 'independent effects mismatch'
        gas = result['effects']['gasUsed']
        entry['effects_gas'] = {key: int(gas[key]) for key in ('computationCost', 'storageCost', 'storageRebate')}
        entry['net_gas_mist'] = int(gas['computationCost']) + int(gas['storageCost']) - int(gas['storageRebate'])
    live.evidence['effects_net_gas_mist'] = sum(entry.get('net_gas_mist', 0) for entry in live.evidence['transactions'])


def run(args):
    live = Live(args.wallet, args.network, args.max_spend_mist)
    try:
        live.preflight(auto_fund=args.auto_fund)
    except Exception:
        live.evidence['success'] = False
        Path(args.evidence).write_text(json.dumps(live.evidence, indent=2) + '\n')
        raise
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    pubfile = state_path.parent / 'publication' / 'fixture.toml'
    pubfile.parent.mkdir(exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    key = hashlib.sha256(json.dumps([live.chain, live.operator, contracts_digest(Path(args.base)), contracts_digest(Path('contracts')), args.fresh]).encode()).hexdigest()
    live.evidence.update(success=False, source_contracts_digest=contracts_digest(Path('contracts')), base_contracts_digest=contracts_digest(Path(args.base)))

    def save():
        state_path.write_text(json.dumps(state, indent=2) + '\n'); state_path.chmod(0o600)

    def checkpoint(identity):
        state['pending']['verified_transaction'] = identity; save()

    def tx(label, *ptb, sender=None, funding=0, budget=20_000_000):
        return live.transaction(label, *ptb, budget=min(budget, 5_000_000) if sender else budget, sender=sender, funding=funding)

    try:
        if state.get('pending'):
            raise RuntimeError('uncertain fixture mutation: reconcile recorded digest before retrying')
        fixture = state.setdefault('fixtures', {}).get(key)
        if not fixture:
            state['pending'] = {'operation': 'publish fresh head' if args.fresh else 'publish base', 'key': key}; save()
            result = live.publish('publish standalone second-person fixture' if args.fresh else 'publish framework base',
                                  Path('contracts') if args.fresh else Path(args.base), pubfile, persist=checkpoint)
            base, cap = live.published(result), live.created(result, '::package::UpgradeCap')[0]
            registry = live.created(result, '::device_registry::DeviceRegistry')[0]
            state['pending'].update(base=base, cap=cap, registry=registry); save()
            result = tx('create standalone organization' if args.fresh else 'create organization before upgrade', '--move-call', f'{base}::customer::create', f'@{registry}', '\"SF199 disposable fixture\"', budget=60_000_000)
            customer, fleet = live.created(result, '::customer::Customer')[0], live.created(result, '::customer::Fleet')[0]
            tx('initialize fixture organization', '--move-call', f'{base}::customer::initialize_security', f'@{customer}')
            if args.fresh:
                fixture = dict(base=base, head=base, cap=cap, registry=registry, customer=customer, fleet=fleet, version=1, mode='fresh')
            else:
                state['pending'].update(customer=customer, fleet=fleet, operation='upgrade head'); save()
                tc.write_pubfile(pubfile, live.chain, Path('contracts'), base, cap)
                result = live.publish('upgrade second-person implementation', Path('contracts'), pubfile, {'cap': cap, 'package': base}, persist=checkpoint)
                fixture = dict(base=base, head=live.published(result), cap=cap, registry=registry, customer=customer, fleet=fleet, version=2, mode='upgrade')
            state['fixtures'][key] = fixture; del state['pending']; save()
        base, head, c, fleet = (fixture[k] for k in ('base', 'head', 'customer', 'fleet'))
        cap = live.object(fixture['cap'])
        assert dm.object_id(cap['owner']['AddressOwner']) == live.operator
        assert dm.object_id(cap['content']['package']) == head and int(cap['content']['version']) == fixture['version']
        facts = dm.chain_facts(live.rpc_url, head)
        origins = dm.origins(facts['objects'][head])
        assert origins['customer::Customer'] == base and origins['security_policy::Action'] == base
        assert origins['customer::SecondPersonConfiguration'] == head
        assert int(live.object(c)['content']['version']) in (197, 199)
        assert live.object(c)['content']['registry_id'] == fixture['registry']
        live.evidence.update(fixture=fixture, type_origins=origins)
        manifest = dm.resolve(argparse.Namespace(package=head, rpc=live.rpc_url, upgrade_cap=fixture['cap'], registry=fixture['registry'],
            chain_identifier=live.chain, network=live.network, publisher='http://127.0.0.1:1', aggregator='http://127.0.0.1:1'))
        manifest_path = state_path.with_suffix('.manifest.json')
        manifest_path.write_text(json.dumps(manifest))

        def console_queue(expected):
            subprocess.run(['node', 'scripts/check_second_person_requests.cjs', str(manifest_path), c, expected], check=True)

        if args.resume_unprotected:
            ids = json.loads(subprocess.check_output(['node', 'scripts/check_second_person_requests.cjs', str(manifest_path), c, 'unprotected-ids'], text=True))
            for action in ids:
                tx('cancel prior unprotected fixture request', '--move-call', f'{head}::customer::cancel_disclosed_security_action', f'@{c}', f'@{action}')
        # Reuse the same funded signing identity; its keystore remains private,
        # never an artifact. Funding is charged before transfer, not replenished.
        approver = state_path.parent / ('sf199-approver-' + key[:12])
        approver.mkdir(mode=0o700, exist_ok=True)
        config = approver / 'client.yaml'
        if not config.exists():
            keystore = approver / 'sui.keystore'
            keystore.write_text('[]'); keystore.chmod(0o600)
            config.write_text(f'keystore:\n  File: {keystore}\nenvs:\n- alias: live\n  rpc: {live.rpc_url}\nactive_env: live\nactive_address: null\n')
            config.chmod(0o600)
            generated = subprocess.run([live.network_sui, 'client', '--client.config', str(config), 'new-address', 'ed25519', '--json'], capture_output=True, text=True)
            if generated.returncode: raise RuntimeError('private approver generation failed; output withheld')
        address = dm.object_id(live.client('active-address', config=config, json_output=False).stdout.strip().split()[-1])
        live.client('switch', '--address', address, config=config)
        funding = 20_000_000
        tx('fund bounded disposable approver', '--split-coins', 'gas', f'[{funding}]', '--assign', 'fund', '--transfer-objects', '[fund]', f'@{address}', funding=funding)
        child = live.child(config, funding)
        tx('add independent administrator', '--move-call', f'{head}::customer::set_credential', f'@{c}', f'@{address}', 'true', 'true')
        expiry = int(live.object('0x6')['content']['timestamp_ms']) + 600_000
        raw = subprocess.check_output(['node', 'scripts/second_person_arguments.cjs', live.rpc_url, c, fleet, origins['customer::Customer'], '--enroll'], text=True)
        vector = 'vector[' + ','.join(str(b) + 'u8' for b in json.loads(raw)) + ']'
        result = tx('propose verifier enrollment', '--move-call', f'{head}::customer::propose_approval', f'@{c}', vector, f'{expiry}u64', CLOCK)
        enrollment = live.created(result, '::security_policy::Action')[0]
        assert int(live.object(c)['content']['version']) == 199
        if not args.fresh:
            live.attack('pre-enrollment old cancel cannot orphan index', '--move-call', f'{base}::customer::cancel_security_action', f'@{c}', f'@{enrollment}', module='customer', function='assert_member', code=0, budget=20_000_000)
            live.attack('pre-enrollment old execution refused', '--move-call', f'{base}::customer::set_security_secured', f'@{c}', 'vector[0u8,0u8,0u8,0u8,0u8,0u8,0u8]', f'@{enrollment}', CLOCK, module='customer', function='assert_member', code=0, budget=20_000_000)
        console_queue(enrollment)
        tx('independent signed enrollment approval', '--move-call', f'{head}::customer::approve_action', f'@{c}', f'@{enrollment}', CLOCK, sender=child)
        tx('configure verifier and enable optional administration protection', '--move-call', f'{head}::customer::configure_second_person', f'@{c}', f'@{enrollment}', CLOCK,
           '--move-call', f'{head}::customer::set_security', f'@{c}', 'vector[0u8,0u8,0u8,0u8,4u8,0u8,4u8]')
        assert int(live.object(c)['content']['version']) == 199
        live.attack('direct current action refused', '--move-call', f'{head}::customer::rename_fleet', f'@{c}', f'@{fleet}', '"bypass"', module='security_policy', function='p0', code=1, budget=20_000_000)
        if not args.fresh:
            live.attack('old-package bypass refused', '--move-call', f'{base}::customer::rename_fleet', f'@{c}', f'@{fleet}', '"old bypass"', module='customer', function='assert_member', code=0, budget=20_000_000)

        def proposal(name=None):
            command = ['node', 'scripts/second_person_arguments.cjs', live.rpc_url, c, fleet, origins['customer::Customer']]
            if name is not None: command.append(name)
            arguments = json.loads(subprocess.check_output(command, text=True))
            vector = 'vector[' + ','.join(str(b) + 'u8' for b in arguments) + ']'
            result = tx('propose disclosed ' + ('policy recovery' if name is None else name), '--move-call', f'{head}::customer::propose_approval', f'@{c}', vector, f'{expiry}u64', CLOCK)
            action = live.created(result, '::security_policy::Action')[0]
            console_queue(action)
            return action

        action = proposal('SF199 independently approved fleet')
        live.attack('self approval refused', '--move-call', f'{head}::customer::approve_action', f'@{c}', f'@{action}', CLOCK, module='customer', function='approve_action', code=9, budget=20_000_000)
        if not args.fresh:
            live.attack('old immutable cancel cannot orphan index', '--move-call', f'{base}::customer::cancel_security_action', f'@{c}', f'@{action}', module='customer', function='assert_member', code=0, budget=20_000_000)
        tx('different current credential approves exact action', '--move-call', f'{head}::customer::approve_action', f'@{c}', f'@{action}', CLOCK, sender=child)
        live.attack('approved argument substitution refused', '--move-call', f'{head}::customer::rename_fleet_secured', f'@{c}', f'@{fleet}', '"substitution"', f'@{action}', CLOCK, module='security_policy', function='begin_scoped_bytes', code=0, budget=20_000_000)
        tx('execute approved exact action', '--move-call', f'{head}::customer::rename_fleet_secured', f'@{c}', f'@{fleet}', '"SF199 independently approved fleet"', f'@{action}', CLOCK)
        assert live.object(fleet)['content']['name'] == 'SF199 independently approved fleet'
        action = proposal('SF199 rejected action')
        tx('second administrator rejects', '--move-call', f'{head}::customer::reject_action', f'@{c}', f'@{action}', sender=child)
        action = proposal()
        tx('independent recovery approval', '--move-call', f'{head}::customer::approve_action', f'@{c}', f'@{action}', CLOCK, sender=child)
        tx('recover by approved policy weakening', '--move-call', f'{head}::customer::set_security_secured', f'@{c}', 'vector[0u8,0u8,0u8,0u8,0u8,0u8,0u8]', f'@{action}', CLOCK)
        tx('all-off administration works after recovery', '--move-call', f'{head}::customer::rename_fleet', f'@{c}', f'@{fleet}', '"SF199 recovered fixture"',
           '--move-call', f'{head}::customer::set_credential', f'@{c}', f'@{address}', 'false', 'false')
        # Return the remaining current-run funded coin. Rebates do not
        # replenish either ledger; the child ledger caps this transaction.
        live.transaction('sweep disposable approval gas', '--transfer-objects', '[gas]', f'@{live.operator}', budget=3_000_000, sender=child)
        console_queue('empty')
        record_effects_gas(live)
        live.check('standalone current-package setup, distinct approval, exact execution, rejection and factor-authorized recovery' if args.fresh else 'base/head existing-object compatibility, old/current bypass refusal, distinct approval, exact execution, rejection and factor-authorized recovery')
        if args.fresh: live.evidence['limits'] = ['Fresh testnet fixture does not establish old-package/public upgrade compatibility; see the separate isolated base-to-head receipt and mandatory automatic CI/testnet gates.']
        live.evidence['success'] = True
    finally:
        live.finish(Path(args.evidence))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--network', choices=['localnet', 'testnet'], required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--max-spend-mist', type=int, default=1_800_000_000)
    parser.add_argument('--resume-unprotected', action='store_true', help='cancel only this verified fixture queue after checking all-off policy; never bypass recovery factors')
    parser.add_argument('--fresh', action='store_true', help='standalone current-package mechanism test; upgrade/old-package acceptance must remain separately recorded')
    parser.add_argument('--auto-fund', action='store_true')
    run(parser.parse_args())
