"""Fail-closed fixture reuse and encrypted-state plumbing; no chain writes."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_passkey_factor as target
from ci_testnet_wallet import save_pending, restore_pending


class CachedPasskeyTests(unittest.TestCase):
    def test_existing_enrollment_binding_is_read_and_malformed_binding_refused(self):
        addresses = ['0x' + '1' * 64, '0x' + '2' * 64]
        binding = [2, *bytes.fromhex('1' * 64 + '2' * 64)]
        live = SimpleNamespace(client=Mock(return_value={'dynamicFields':[{'fieldId':'factor'}]}),
            object=Mock(return_value={'objType':'0x1::dynamic_field::Field<0x2::security_policy::FactorKey,0x2::security_policy::FactorConfiguration>',
                                     'content':{'name':{'factor':2},'value':{'binding':binding}}}))
        self.assertEqual(target.enrolled_keys(live, 'customer'), addresses)
        import base64
        live.object.return_value['content']['value']['binding'] = base64.b64encode(bytes(binding)).decode()
        self.assertEqual(target.enrolled_keys(live, 'customer'), addresses)
        for malformed in ([3, *binding[1:]], [2, 0], []):
            live.object.return_value['content']['value']['binding'] = malformed
            with self.assertRaisesRegex(RuntimeError, 'invalid cached'):
                target.enrolled_keys(live, 'customer')
        live.client.return_value = {'dynamicFields':[]}
        self.assertEqual(target.enrolled_keys(live, 'customer'), [])

    def test_private_subprocess_diagnostics_are_redacted_without_retry(self):
        import subprocess
        for stderr, expected in [('fixture gas coin unavailable', 'fixture gas coin unavailable'),
                                 ('private PEM secret request bytes', 'diagnostic withheld')]:
            with self.subTest(stderr=stderr), TemporaryDirectory() as directory:
                live = SimpleNamespace(network='testnet', rpc_url='read-only')
                result = subprocess.CompletedProcess([], 1, '', stderr)
                state = {'pending': {'digest': 'preserve-uncertainty'}}
                with patch.object(target.subprocess, 'run', return_value=result) as run:
                    with self.assertRaisesRegex(RuntimeError, expected) as failure:
                        target.exercise(live, {}, Path(directory) / 'state.json', state, Mock())
                    self.assertNotIn('private PEM', str(failure.exception))
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(state['pending']['digest'], 'preserve-uncertainty')

    def test_cached_gas_is_charged_and_only_deficit_is_transferred(self):
        from testnet_budget import Budget, Spent
        for balance in (0, 29_000_000, 30_000_000, 31_000_000):
            with self.subTest(balance=balance):
                live = SimpleNamespace(balance=Mock(return_value=balance), ledger=Budget(100_000_000, []),
                                       transaction=Mock())
                target.fund_passkey(live, 0, 'retained-address')
                self.assertEqual(live.ledger.used, balance)
                if balance < 30_000_000:
                    self.assertEqual(live.transaction.call_args.kwargs['funding'], 30_000_000 - balance)
                    self.assertIn(f'[{30_000_000 - balance}]', live.transaction.call_args.args)
                else:
                    live.transaction.assert_not_called()
        live.ledger = Budget(20_000_000, [])
        live.transaction.reset_mock()
        with self.assertRaises(Spent):
            target.fund_passkey(live, 0, 'retained-address')
        live.transaction.assert_not_called()

    def test_signing_identity_and_uncertainty_survive_ci_roundtrip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'cache.json'
            state.write_text('{}')
            values = {'live-passkey-state.keys.json': '[{"privateKey":"fixture-only"}]',
                      'live-passkey-state.json': '{"pending":{"digest":"uncertain"}}'}
            for name, value in values.items():
                (root / name).write_text(value)
            save_pending(root, state)
            for name in values:
                (root / name).unlink()
            restore_pending(root, state)
            for name, value in values.items():
                self.assertEqual((root / name).read_text().strip(), value)
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)

    def test_base_probe_requires_missing_factor_for_compatible_and_unchanged_packages(self):
        # Run the real exercise/tx control flow with deterministic signing and
        # effects fixtures: compatible packages accept a valid co-signer, while
        # a sealed legacy package refuses every call. Both reject wallet-only.
        import subprocess
        for upgraded, bad_abort, sealed in ((False, False, False), (True, False, False),
                                             (True, False, True), (False, True, False)):
            with self.subTest(upgraded=upgraded, bad_abort=bad_abort, sealed=sealed):
                head, base = '0x' + '2'*64, '0x' + ('3' if upgraded else '2')*64
                operator = '0x' + '1'*64
                keys = [dict(address='0x' + c*64) for c in ('4', '5')]
                requests, current = [], {}
                evidence = {'transactions': []}
                def prepare(args, *, input, **kwargs):
                    request = json.loads(input)
                    if request['operation'] == 'info':
                        return subprocess.CompletedProcess(args, 0, json.dumps(keys), '')
                    requests.append(request)
                    current.clear(); current.update(request)
                    return subprocess.CompletedProcess(args, 0, json.dumps(
                        dict(bytes='bytes', digest='digest', signature='signature' if 'sponsor' in request else None)), '')
                def decode(_):
                    return dict(digest='digest', gas_budget=20_000_000, gas_payment=[], expiration=None,
                        gas_owner=keys[current['sponsor']]['address'] if 'sponsor' in current else operator,
                        sender=operator)
                def execute(client, label, submit, **kwargs):
                    evidence['transactions'].append({'label': label})
                    submit(20_000_000)
                    # Distinct compatible packages enforce the same policy. An
                    # actually sealed legacy version rejects even co-signers.
                    status = 'success'
                    if current['operation'] == 'rename' and (
                            'sponsor' not in current or current['name'] == 'must fail removed'
                            or (sealed and current['package'] != head)):
                        status = 'failure'
                    self.assertEqual(status, kwargs['expect'], label)
                    return {'effects': {'status': {'status': status, 'error': 'fixture'}}}
                live = SimpleNamespace(network='testnet', rpc_url='fixture', operator=operator,
                    evidence=evidence, assert_lock=Mock(), decode=decode,
                    executor=Mock(return_value=Mock()), signer=Mock(return_value=Mock(return_value='wallet')),
                    ledger=SimpleNamespace(execute=execute))
                abort = Mock()
                abort.search.return_value.groups.return_value = (
                    'security_policy', 'wrong' if bad_abort else target.abort_site(
                        Path('contracts'), 'security_policy', 'check', 1), '1')
                installed = [value for key in keys for value in (operator, key['address'])]
                with patch.object(target.subprocess, 'run', side_effect=prepare), \
                     patch.object(target, 'enrolled_keys', return_value=installed), \
                     patch.object(target, 'fund_passkey'), \
                     patch.object(target.tc, 'bound', return_value={}), \
                     patch.object(target, 'ABORT', abort):
                    if bad_abort:
                        with self.assertRaisesRegex(AssertionError, 'missing-factor abort'):
                            target.exercise(live, dict(head=head, base=base, customer='customer', fleet='fleet'),
                                Path('/private/state.json'), {}, Mock())
                        continue
                    target.exercise(live, dict(head=head, base=base, customer='customer', fleet='fleet'),
                        Path('/private/state.json'), {}, Mock())
                probe = next(r for r in requests if r.get('name') == 'must fail old')
                self.assertEqual(probe['package'], base)
                self.assertNotIn('sponsor', probe)
                self.assertEqual(evidence['native_old_package_probe'],
                    'base-wallet-only')
                self.assertEqual(len(requests), 9)

    def fixture(self, upgraded=False, retained=False):
        base, head = '0x'+'2'*64, '0x'+('3' if upgraded else '2')*64
        source = 'bytecode-v1:' + 'a'*64
        built = {'base': {'kind': 'publish', 'digest': ('b' if upgraded else 'a')*64}}
        if upgraded:
            built['upgrade'] = {'kind': 'upgrade', 'digest': 'a'*64}
        cached = dict(base_package=base, head_package=head, upgrade_cap='cap', registry='registry',
            provenance=dict(build_cli=target.tc.BUILD['version'], verified_transactions=built))
        def package(version, enrollment):
            entries = [('customer', 'Customer', base)]
            if enrollment:
                entries.append(('passkey_factor', 'Enrollment', enrollment))
            return dict(kind='package', version=version, type_origins=[
                dict(module=m, datatype=t, package=p) for m,t,p in entries])
        facts = dict(chain_identifier='chain', objects={base: package(1, base if retained or not upgraded else None)})
        facts['objects'][head] = package(2 if upgraded else 1, base if retained else head)
        live = SimpleNamespace(chain='chain', operator='0x'+'1'*64, rpc_url='read-only',
            object=Mock(side_effect=lambda key: cap if key == 'cap' else dict(owner={'Shared': {}})),
            transaction=Mock(), evidence={})
        cap = dict(owner={'AddressOwner': live.operator}, content={'package': head, 'version': 2 if upgraded else 1})
        return live, cached, source, facts, cap

    def test_unfinished_pair_wins_over_same_head_cache_order(self):
        from live_package_upgrade import fixture_key
        import copy
        live, retained, source, facts, cap = self.fixture(upgraded=True)
        other = copy.deepcopy(retained)
        other['base_package'] = other['head_package'] = '0x' + '4' * 64
        old_key = fixture_key('bytecode-v1:' + 'b' * 64, source)
        new_key = fixture_key(source, source)
        state = dict(active_fixture=target.fixture_identity(live, retained),
                     pending={'digest': 'uncertain'}, completed={'proof': {'digest': 'known'}})
        before = copy.deepcopy(state)
        for entries in ([(new_key, other), (old_key, retained)],
                        [(old_key, retained), (new_key, other)]):
            cache = {'fixtures': dict(entries)}
            self.assertIs(target.retained_fixture(live, cache, source, state), retained)
            with patch.object(target.dm, 'chain_facts', return_value=facts):
                target.verify_cached_fixture(live, retained, source)
        self.assertEqual(state, before)
        live.transaction.assert_not_called()
        # Missing original provenance must never discard the journal.
        for cache, requested in (({'fixtures': {new_key: other}}, source),):
            with self.assertRaisesRegex(RuntimeError, 'requires its exact source package pair'):
                target.retained_fixture(live, cache, requested, state)
            self.assertEqual(state, before)
        changed_source = 'bytecode-v1:' + 'c' * 64
        changed_key = fixture_key(source, changed_source)
        cache = {'fixtures': {old_key: retained, changed_key: other}}
        self.assertIs(target.retained_fixture(live, cache, changed_source, state), other)
        self.assertEqual(state, before)
        self.assertIsNone(target.retained_fixture(live, {'fixtures': {old_key: retained}},
                                                changed_source, state))
        self.assertEqual(state, before)
        self.assertIs(target.retained_fixture(live, {'fixtures': {new_key: other}}, source,
            dict(active_fixture=state['active_fixture'], completed={})), other)

    def test_verified_unchanged_and_upgraded_origins(self):
        for upgraded, retained in ((False, False), (True, False), (True, True)):
            with self.subTest(upgraded=upgraded, retained=retained):
                live, cached, source, facts, cap = self.fixture(upgraded, retained)
                with patch.object(target.dm, 'chain_facts', return_value=facts):
                    origins = target.verify_cached_fixture(live, cached, source)
                self.assertEqual(origins['passkey_factor::Enrollment'],
                    cached['base_package'] if retained else cached['head_package'])
                live.transaction.assert_not_called()

    def test_bad_evidence_refused_before_mutation(self):
        changes = [
            lambda c,f,cap: cap['content'].update(version=3),
            lambda c,f,cap: cap['content'].update(package=c['base_package']),
            lambda c,f,cap: cap['owner'].update(AddressOwner='0x'+'4'*64),
            lambda c,f,cap: f.update(chain_identifier='wrong'),
            lambda c,f,cap: f['objects'][c['head_package']].update(version=1),
            lambda c,f,cap: f['objects'][c['head_package']]['type_origins'][0].update(package=c['head_package']),
            lambda c,f,cap: f['objects'][c['head_package']]['type_origins'][1].update(package='0x'+'4'*64),
            lambda c,f,cap: c.pop('provenance'),
            lambda c,f,cap: c['provenance'].update(build_cli='unverified'),
            lambda c,f,cap: c['provenance']['verified_transactions'].pop('upgrade'),
            lambda c,f,cap: c['provenance']['verified_transactions']['upgrade'].update(digest='bad'),
            lambda c,f,cap: c['provenance']['verified_transactions']['base'].update(kind='upgrade'),
        ]
        for change in changes:
            live, cached, source, facts, cap = self.fixture(True)
            change(cached, facts, cap)
            with patch.object(target.dm, 'chain_facts', return_value=facts):
                with self.assertRaises((AssertionError, RuntimeError, KeyError, ValueError)):
                    target.verify_cached_fixture(live, cached, source)
            live.transaction.assert_not_called()
        live, cached, source, facts, cap = self.fixture()
        cached['provenance']['verified_transactions']['upgrade'] = {'kind': 'upgrade', 'digest': 'a'*64}
        with self.assertRaises(RuntimeError):
            target.verify_cached_fixture(live, cached, source)
        del cached['provenance']['verified_transactions']['upgrade']
        cap['content']['version'] = 2
        with self.assertRaises(AssertionError):
            target.verify_cached_fixture(live, cached, source)

    def test_pending_refuses_after_verification_without_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            live, cached, source, facts, cap = self.fixture()
            live.wallet = root / 'client.yaml'
            private = root / 'live-passkey-state.json'
            private.write_text(json.dumps({'pending': {'digest': 'uncertain'}}))
            with patch('live_package_upgrade.load_state', return_value={'fixtures': {}}), \
                 patch('live_wallet_gate.head_fixture', return_value=cached), \
                 patch.object(target, 'contracts_digest', return_value=source), \
                 patch.object(target.dm, 'chain_facts', return_value=facts):
                with self.assertRaisesRegex(RuntimeError, 'missing retained'):
                    target.cached_acceptance(live, root / 'package.json')
            live.transaction.assert_not_called()
            self.assertEqual(json.loads(private.read_text())['pending']['digest'], 'uncertain')


class JournalRecoveryTests(unittest.TestCase):
    def live(self, status='success'):
        from testnet_budget import Budget
        result = dict(digest='digest', effects=dict(status=dict(status=status),
            gasUsed=dict(computationCost='1000000', storageCost='2000000', storageRebate='9000000')),
            objectChanges=[dict(type='created', objectId='proof', objectType='pkg::passkey_factor::Enrollment')])
        evidence = dict(transactions=[])
        live = SimpleNamespace(ledger=Budget(100_000_000, evidence['transactions']), evidence=evidence,
            assert_lock=Mock(), executor=Mock(), read_back=Mock(return_value=result))
        return live, result

    def test_interrupted_exact_digest_recovery_is_charged_without_replay(self):
        live, result = self.live()
        state = dict(pending=dict(operation='proof', digest='digest', gas_budget_mist=20_000_000, expect='success'))
        save = Mock()
        journal = target.FixtureJournal(live, state, save)
        with patch.object(target.tc, 'bound', side_effect=lambda r,d:r):
            journal.recover()
        live.read_back.assert_called_once_with('digest')
        live.executor.assert_not_called()
        self.assertNotIn('pending', state)
        self.assertEqual(live.ledger.used, 20_000_000)  # rebate does not replenish
        submit = Mock(side_effect=AssertionError('must not replay'))
        self.assertEqual(journal.run('proof', submit), result)
        submit.assert_not_called()
        self.assertEqual(state['completed']['proof']['objectChanges'][0]['objectId'], 'proof')

    def test_unknown_failed_and_changed_digest_retain_pending(self):
        for case in ('unknown', 'failure', 'wrong_digest'):
            live, result = self.live('failure' if case == 'failure' else 'success')
            if case == 'unknown':
                live.read_back.return_value = {}
            if case == 'wrong_digest':
                result['digest'] = 'different'
            state = dict(pending=dict(operation='setup', digest='digest', gas_budget_mist=20_000_000, expect='success'))
            with self.subTest(case=case), self.assertRaises((RuntimeError, AssertionError)):
                target.FixtureJournal(live, state, Mock()).recover()
            self.assertEqual(state['pending']['digest'], 'digest')
            self.assertEqual(live.ledger.used, 20_000_000)
            live.executor.assert_not_called()

    def test_expected_failed_probe_is_accounted_and_checkpointed(self):
        live, result = self.live('failure')
        state = dict(pending=dict(operation='wallet refused', digest='digest', gas_budget_mist=20_000_000, expect='failure'))
        with patch.object(target.tc, 'bound', side_effect=lambda r,d:r):
            target.FixtureJournal(live, state, Mock()).recover()
        self.assertEqual(live.evidence['transactions'][0]['status'], 'failure')
        self.assertIn('wallet refused', state['completed'])

    def test_legacy_cosigned_probe_is_retired_not_accepted_or_replayed(self):
        for status in ('success', 'failure'):
            live, result = self.live(status)
            pending = dict(operation='old package cannot bypass protection',
                digest='digest', gas_budget_mist=20_000_000, expect='failure')
            state = dict(pending=pending.copy())
            with patch.object(target.tc, 'bound', side_effect=lambda r,d:r):
                journal = target.FixtureJournal(live, state, Mock())
                journal.retire_legacy_base_probe()
                journal.recover()
            self.assertEqual(live.ledger.used, 20_000_000)
            self.assertNotIn('pending', state)
            self.assertNotIn(pending['operation'], state['completed'])
            self.assertEqual(state['retired_probes'][0]['pending'], pending)
            self.assertEqual(state['retired_probes'][0]['result'], result)
            self.assertFalse(live.evidence['retired_native_base_probe']['acceptance'])
            live.executor.assert_not_called()

    def test_legacy_probe_unknown_or_wrong_digest_is_preserved(self):
        for case in ('unknown', 'wrong_digest', 'wrong_budget'):
            live, result = self.live()
            if case == 'unknown': live.read_back.return_value = {}
            if case == 'wrong_digest': result['digest'] = 'different'
            state = dict(pending=dict(operation='old package cannot bypass protection',
                digest='digest', gas_budget_mist=1 if case == 'wrong_budget' else 20_000_000,
                expect='failure'))
            with self.assertRaises((RuntimeError, AssertionError)):
                target.FixtureJournal(live, state, Mock()).retire_legacy_base_probe()
            self.assertEqual(state['pending']['digest'], 'digest')
            live.executor.assert_not_called()

    def test_incomplete_pending_never_replays(self):
        live, _ = self.live()
        for pending in ({'operation':'setup'}, {'operation':'setup','digest':'d'},
                        {'operation':'setup','digest':'d','expect':'success','gas_budget_mist':None}):
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                target.FixtureJournal(live, dict(pending=pending), Mock()).recover()
        live.executor.assert_not_called()
        live.read_back.assert_not_called()

    def test_policy_field_read_preserves_initialized_authority(self):
        live = SimpleNamespace(client=Mock(return_value=dict(dynamicFields=[dict(fieldId='policy')])),
            object=Mock(return_value=dict(objType='pkg::dynamic_field::Field<pkg::security_policy::PolicyKey,pkg::security_policy::Policy>')))
        self.assertTrue(target.security_initialized(live, 'customer'))
        live.client.return_value = dict(dynamicFields=[])
        self.assertFalse(target.security_initialized(live, 'customer'))

class ProtectedResumeTests(unittest.TestCase):
    def test_completed_removal_is_not_undone_and_full_cycle_can_repeat(self):
        import subprocess
        operator = '0x' + '1'*64
        keys = [dict(address='0x'+c*64) for c in ('4','5')]
        head = '0x'+'2'*64
        installed = [v for key in keys for v in (operator,key['address'])]
        state, requests, current = {}, [], {}
        protected = [False]
        interruption = [True]
        class Interrupted(Exception): pass
        def save():
            if interruption[0] and 'backup passkey recovers lost primary' in state.get('completed',{}):
                interruption[0] = False
                raise Interrupted()
        def prepare(args, *, input, **kwargs):
            request = json.loads(input)
            if request['operation']=='info':
                return subprocess.CompletedProcess(args,0,json.dumps(keys),'')
            requests.append(request); current.clear(); current.update(request)
            return subprocess.CompletedProcess(args,0,json.dumps(dict(bytes='bytes',digest='digest',signature='sig')),'')
        def decode(_):
            return dict(digest='digest',gas_budget=20_000_000,gas_payment=[],expiration=None,
                sender=current['sender'],gas_owner=keys[current['sponsor']]['address']
                if 'sponsor' in current else current['sender'])
        def execute(client,label,submit,**kwargs):
            result = submit(20_000_000)
            operation = current['operation']
            authorized = not protected[0] or ('sponsor' in current
                and keys[current['sponsor']]['address'] in installed[1::2])
            status = 'success' if operation=='proof' or authorized else 'failure'
            self.assertEqual(status,kwargs['expect'],label)
            if status=='success':
                if operation=='policy':protected[0]=bool(current['mask'])
                if operation=='remove':
                    index=installed.index(keys[current['key']]['address'])
                    del installed[index-1:index+1]
                if operation=='enroll':
                    key = keys[0] if 'proof 0' in current['proof'] else keys[1]
                    installed.extend([operator,key['address']])
            return dict(digest='digest',effects=dict(status=dict(status=status,error='fixture')),
                objectChanges=[dict(type='created',objectId=label,objectType='pkg::passkey_factor::Enrollment')])
        evidence = dict(transactions=[])
        live = SimpleNamespace(network='testnet',rpc_url='fixture',operator=operator,evidence=evidence,
            assert_lock=Mock(),decode=decode,executor=Mock(return_value=Mock(return_value={})),
            signer=Mock(return_value=Mock(return_value='wallet')),
            ledger=SimpleNamespace(execute=execute),
            object=Mock(return_value=dict(content=dict(timestamp_ms=1000))),created=target.Live.created)
        abort = Mock()
        abort.search.return_value.groups.return_value = ('security_policy',target.abort_site(
            Path('contracts'),'security_policy','check',1),'1')
        with TemporaryDirectory() as directory, patch.object(target.subprocess,'run',side_effect=prepare), \
             patch.object(target,'enrolled_keys',side_effect=lambda *args:list(installed)), \
             patch.object(target,'fund_passkey'), patch.object(target.tc,'bound',side_effect=lambda r,d:r), \
             patch.object(target,'ABORT',abort):
            path=Path(directory)/'state.json'
            path.with_suffix('.keys.json').write_text('retained test identity')
            fixture=dict(head=head,base=head,customer='customer',fleet='fleet')
            with self.assertRaises(Interrupted):target.exercise(live,fixture,path,state,save)
            self.assertNotIn(keys[0]['address'],installed)
            before=len(requests)
            target.exercise(live,fixture,path,state,save)
            self.assertFalse(any(r['operation']=='enroll' for r in requests[before:]))
            self.assertEqual(installed,[])
            # Same retained identities, next successful independent cycle.
            state['completed']={}
            target.exercise(live,fixture,path,state,save)
            self.assertEqual(installed,[])
            self.assertEqual(sum(r['operation']=='enroll' for r in requests[before:]),2)
            self.assertEqual(path.with_suffix('.keys.json').read_text(),'retained test identity')

    def test_expired_interrupted_enrollment_proof_renews_with_same_keys(self):
        import subprocess
        operator='0x'+'1'*64
        keys=[dict(address='0x'+c*64) for c in ('4','5')]
        requests,current=[],{}
        state=dict(completed={'native possession proof 0':dict(digest='old-proof-digest',
            objectChanges=[dict(type='created',objectId='old-proof',objectType='pkg::passkey_factor::Enrollment')])})
        def prepare(args,*,input,**kwargs):
            request=json.loads(input)
            if request['operation']=='info':return subprocess.CompletedProcess(args,0,json.dumps(keys),'')
            requests.append(request);current.clear();current.update(request)
            return subprocess.CompletedProcess(args,0,json.dumps(dict(bytes='bytes',digest='new-digest',signature='sig')),'')
        def decode(_):
            return dict(digest='new-digest',gas_budget=20_000_000,gas_payment=[],expiration=None,
                sender=current['sender'],gas_owner=keys[current['sponsor']]['address']
                if 'sponsor' in current else current['sender'])
        def execute(client,label,submit,**kwargs):
            submit(20_000_000)
            return dict(digest='new-digest',effects=dict(status=dict(status='success')),
                objectChanges=[dict(type='created',objectId='new-proof',objectType='pkg::passkey_factor::Enrollment')])
        live=SimpleNamespace(network='testnet',rpc_url='fixture',operator=operator,evidence={},
            assert_lock=Mock(),executor=Mock(return_value=Mock(return_value={})),decode=decode,
            signer=Mock(return_value=Mock(return_value='wallet')),ledger=SimpleNamespace(execute=execute),
            created=target.Live.created,
            object=Mock(side_effect=lambda key:dict(content=dict(expires_ms=0) if key=='old-proof' else dict(timestamp_ms=1000))))
        with patch.object(target.subprocess,'run',side_effect=prepare), \
             patch.object(target,'enrolled_keys',return_value=[operator,keys[1]['address']]), \
             patch.object(target,'fund_passkey'), patch.object(target.tc,'bound',side_effect=lambda r,d:r):
            target.exercise(live,dict(head='head',customer='customer'),Path('/private/state.json'),state,Mock(),enrollment_only=True)
        self.assertIn('native possession proof 0 renewal old-proof-digest',state['completed'])
        enrollment=next(r for r in requests if r['operation']=='enroll')
        self.assertEqual((enrollment['proof'],enrollment['sponsor']),('new-proof',1))
        self.assertEqual([r['key'] for r in requests if r['operation']=='proof'],[0])


class PairTransitionTests(JournalRecoveryTests):
    def fixture(self):
        live, result = self.live()
        live.chain, live.operator = 'test-chain', 'operator'
        old = dict(base_package='old-base', head_package='old-head', provenance=dict(
            verified_transactions=dict(upgrade=dict(digest='a'*64))))
        new = dict(base_package='new-base', head_package='new-head', provenance=old['provenance'])
        old_id, new_id = (target.fixture_identity(live, f) for f in (old, new))
        state = dict(active_fixture=old_id, require_keys=True, fixtures={old_id: dict(customer='retained')},
                     pending=dict(operation='proof', digest='digest', gas_budget_mist=20_000_000, expect='success'))
        return live, result, {'fixtures': {'old': old, 'new': new}}, state, old_id, new_id

    def test_changed_source_selection_reconciles_before_switching(self):
        from live_package_upgrade import fixture_key
        live, result, cache, state, old, new = self.fixture()
        old_source, new_source = 'bytecode-v1:' + 'a' * 64, 'bytecode-v1:' + 'c' * 64
        cache['fixtures'] = {
            fixture_key(old_source, old_source): cache['fixtures']['old'],
            fixture_key(old_source, new_source): cache['fixtures']['new'],
        }
        original = json.loads(json.dumps(state))
        selected = target.retained_fixture(live, cache, new_source, state)
        self.assertEqual(target.fixture_identity(live, selected), new)
        self.assertEqual(state, original)
        snapshots = []
        with patch.object(target, 'verify_cached_fixture') as verify, \
             patch.object(target.tc, 'bound', side_effect=lambda r, d: r):
            target.select_journal(live, cache, state, new,
                                  lambda: snapshots.append(json.loads(json.dumps(state))))
        verify.assert_called_once_with(live, cache['fixtures'][fixture_key(old_source, old_source)], old_source)
        self.assertEqual(snapshots[-1]['active_fixture'], new)
        self.assertEqual(state['journals'][old]['completed']['proof'], result)
        self.assertEqual(state['fixtures'], original['fixtures'])
        self.assertEqual(live.ledger.used, 20_000_000)
        live.executor.assert_not_called()

    def test_legacy_probe_retires_before_pair_transition_without_replay(self):
        for status in ('success', 'failure', 'unreadable'):
            live,result,cache,state,old,new = self.fixture()
            label = 'old package cannot bypass protection'
            state['pending'].update(operation=label, expect='failure')
            original = dict(state['pending'])
            if status == 'unreadable':
                live.read_back.return_value = {}
            else:
                result['effects']['status']['status'] = status
            with self.subTest(status=status), patch.object(target,'verify_cached_fixture') as verify, \
                 patch.object(target.tc,'bound',side_effect=lambda r,d:r):
                if status == 'unreadable':
                    with self.assertRaisesRegex(RuntimeError, 'exact digest not readable'):
                        target.select_journal(live,cache,state,new,lambda: None)
                    self.assertEqual(state['pending'],original)
                    self.assertEqual(state['active_fixture'],old)
                    self.assertNotIn('journals',state)
                else:
                    target.select_journal(live,cache,state,new,lambda: None)
                    self.assertEqual(state['active_fixture'],new)
                    self.assertNotIn('pending',state)
                    self.assertNotIn(label,state['journals'][old]['completed'])
                    self.assertEqual(state['retired_probes'][0]['pending'],original)
                    self.assertEqual(state['retired_probes'][0]['result'],result)
                    self.assertFalse(live.evidence['retired_native_base_probe']['acceptance'])
                    self.assertEqual(live.ledger.used,20_000_000)
                verify.assert_called_once()
                live.executor.assert_not_called()
            self.assertTrue(state['require_keys'])
            self.assertEqual(state['fixtures'][old]['customer'],'retained')

    def test_old_pending_parked_and_restored_without_duplicate_submission(self):
        live,result,cache,state,old,new = self.fixture()
        snapshots=[]
        save=lambda: snapshots.append(json.loads(json.dumps(state)))
        with patch.object(target,'verify_cached_fixture') as verify, \
             patch.object(target.tc,'bound',side_effect=lambda r,d:r):
            target.select_journal(live,cache,state,new,save)
            self.assertEqual(state['active_fixture'],new)
            self.assertEqual(state['journals'][old]['completed']['proof'],result)
            self.assertEqual(state['completed'],{})
            self.assertNotIn('pending',state)
            # Simulate a restart by round-tripping the last durable checkpoint.
            state=json.loads(json.dumps(snapshots[-1]))
            target.select_journal(live,cache,state,old,lambda: None)
            self.assertEqual(state['completed']['proof'],result)
            verify.assert_called()
        self.assertTrue(state['require_keys'])
        self.assertEqual(state['fixtures'][old]['customer'],'retained')
        live.executor.assert_not_called()
        self.assertEqual(live.ledger.used,20_000_000)

    def test_unknown_failure_missing_provenance_and_changed_effects_never_switch(self):
        for case in ('unknown','failure','missing','changed','ambiguous'):
            live,result,cache,state,old,new=self.fixture()
            if case=='unknown':live.read_back.return_value={}
            if case=='failure':result['effects']['status']['status']='failure'
            if case=='missing':cache['fixtures']={}
            if case=='changed':
                state['completed']={'proof':json.loads(json.dumps(result))}
                state.pop('pending')
                result['effects']['status']['status']='failure'
            if case=='ambiguous':state['completed']={'proof':result}
            with self.subTest(case=case), patch.object(target,'verify_cached_fixture'), \
                 patch.object(target.tc,'bound',side_effect=lambda r,d:r):
                with self.assertRaises(RuntimeError):
                    target.select_journal(live,cache,state,new,lambda: None)
            self.assertEqual(state['active_fixture'],old)
            self.assertNotIn('journals',state)
            live.executor.assert_not_called()

    def test_crash_after_pending_recovery_retains_original_pair_and_completed(self):
        live,result,cache,state,old,new=self.fixture()
        snapshots=[]
        def save():
            snapshots.append(json.loads(json.dumps(state)))
            raise OSError('simulated crash after atomic write')
        with patch.object(target,'verify_cached_fixture'), patch.object(target.tc,'bound',side_effect=lambda r,d:r):
            with self.assertRaises(OSError):target.select_journal(live,cache,state,new,save)
            state=snapshots[-1]
            target.select_journal(live,cache,state,new,lambda: None)
        self.assertEqual(state['journals'][old]['completed']['proof'],result)
        live.executor.assert_not_called()
        self.assertEqual(live.ledger.used,20_000_000)


class RetainedPairKeysTests(unittest.TestCase):
    def test_archived_journal_never_generates_replacement_keys(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            private=root/'live-passkey-state.json'
            private.write_text(json.dumps(dict(journals={'old':{'completed':{}}})))
            live=SimpleNamespace(chain='chain',operator='operator',wallet=root/'client.yaml')
            cached=dict(base_package='base',head_package='head')
            with patch('live_package_upgrade.load_state',return_value={'fixtures':{}}), \
                 patch('live_wallet_gate.head_fixture',return_value=cached), \
                 patch.object(target,'contracts_digest',return_value='source'), \
                 patch.object(target,'verify_cached_fixture'), patch.object(target,'native_fixture') as native:
                with self.assertRaisesRegex(RuntimeError,'missing retained'):
                    target.cached_acceptance(live,root/'package.json',private)
                native.assert_not_called()
            self.assertIn('old',json.loads(private.read_text())['journals'])
