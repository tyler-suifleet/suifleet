#!/usr/bin/env python3
"""Draft isolated-node native passkey sponsorship acceptance.

Reuses exact source-pair fixtures, stops on uncertainty, and charges publication,
failures and passkey gas grants against one non-replenishing run allowance.
"""
from abort_site import abort_site
import argparse
import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from live_common import Live, plain, ABORT
from live_package_upgrade import contracts_digest
import deployment_manifest as dm
import sui_toolchain as tc


def fund_passkey(live, index, address, journal=None):
    total = live.balance(address)
    live.ledger.grant(total)  # Cached gas is not free allowance.
    deficit = max(0, 30_000_000 - total)
    if deficit:
        transaction = journal.wallet if journal else live.transaction
        transaction(f'fund passkey {index}', '--split-coins', 'gas', f'[{deficit}]',
                         '--assign', 'funding', '--transfer-objects', '[funding]',
                         f'@{address}', budget=20_000_000, funding=deficit)


def enrolled_keys(live, customer):
    """Read the existing binding before retrying pre-policy setup; never rotate it."""
    for field in live.client('dynamic-field', customer)['dynamicFields']:
        value = live.object(field['fieldId'])
        if '::security_policy::FactorKey,' not in str(value.get('objType', '')):
            continue
        content = plain(value['content'])
        if int(content['name']['factor']) != 2:
            continue
        raw = content['value']['binding']
        binding = base64.b64decode(raw, validate=True) if isinstance(raw, str) else bytes(raw)
        # Enrollment caps at 32 pairs, so the BCS vector length is one byte.
        if not binding or binding[0] > 64 or binding[0] % 2 or len(binding) != 1 + 32 * binding[0]:
            raise RuntimeError('invalid cached native passkey binding')
        return ['0x' + binding[i:i+32].hex() for i in range(1, len(binding), 32)]
    return []



def native_fixture(live, state_path, **kwargs):
    result = subprocess.run(['node','scripts/passkey_transaction.cjs'], input=json.dumps(dict(keys=str(state_path.with_suffix('.keys.json')), network=live.network, endpoint=live.rpc_url, **kwargs)), capture_output=True, text=True, check=False)
    if result.returncode:
        # Provider/SDK messages can contain request bytes or private paths.
        # Expose only reviewed diagnostics; preserve the conservative ledger
        # reservation and never retry signing/submission on this exception.
        safe = {'fixture gas coin unavailable', 'Invalid private fixture state',
                'No valid gas coins found for the transaction.', 'Missing retained private fixture state'}
        diagnostic = result.stderr.strip()
        reason = diagnostic if diagnostic in safe else 'private subprocess diagnostic withheld'
        raise RuntimeError(f'native passkey {kwargs["operation"]} preparation failed: {reason}')
    return json.loads(result.stdout)

