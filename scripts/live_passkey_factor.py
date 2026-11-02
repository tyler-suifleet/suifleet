#!/usr/bin/env python3
"""Draft isolated-node native passkey sponsorship acceptance.

Reuses exact source-pair fixtures, stops on uncertainty, and charges publication,
failures and passkey gas grants against one non-replenishing run allowance.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from live_common import Live
from live_package_upgrade import contracts_digest
import deployment_manifest as dm
import sui_toolchain as tc


def exercise(live, f, state_path, state, save):
    def fixture(**kwargs):
        result = subprocess.run(['node','scripts/passkey_transaction.cjs'], input=json.dumps(dict(keys=str(state_path.with_suffix('.keys.json')), network=live.network, endpoint=live.rpc_url, **kwargs)), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    def tx(label, **kwargs):
        live.assert_lock()
        def submit(budget):
            prepared = fixture(budget=budget, **kwargs)
            data = live.decode(prepared['bytes'])
            assert data['digest'] == prepared['digest']
            assert int(data['gas_budget']) == budget
            expected_owner = keys[kwargs['sponsor']]['address'] if 'sponsor' in kwargs else kwargs['sender']
            assert tc.normal(data['gas_owner']) == tc.normal(expected_owner)
            assert tc.normal(data['sender']) == tc.normal(kwargs['sender'])
            # The locally constructed fixture is the reviewed operation builder;
            # wallet authority signs only its verified bytes, never a digest alone.
            state['pending']={'operation':label,'digest':data['digest']};save()
            signatures = [prepared['signature']] if kwargs['operation'] == 'proof' else [live.signer(None, live.operator)(prepared['bytes'])] + ([prepared['signature']] if prepared['signature'] else [])
            result = tc.bound(live.executor()('execute-signed-tx','--tx-bytes',prepared['bytes'],*[arg for signature in signatures for arg in ('--signatures', signature)]), data['digest'])
            del state['pending'];save()
            return result
        return live.ledger.execute(live.executor(), label, submit, gas=20_000_000, expect=kwargs.pop('expect','success'), require_full=True)
    keys=fixture(operation='info')
    for i,key in enumerate(keys):
        # Funding is charged as a grant; subsequent passkey-paid gas cannot
        # enlarge the allowance. Reused signing identities retain their gas.
        total=live.balance(key['address'])
        if total<15_000_000:
            live.transaction(f'fund passkey {i}','--split-coins','gas','[30000000]','--assign','funding','--transfer-objects','[funding]',f'@{key["address"]}',budget=20_000_000,funding=30_000_000)
        result=tx(f'native possession proof {i}',operation='proof',key=i,package=f['head'],customer=f['customer'],administrator=live.operator,sender=key['address'],expires=int(live.object('0x6')['content']['timestamp_ms'])+300000)
        proof=live.created(result,'::passkey_factor::Enrollment')[0]
        live.transaction(f'wallet enrolls passkey {i}','--move-call',f'{f["head"]}::passkey_factor::enroll',f'@{f["customer"]}',f'@{proof}','@0x6',budget=20_000_000)
    common=dict(package=f['head'],customer=f['customer'],sender=live.operator)
    tx('enable native passkey protection',operation='policy',mask=2,**common)
    tx('wallet plus passkey allows action',operation='rename',fleet=f['fleet'],name='Native co-signed action',sponsor=0,**common)
    tx('wallet alone refused on chain',operation='rename',fleet=f['fleet'],name='must fail',expect='failure',**common)
    tx('old package cannot bypass protection',operation='rename',fleet=f['fleet'],name='must fail old',expect='failure',sponsor=0,**{**common,'package':f['base']})
    tx('backup passkey recovers lost primary',operation='remove',administrator=live.operator,key=0,sponsor=1,**common)
    tx('removed primary refused',operation='rename',fleet=f['fleet'],name='must fail removed',sponsor=0,expect='failure',**common)
    tx('surviving backup allows action',operation='rename',fleet=f['fleet'],name='Recovered with backup',sponsor=1,**common)
    tx('backup authorizes disable',operation='policy',mask=0,sponsor=1,**common)
    tx('wallet removes final key after disable',operation='remove',administrator=live.operator,key=1,**common)

def cached_acceptance(live, package_state, private=None):
    """Use the package gate's exact source fixture and existing security ledger.

    CI preserves these files only inside its authenticated encrypted state.
    No publication, new operator wallet, faucet or extra allowance is introduced.
    """
    from live_wallet_gate import head_fixture
    from live_package_upgrade import load_state, save_state
    cache = load_state(package_state, live.chain, live.operator)
    cached = head_fixture(cache, contracts_digest(Path('contracts')))
    if not cached:
        raise RuntimeError('native passkey acceptance requires the exact cached package gate fixture')
    head, base = cached['head_package'], cached['base_package']
    cap = live.object(cached['upgrade_cap'])
    assert dm.object_id(cap['owner']['AddressOwner']) == live.operator
    assert dm.object_id(cap['content']['package']) == head
    assert int(cap['content']['version']) == 2
    facts = dm.chain_facts(live.rpc_url, head)
    origins = dm.origins(facts['objects'][head])
    assert origins['passkey_factor::Enrollment'] == head
    assert 'Shared' in live.object(cached['registry'])['owner']
    private = Path(private) if private else live.wallet.parent / 'live-passkey-state.json'
    if private.resolve().is_relative_to(Path.cwd().resolve()):
        raise RuntimeError('private signing state must stay outside the checkout')
    state = json.loads(private.read_text()) if private.exists() else {}
    def save():
        save_state(private, state)
    if state.get('pending'):
        raise RuntimeError('uncertain native passkey mutation; reconcile exact digest before retry')
    identity = hashlib.sha256(json.dumps([live.chain, live.operator, base, head]).encode()).hexdigest()
    f = state.setdefault('fixtures', {}).get(identity)
    if not f:
        state['pending'] = {'operation': 'create cached passkey organization'}; save()
        result = live.transaction('create cached passkey organization', '--move-call',
            f'{head}::customer::create', '@' + cached['registry'],
            '"SF198 protected CI fixture"', budget=100_000_000)
        f = dict(base=base, head=head, customer=live.created(result, '::customer::Customer')[0],
                 fleet=live.created(result, '::customer::Fleet')[0])
        state['fixtures'][identity] = f
        del state['pending']; save()
    # Initializer is additive and idempotent; all protected cases share the
    # parent's strict ledger, including failures and full gas reservations.
    live.transaction('initialize cached passkey authority', '--move-call',
                     f'{head}::customer::initialize_security', '@' + f['customer'], budget=20_000_000)
    exercise(live, f, private, state, save)
    live.evidence['native_passkey'] = dict(fixture=f, type_origins=origins, success=True)


def run(a):
    if a.network != "localnet":
        raise RuntimeError("Draft runner is local-only: public acceptance requires verified cached-base reuse, dedicated wallet restoration and a global mutation lock")
    live = Live(a.wallet, a.network, a.max_spend_mist)
    live.preflight(auto_fund=a.auto_fund)
    state_path = Path(a.state).resolve()
    if state_path.is_relative_to(Path.cwd().resolve()):
        raise RuntimeError('private signing state must stay outside the checkout')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    fingerprint = hashlib.sha256(json.dumps([live.chain, live.operator, contracts_digest(Path(a.base)), contracts_digest(Path('contracts'))]).encode()).hexdigest()
    def save():
        state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    def pending(identity):
        state['pending']['verified_transaction'] = identity; save()
    live.evidence['success'] = False
    try:
        if state.get('pending'): raise RuntimeError('uncertain fixture mutation; reconcile before retrying')
        f = state.setdefault('fixtures',{}).get(fingerprint)
        if not f:
            state['pending']={'key':fingerprint,'operation':'publish base'};save()
            result=live.publish('publish pre-passkey framework',Path(a.base),state_path.with_suffix('.toml'),persist=pending)
            base=live.published(result);cap=live.created(result,'::package::UpgradeCap')[0];registry=live.created(result,'::device_registry::DeviceRegistry')[0]
            state['pending'].update(base=base,cap=cap,registry=registry);save()
            result=live.transaction('create existing framework organization','--move-call',f'{base}::customer::create',f'@{registry}','"SF198 disposable fixture"',budget=100_000_000)
            customer=live.created(result,'::customer::Customer')[0];fleet=live.created(result,'::customer::Fleet')[0]
            live.transaction('initialize existing authority','--move-call',f'{base}::customer::initialize_security',f'@{customer}',budget=20_000_000)
            tc.write_pubfile(state_path.with_suffix('.toml'),live.chain,Path('contracts'),base,cap)
            state['pending'].update(customer=customer,fleet=fleet,operation='upgrade head');save()
            result=live.publish('compatible passkey upgrade',Path('contracts'),state_path.with_suffix('.toml'),{'cap':cap,'package':base},persist=pending)
            f=dict(base=base,head=live.published(result),cap=cap,registry=registry,customer=customer,fleet=fleet)
            state['fixtures'][fingerprint]=f;del state['pending'];save()
        cap=live.object(f['cap']);assert dm.object_id(cap['owner']['AddressOwner'])==live.operator and dm.object_id(cap['content']['package'])==f['head']
        facts=dm.chain_facts(live.rpc_url,f['head']);origins=dm.origins(facts['objects'][f['head']]);assert origins['customer::Customer']==f['base'] and origins['passkey_factor::Enrollment']==f['head']
        live.evidence['fixture']=f;live.evidence['type_origins']=origins
        exercise(live, f, state_path, state, save)
        # Exercise the protected-lane adapter on the same actual chain and
        # existing signing identities; no package is published by this path.
        from live_package_upgrade import fixture_key, save_state
        source_key = fixture_key(contracts_digest(Path(a.base)), contracts_digest(Path('contracts')))
        gate_state = state_path.with_name('package-gate.json')
        save_state(gate_state, dict(chain=live.chain, operator=live.operator, fixtures={source_key:
            dict(base_package=f['base'], head_package=f['head'], upgrade_cap=f['cap'], registry=f['registry'])}))
        identity = hashlib.sha256(json.dumps([live.chain, live.operator, f['base'], f['head']]).encode()).hexdigest()
        state['fixtures'][identity] = f; save()
        cached_acceptance(live, gate_state, private=state_path)
        live.evidence['success']=True
    finally:
        live.finish(a.evidence)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet',required=True);p.add_argument('--network',choices=['testnet','localnet'],required=True)
    p.add_argument('--base',required=True);p.add_argument('--state',required=True);p.add_argument('--evidence',required=True)
    p.add_argument('--max-spend-mist',type=int,default=1_800_000_000);p.add_argument('--auto-fund',action='store_true')
    run(p.parse_args())
