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
                with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                    target.cached_acceptance(live, root / 'package.json')
            live.transaction.assert_not_called()
            self.assertEqual(json.loads(private.read_text())['pending']['digest'], 'uncertain')