class FixtureJournal:
    """Private checkpoints; never rebuild a transaction with an unknown outcome."""
    def __init__(self, live, state, save):
        self.live, self.state, self.save = live, state, save

    def pending(self, label, data, budget):
        self.state['pending'] = dict(operation=label, digest=data['digest'], gas_budget_mist=budget, funding_mist=self.funding, expect=self.expected)
        self.save()

    def run(self, label, submit, *, budget=20_000_000, funding=0, expect='success'):
        self.expected, self.funding = expect, funding
        completed = self.state.setdefault('completed', {})
        if label in completed:
            return completed[label]
        pending = self.state.get('pending')
        if pending:
            if (pending.get('operation') != label or not pending.get('digest')
                    or pending.get('gas_budget_mist') != budget
                    or pending.get('funding_mist', 0) != funding):
                raise RuntimeError('uncertain native passkey mutation; reconcile exact digest before retry')
            # A prior process may have submitted it. Charge the full original
            # reservation in this run too; refunds cannot replenish allowance.
            self.live.ledger.grant(budget + funding)
            result = self.live.read_back(pending['digest'])
            if not result:
                raise RuntimeError('uncertain native passkey mutation; exact digest not readable')
            result = tc.bound(result, pending['digest'])
            effects = result['effects']
            cost = max(0, int(effects['gasUsed']['computationCost'])
                       + int(effects['gasUsed']['storageCost']) - int(effects['gasUsed']['storageRebate']))
            if cost > budget:
                raise RuntimeError('reconciled gas exceeds original reservation')
            self.live.evidence['transactions'].append(dict(label=label, digest=result['digest'],
                status=effects['status']['status'], gas_mist=cost, gas_budget_mist=budget, funding_mist=funding,
                reconciled=True, allowance_used_mist=self.live.ledger.used))
        else:
            self.live.assert_lock()
            result = self.live.ledger.execute(self.live.executor(), label, submit,
                gas=budget, funding=funding, expect=expect, require_full=True)
        if result['effects']['status']['status'] != expect:
            # Keep the failed effects and pending identity. Explicit repair is
            # needed; failure gas is charged and the mutation is never replayed.
            raise RuntimeError('reconciled native passkey effects differ from expected outcome')
        completed[label] = result
        self.state.pop('pending', None)
        self.save()
        return result

    def recover(self):
        pending = self.state.get('pending')
        if pending:
            if (pending.get('expect') not in ('success', 'failure') or not pending.get('digest')
                    or type(pending.get('gas_budget_mist')) is not int or pending['gas_budget_mist'] <= 0
                    or type(pending.get('funding_mist', 0)) is not int or pending.get('funding_mist', 0) < 0):
                raise RuntimeError('uncertain native passkey mutation; incomplete retained transaction identity')
            self.run(pending['operation'], None, budget=pending.get('gas_budget_mist'),
                     funding=pending.get('funding_mist', 0), expect=pending['expect'])

    def wallet(self, label, *ptb, budget=20_000_000, funding=0):
        return self.run(label, self.live.exact_ptb(ptb,
            persist=lambda data: self.pending(label, data, budget)), budget=budget, funding=funding)


def security_initialized(live, customer):
    """Read authority state; an existing protected policy must not be reinitialized."""
    fields = live.client('dynamic-field', customer)['dynamicFields']
    return any('::security_policy::PolicyKey,' in str(live.object(field['fieldId']).get('objType', ''))
               for field in fields)


