#!/usr/bin/env python3
"""Effects-checked SF-200 setup, early refusal, cancellation and delayed recovery.

Public mode reuses bytecode-keyed package fixtures. --base additionally tests an
existing v197 object upgraded on a disposable network-none local chain. Uncertain
publication remains reserved in the private state and is never blindly retried.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import deployment_manifest as dm
import sui_toolchain as tc
from live_common import Live, CLOCK
from live_package_upgrade import contracts_digest


def policy_failure_function(package):
    """Pin the exact abort site to the compiled source, including older bases."""
    source = (Path(package) / 'sources/security_policy.move').read_text()
    if 'p0(p.transaction == *ctx.digest()); p0((p.attested & needed) == needed);' in source:
        if 'fun p0(condition: bool)' not in source or 'if (!condition) abort EFactor' not in source:
            raise ValueError('unrecognized factored policy guard')
        return 'p0'
    if 'assert!(needed == 0 || (p.transaction == *ctx.digest() && (p.attested & needed) == needed), EFactor);' in source:
        return 'check'
    raise ValueError('unrecognized policy authority guard')


def record_net_gas(live):
    """Record signed net gas separately from the non-replenishing ledger charge."""
    for entry in live.evidence['transactions']:
        if not entry.get('digest'):
            continue
        result = live.executed(entry['digest'])
        assert result['effects']['status']['status'] == entry['status']
        used = result['effects']['gasUsed']
        net = int(used['computationCost']) + int(used['storageCost']) - int(used['storageRebate'])
        assert max(0, net) == entry['gas_mist']
        entry.update(net_gas_mist=net, gas_costs=used)


def run(args):
    if args.network == 'testnet' and not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('requires network:suifleet-testnet lock')
    head_failure = policy_failure_function('contracts')
    base_failure = policy_failure_function(args.base) if args.base else None
    live = Live(args.wallet, args.network, args.max_spend_mist)
    live.preflight(auto_fund=args.auto_fund)
    path = Path(args.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text()) if path.exists() else {}
    head_hash = contracts_digest(Path('contracts'))
    base_hash = contracts_digest(Path(args.base)) if args.base else None
    key = hashlib.sha256(json.dumps([live.chain, live.operator, base_hash, head_hash]).encode()).hexdigest()
    def save():
        path.write_text(json.dumps(state, indent=2)+'\n'); path.chmod(0o600)
    def checkpoint(identity):
        state['pending']['verified_transaction'] = identity; save()
    def tx(label, *ptb):
        return live.transaction(label, *ptb, budget=min(100_000_000, live.ledger.remaining))
    def vector(values, kind='u8'):
        return 'vector['+','.join(str(v)+kind for v in values)+']'
    live.evidence['success'] = False
    try:
        if state.get('pending'):
            raise RuntimeError('uncertain publication: reconcile its recorded exact digest before retrying')
        fixture = state.setdefault('fixtures', {}).get(key)
        if not fixture:
            state['pending'] = {'key': key, 'operation': 'publish fixture'}; save()
            result = live.publish('publish isolated delay fixture', Path(args.base or 'contracts'), path.with_suffix('.toml'), persist=checkpoint)
            base = live.published(result)
            cap = live.created(result, '::package::UpgradeCap')[0]
            registry = live.created(result, '::device_registry::DeviceRegistry')[0]
            state['pending'].update(base=base, cap=cap, registry=registry); save()
            head = base
            if args.base:
                # Existing v197 tenant and action survive a compatible package upgrade.
                result = tx('create v197 customer before upgrade', '--move-call', f'{base}::customer::create', '@'+registry, '"SF200 pre-upgrade"')
                prior_customer = live.created(result, '::customer::Customer')[0]
                state['pending']['prior_customer'] = prior_customer; save()
                tc.write_pubfile(path.with_suffix('.toml'), live.chain, Path('contracts'), base, cap)
                result = live.publish('compatible delay upgrade', Path('contracts'), path.with_suffix('.toml'), {'package':base,'cap':cap}, persist=checkpoint)
                head = live.published(result)
            fixture = dict(base=base, head=head, cap=cap, registry=registry, version=2 if args.base else 1,
                           prior_customer=state['pending'].get('prior_customer'), base_hash=base_hash, head_hash=head_hash)
            state['fixtures'][key] = fixture; del state['pending']; save()
        base, head, registry = fixture['base'], fixture['head'], fixture['registry']
        cap = live.object(fixture['cap'])
        assert dm.object_id(cap['owner']['AddressOwner']) == live.operator
        assert dm.object_id(cap['content']['package']) == head and int(cap['content']['version']) == fixture['version']
        origins = dm.origins(dm.chain_facts(live.rpc_url, head)['objects'][head])
        assert origins['customer::Customer'] == base
        assert origins['security_policy::Action'] == base
        assert origins['security_policy::QueuedAt'] == head
        assert 'Shared' in live.object(registry)['owner']
        live.evidence.update(fixture=fixture, type_origins=origins)
        if args.resume_receipt:
            previous_path = Path(args.resume_receipt)
            previous = json.loads(previous_path.read_text())
            assert not previous['success'] and previous['chain'] == live.chain and previous['operator'] == live.operator
            assert previous['fixture']['head'] == head and previous['fixture']['head_hash'] == head_hash
            assert live.max_spend <= previous['max_spend_mist'] - previous['allowance_used_mist']
            created = next(t for t in previous['transactions'] if t['label'] == 'create test organization with all factors off')
            prior_customer = live.created(live.executed(created['digest']), '::customer::Customer')[0]
            assert prior_customer == previous['customer']
            customer_object = live.object(prior_customer)
            assert customer_object['content']['registry_id'] == registry and 'Shared' in customer_object['owner']
            queued = next(t for t in previous['transactions'] if t['label'] == 'queue cancelable recovery')
            retained = live.created(live.executed(queued['digest']), '::security_policy::Action')[0]
            retained_object = live.object(retained)
            assert retained_object['content']['customer_id'] == prior_customer and retained_object['content']['proposer'] == live.operator
            fixture.update(customer=prior_customer, protected=True, retained_actions=[retained]); save()
            live.evidence['continuation'] = dict(receipt_sha256=hashlib.sha256(previous_path.read_bytes()).hexdigest(),
                previous_allowance_used_mist=previous['allowance_used_mist'], original_ceiling_mist=previous['max_spend_mist'])
        if fixture.get('customer') or fixture.get('prior_customer'):
            customer = fixture.get('customer') or fixture['prior_customer']
        else:
            result = tx('create test organization with all factors off', '--move-call', f'{head}::customer::create', '@'+registry, '"SF200 disposable organization"')
            customer = live.created(result, '::customer::Customer')[0]
            fixture['customer'] = customer; save()
        # All tests operate only on this explicitly selected run fixture.
        live.evidence['customer'] = customer
        if not fixture.get('protected'):
            tx('configure seven 60-second cancellation windows', '--move-call', f'{head}::customer::configure_security_delays', '@'+customer, vector([60000]*7,'u64'))
            tx('enable delay for all action classes', '--move-call', f'{head}::customer::set_security', '@'+customer, vector([8]*7))
        fixture['protected'] = True; save()
        for retained in list(fixture.get('retained_actions', [])):
            tx('cancel retained nonce from interrupted acceptance', '--move-call',f'{head}::customer::cancel_security_action','@'+customer,'@'+retained)
            fixture['retained_actions'].remove(retained); save()
        if args.base:
            live.attack('old package direct policy weakening refused', '--move-call', f'{base}::customer::set_security', '@'+customer, vector([0]*7), module='security_policy', function=base_failure, code=1, budget=20_000_000)
        live.attack('direct policy weakening refused', '--move-call', f'{head}::customer::set_security', '@'+customer, vector([0]*7), module='security_policy', function=head_failure, code=1, budget=20_000_000)
        def queue(label):
            raw = subprocess.check_output(['node','scripts/security_delay_fixture.cjs',live.rpc_url,customer,origins['customer::Customer'],'set_security',json.dumps([0]*7)],text=True)
            expiry = int(live.object('0x6')['content']['timestamp_ms']) + 600000
            result = tx(label, '--move-call',f'{head}::customer::queue_security_action','@'+customer, vector(json.loads(raw)),str(expiry)+'u64',CLOCK)
            action_id = live.created(result,'::security_policy::Action')[0]
            fixture.setdefault('retained_actions', []).append(action_id); save()
            actual = live.object(action_id)['content']['intent']
            subprocess.run(['node','scripts/check_security_delay_intent.cjs'], input=json.dumps(dict(chain=live.chain,head=head,origins=origins,customer=customer,arguments=json.loads(raw),intent=actual)),text=True,check=True)
            return action_id
        canceled = queue('queue cancelable recovery')
        result = tx('cancel immediately without factors','--move-call',f'{head}::customer::cancel_security_action','@'+customer,'@'+canceled)
        assert any(c['type']=='deleted' and c['objectId']==canceled for c in result['objectChanges'])
        fixture['retained_actions'].remove(canceled); save()
        refused = live.client('ptb','--move-call',f'{head}::customer::set_security_secured','@'+customer,vector([0]*7),'@'+canceled,CLOCK,'--gas-budget','20000000','--serialize-unsigned-transaction',check=False,json_output=False)
        assert refused.returncode != 0 and canceled in refused.stderr+refused.stdout
        live.check('cancellation deletes the nonce; canceled execution is refused before signing')
        legacy_action = None
        if args.base:
            raw = subprocess.check_output(['node','scripts/security_delay_fixture.cjs',live.rpc_url,customer,origins['customer::Customer'],'set_security',json.dumps([0]*7)],text=True)
            expiry = int(live.object('0x6')['content']['timestamp_ms']) + 600000
            result = tx('queue through old package without trusted timestamp', '--move-call',f'{base}::security_policy::action_intent','@'+customer,vector(json.loads(raw)), '--assign','intent',
                        '--move-call', f'{base}::customer::prepare_security_action','@'+customer,'intent',str(expiry)+'u64',CLOCK)
            legacy_action = live.created(result,'::security_policy::Action')[0]
        action = queue('queue currently authorized recovery')
        live.attack('execution before chain-clock maturity refused','--move-call',f'{head}::customer::set_security_secured','@'+customer,vector([0]*7),'@'+action,CLOCK,module='security_policy',function='begin_scoped_bytes',code=3,budget=20_000_000)
        # Early failure rolls back and retains the original nonce/timestamp.
        assert live.object(action)['content']['customer_id'] == customer
        start = time.monotonic()
        # Advance past the minimum window after the confirmed early refusal.
        # Execution still checks the stored timestamp against the chain Clock.
        while time.monotonic()-start < 61:
            time.sleep(1)
        if legacy_action:
            live.attack('old queue still refused after maturity: missing trusted timestamp','--move-call',f'{head}::customer::set_security_secured','@'+customer,vector([0]*7),'@'+legacy_action,CLOCK,module='security_policy',function='begin_scoped_bytes',code=3,budget=20_000_000)
            tx('cancel legacy queue safely','--move-call',f'{head}::customer::cancel_security_action','@'+customer,'@'+legacy_action)
        result = tx('mature recovery disables delay under the old policy','--move-call',f'{head}::customer::set_security_secured','@'+customer,vector([0]*7),'@'+action,CLOCK)
        assert any(c['type']=='deleted' and c['objectId']==action for c in result['objectChanges'])
        fixture['retained_actions'].remove(action); fixture['protected'] = False; save()
        tx('all-off administration restored after recovery','--move-call',f'{head}::customer::set_security','@'+customer,vector([0]*7))
        live.check('immutable queue creation, early execution abort, mature execution and explicit delayed recovery effects')
        live.evidence['success'] = True
    finally:
        try:
            record_net_gas(live)
        except Exception:
            live.evidence['success'] = False
            raise
        finally:
            live.finish(Path(args.evidence))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet',required=True);p.add_argument('--network',choices=['testnet','localnet'],required=True)
    p.add_argument('--base');p.add_argument('--state',required=True);p.add_argument('--evidence',required=True)
    p.add_argument('--resume-receipt', help='reconcile the exact failed hash-check fixture without republishing or replenishing its allowance')
    p.add_argument('--auto-fund',action='store_true');p.add_argument('--max-spend-mist',type=int,default=1_600_000_000)
    run(p.parse_args())