def exercise(live, f, state_path, state, save, *, enrollment_only=False):
    journal = FixtureJournal(live, state, save)
    def fixture(**kwargs):
        return native_fixture(live, state_path, requireExistingKeys=bool(state.get('require_keys')), **kwargs)
    def tx(label, **kwargs):
        live.assert_lock()
        def submit(budget):
            prepared = fixture(budget=budget, **kwargs)
            data = live.decode(prepared['bytes'])
            assert data['digest'] == prepared['digest']
            assert int(data['gas_budget']) == budget
            live.evidence.setdefault('native_payment', []).append(dict(
                operation=label, gas_objects=len(data['gas_payment']), expiration=data['expiration']))
            expected_owner = keys[kwargs['sponsor']]['address'] if 'sponsor' in kwargs else kwargs['sender']
            assert tc.normal(data['gas_owner']) == tc.normal(expected_owner)
            assert tc.normal(data['sender']) == tc.normal(kwargs['sender'])
            # The locally constructed fixture is the reviewed operation builder;
            # wallet authority signs only its verified bytes, never a digest alone.
            journal.pending(label, data, budget)
            signatures = [prepared['signature']] if kwargs['operation'] == 'proof' else [live.signer(None, live.operator)(prepared['bytes'])] + ([prepared['signature']] if prepared['signature'] else [])
            result = tc.bound(live.executor()('execute-signed-tx','--tx-bytes',prepared['bytes'],*[arg for signature in signatures for arg in ('--signatures', signature)]), data['digest'])
            return result
        expected = kwargs.pop('expect', 'success')
        result = journal.run(label, submit, expect=expected)
        if expected == 'failure' and kwargs['package'] == f['head']:
            found = ABORT.search(str(result['effects']['status'].get('error', '')))
            observed = found.groups() if found else None
            live.evidence.setdefault('native_aborts', {})[label] = observed
            assert observed == ('security_policy', abort_site(Path('contracts'), 'security_policy', 'check', 1), '1'), 'native refusal must be the missing-factor abort'
        return result
    if state.get('require_keys') and not state_path.with_suffix('.keys.json').is_file():
        raise RuntimeError('missing retained native passkey signing identity; preserve protected fixture')
    keys=fixture(operation='info')
    state['require_keys'] = True
    save()
    installed = enrolled_keys(live, f['customer'])
    expected = {tc.normal(key['address']) for key in keys}
    if any(tc.normal(installed[i]) != tc.normal(live.operator) or tc.normal(installed[i+1]) not in expected
           for i in range(0, len(installed), 2)):
        raise RuntimeError('cached native passkey binding differs; preserve identities and reconcile')
    for i,key in enumerate(keys):
        # Funding is charged as a grant; subsequent passkey-paid gas cannot
        # enlarge the allowance. Reused signing identities retain their gas.
        fund_passkey(live, i, key['address'], journal)
        reused = tc.normal(key['address']) in {tc.normal(k) for k in installed[1::2]}
        if reused:
            live.evidence.setdefault('native_reused_enrollments', []).append(key['address'])
        if reused or 'enable native passkey protection' in state.get('completed', {}):
            continue
        label = f'native possession proof {i}'
        # Resume the latest proof, including a prior expiry renewal. Never replay
        # an unreadable pending digest or change the retained signing identity.
        completed_proofs = [(name, result) for name, result in state.get('completed', {}).items()
                            if name == label or name.startswith(label + ' renewal ')]
        if completed_proofs:
            label, result = completed_proofs[-1]
            proof = live.created(result, '::passkey_factor::Enrollment')[0]
            expires = int(live.object(proof)['content']['expires_ms'])
            if expires <= int(live.object('0x6')['content']['timestamp_ms']):
                label = f'native possession proof {i} renewal {result["digest"]}'
        result = tx(label, operation='proof', key=i, package=f['head'], customer=f['customer'],
                    administrator=live.operator, sender=key['address'],
                    expires=int(live.object('0x6')['content']['timestamp_ms'])+300000)
        proof=live.created(result,'::passkey_factor::Enrollment')[0]
        sponsors = [j for j, candidate in enumerate(keys)
                    if tc.normal(candidate['address']) in {tc.normal(k) for k in installed[1::2]}]
        tx(f'wallet enrolls passkey {i}', operation='enroll', proof=proof,
           package=f['head'], customer=f['customer'], sender=live.operator,
           **({'sponsor': sponsors[0]} if sponsors else {}))
        installed.extend([live.operator, key['address']])
    if enrollment_only:
        return
    common=dict(package=f['head'],customer=f['customer'],sender=live.operator)
    tx('enable native passkey protection',operation='policy',mask=2,sponsor=0,**common)
    tx('wallet plus passkey allows action',operation='rename',fleet=f['fleet'],name='Native co-signed action',sponsor=0,**common)
    tx('wallet alone refused on chain',operation='rename',fleet=f['fleet'],name='must fail',expect='failure',**common)
    # A co-signed call to an unchanged base/head is authorized, not a bypass.
    # Only a distinct old package must refuse even with a valid factor.
    distinct_base = dm.object_id(f['base']) != dm.object_id(f['head'])
    tx('old package cannot bypass protection' if distinct_base else 'unchanged base wallet alone refused',
       operation='rename',fleet=f['fleet'],name='must fail old',expect='failure',
       **({'sponsor': 0} if distinct_base else {}), **{**common,'package':f['base']})
    live.evidence['native_old_package_probe'] = 'distinct-package' if distinct_base else 'unchanged-root-wallet-only'
    tx('backup passkey recovers lost primary',operation='remove',administrator=live.operator,key=0,sponsor=1,**common)
    tx('removed primary refused',operation='rename',fleet=f['fleet'],name='must fail removed',sponsor=0,expect='failure',**common)
    tx('surviving backup allows action',operation='rename',fleet=f['fleet'],name='Recovered with backup',sponsor=1,**common)
    tx('backup authorizes disable',operation='policy',mask=0,sponsor=1,**common)
    tx('wallet removes final key after disable',operation='remove',administrator=live.operator,key=1,**common)

def verify_cached_fixture(live, cached, source):
    """Recheck authenticated package-gate evidence before any passkey mutation."""
    head, base = (dm.object_id(cached[k]) for k in ('head_package', 'base_package'))
    proof = cached.get('provenance') or {}
    built = proof.get('verified_transactions') or {}
    original = built.get('base') or {}
    upgraded = built.get('upgrade')
    current = upgraded if head != base else original
    if (proof.get('build_cli') != tc.BUILD['version']
            or original.get('kind') != 'publish'
            or not re.fullmatch('[0-9a-f]{64}', str(original.get('digest', '')))
            or not isinstance(current, dict)
            or current.get('kind') != ('upgrade' if head != base else 'publish')
            or 'bytecode-v1:' + str(current.get('digest')) != source
            or (head == base and upgraded is not None)):
        raise RuntimeError('native passkey fixture lacks verified source/publication provenance')
    cap = live.object(cached['upgrade_cap'])
    assert dm.object_id(cap['owner']['AddressOwner']) == dm.object_id(live.operator)
    assert dm.object_id(cap['content']['package']) == head
    expected_version = 1 if head == base else 2
    assert str(cap['content']['version']) == str(expected_version)
    facts = dm.chain_facts(live.rpc_url, base, head)
    assert facts['chain_identifier'] == live.chain
    base_fact, head_fact = (facts['objects'][k] for k in (base, head))
    assert str(base_fact['version']) == '1'
    assert str(head_fact['version']) == str(expected_version)
    base_origins, origins = dm.origins(base_fact), dm.origins(head_fact)
    # Retained types keep their introduction origin; an additive Enrollment
    # introduced by the upgrade has the head origin instead.
    assert all(origins.get(name) == origin for name, origin in base_origins.items())
    assert base_origins['customer::Customer'] == base
    if 'passkey_factor::Enrollment' in base_origins:
        assert base_origins['passkey_factor::Enrollment'] == base
    assert origins['customer::Customer'] == base
    assert origins['passkey_factor::Enrollment'] == base_origins.get('passkey_factor::Enrollment', head)
    assert 'Shared' in live.object(cached['registry'])['owner']
    return origins


def cached_acceptance(live, package_state, private=None):
    """Use the package gate's exact source fixture and existing security ledger.

    CI preserves these files only inside its authenticated encrypted state.
    No publication, new operator wallet, faucet or extra allowance is introduced.
    """
    from live_wallet_gate import head_fixture
    from live_package_upgrade import load_state, save_state
    cache = load_state(package_state, live.chain, live.operator)
    source = contracts_digest(Path('contracts'))
    cached = head_fixture(cache, source)
    if not cached:
        raise RuntimeError('native passkey acceptance requires the exact cached package gate fixture')
    head, base = cached['head_package'], cached['base_package']
    origins = verify_cached_fixture(live, cached, source)
    private = Path(private) if private else live.wallet.parent / 'live-passkey-state.json'
    if private.resolve().is_relative_to(Path.cwd().resolve()):
        raise RuntimeError('private signing state must stay outside the checkout')
    state = json.loads(private.read_text()) if private.exists() else {}
    def save():
        save_state(private, state)
    identity = hashlib.sha256(json.dumps([live.chain, live.operator, base, head]).encode()).hexdigest()
    if state.get('active_fixture', identity) != identity and (state.get('pending') or state.get('completed')):
        raise RuntimeError('unfinished native passkey fixture belongs to another package pair')
    state['active_fixture'] = identity
    f = state.setdefault('fixtures', {}).get(identity)
    keys_path = private.with_suffix('.keys.json')
    if (f or state.get('pending') or state.get('completed')) and not keys_path.is_file():
        raise RuntimeError('missing retained native passkey signing identity; preserve protected fixture')
    native_fixture(live, private, operation='info', requireExistingKeys=bool(f or state.get('pending') or state.get('completed')))
    state['require_keys'] = True
    journal = FixtureJournal(live, state, save)
    journal.recover()
    if not f:
        result = journal.wallet('create cached passkey organization', '--move-call',
            f'{head}::customer::create', '@' + cached['registry'],
            '"SF198 protected CI fixture"', budget=100_000_000)
        f = dict(base=base, head=head, customer=live.created(result, '::customer::Customer')[0],
                 fleet=live.created(result, '::customer::Fleet')[0])
        state['fixtures'][identity] = f
        save()
    # Existing policy can require a passkey even for this idempotent call.
    # Reconcile an interrupted initializer before inspecting current state.
    label = 'initialize cached passkey authority'
    if state.get('pending', {}).get('operation') == label or not security_initialized(live, f['customer']):
        journal.wallet(label, '--move-call', f'{head}::customer::initialize_security',
                       '@' + f['customer'])
    state['require_keys'] = keys_path.exists()
    exercise(live, f, private, state, save)
    state['completed'] = {}
    state['require_keys'] = True
    save()
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
        # Exercise real coinless gas execution on the disposable chain. This
        # self-transfer only changes balance representation; no public-wallet
        # shaping or extra allowance is used by cached_acceptance.
        live.transaction('local regression funds operator address balance',
            '--split-coins', 'gas', '[200000000]', '--assign', 'address_funds',
            '--move-call', '0x2::coin::send_funds', '<0x2::sui::SUI>',
            'address_funds.0', '@' + live.operator, budget=20_000_000)
        # Exercise the protected-lane adapter on the same actual chain and
        # existing signing identities; no package is published by this path.
        from live_package_upgrade import fixture_key, save_state
        source_key = fixture_key(contracts_digest(Path(a.base)), contracts_digest(Path('contracts')))
        gate_state = state_path.with_name('package-gate.json')
        save_state(gate_state, dict(chain=live.chain, operator=live.operator, fixtures={source_key:
            dict(base_package=f['base'], head_package=f['head'], upgrade_cap=f['cap'], registry=f['registry'],
                 provenance=dict(build_cli=tc.BUILD['version'], verified_transactions=dict(
                     base=dict(kind='publish', digest=contracts_digest(Path(a.base)).removeprefix('bytecode-v1:')),
                     upgrade=dict(kind='upgrade', digest=contracts_digest(Path('contracts')).removeprefix('bytecode-v1:')))))}))
        identity = hashlib.sha256(json.dumps([live.chain, live.operator, f['base'], f['head']]).encode()).hexdigest()
        state['fixtures'][identity] = f; save()
        # Persist a real protected on-chain policy and interrupt immediately
        # after its successful effects, then resume through the CI adapter.
        class Interrupted(Exception):
            pass
        original_executor = live.executor
        def interrupting_executor(*args, **kwargs):
            execute = original_executor(*args, **kwargs)
            def submitted(*command):
                result = execute(*command)
                if state.get('pending', {}).get('operation') == 'enable native passkey protection':
                    raise Interrupted()
                return result
            return submitted
        live.executor = interrupting_executor
        try:
            exercise(live, f, state_path, state, save)
        except Interrupted:
            pass
        else:
            raise AssertionError('protected interruption was not exercised')
        finally:
            live.executor = original_executor
        cached_acceptance(live, gate_state, private=state_path)
        assert len(live.evidence.get('native_reused_enrollments', [])) >= 2
        policy_payments = [p for p in live.evidence['native_payment']
                           if p['operation'] == 'wallet alone refused on chain']
        assert policy_payments and all(p['gas_objects'] == 0 for p in policy_payments), 'local regression needs coinless wallet payment'
        live.evidence['success']=True
    finally:
        live.finish(a.evidence)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet',required=True);p.add_argument('--network',choices=['testnet','localnet'],required=True)
    p.add_argument('--base',required=True);p.add_argument('--state',required=True);p.add_argument('--evidence',required=True)
    p.add_argument('--max-spend-mist',type=int,default=1_800_000_000);p.add_argument('--auto-fund',action='store_true')
    run(p.parse_args())
