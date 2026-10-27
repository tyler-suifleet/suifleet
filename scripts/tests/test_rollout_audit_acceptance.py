"""Offline orchestration regressions; no wallet, RPC, or remote mutations."""
import json
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rollout_audit_acceptance as audit


class AuditAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(chain_identifier='4c78adac', call_package_id='package',
            type_origins={'deployment_manager::DecisionKey': 'new-origin',
                          'deployment_manager::DeploymentRecord': 'origin'},
            shared_objects={'registry': {'id': 'registry'}})
        self.fixture, self.disk, self.calls, self.reads = {}, {}, [], []
        self.output = SimpleNamespace(returncode=1, stdout='', stderr='retention has not advanced')

    def save(self):
        self.disk = copy.deepcopy(self.fixture)

    def step(self, label, ptb, calls):
        self.calls.append((label, ptb, calls))
        return dict(digest=label, effects={'status': {'status': 'success'}},
            objectChanges=[dict(type='created', objectId='record',
                                objectType='origin::deployment_manager::DeploymentRecord')])

    def client(self, *args):
        self.reads.append(args)
        if args[0] == 'object':
            return {'owner': {'Shared': {}}, 'content': {
                'type': 'origin::deployment_manager::DeploymentRecord', 'fields': {
                    'creator': 'operator', 'component_name': 'audit-only', 'version': '1', 'schema_version': '3'}}}
        return {'digest': args[-1], 'checkpoint': '10', 'effects': {'status': {'status': 'success'}}}

    def invoke(self, **kw):
        with tempfile.TemporaryDirectory() as root, patch.object(audit.subprocess, 'run', return_value=self.output) as reader:
            evidence = {'checks': []}
            try:
                audit.run(self.step, self.client, self.manifest, 'device', evidence, root,
                    fixture=self.fixture, save=self.save, operator='operator', fixture_key='bytecode-key', **kw)
            finally:
                if (Path(root) / 'rollout-audit-input.json').exists():
                    self.assertEqual(json.loads((Path(root) / 'rollout-audit-input.json').read_text())['timeoutMs'], 0)
                self.evidence = evidence
                self.reader_calls = reader.call_count

    def admit(self):
        with self.assertRaisesRegex(RuntimeError, 'retention'):
            self.invoke()

    def prove(self):
        self.output = SimpleNamespace(returncode=0, stdout=json.dumps({'lowest': '11',
            'audit': {'complete': True}}), stderr='')

    def test_timeout_restart_preserves_original_record_without_more_gas(self):
        self.admit()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.evidence['rollout_audit_pending']['retained'])
        self.assertNotIn('rollout_audit', self.evidence)
        self.fixture = copy.deepcopy(self.disk)  # Process restart from durable cache.
        self.admit()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(self.fixture['checkpoint'], '10')
        self.prove()
        self.invoke()
        self.assertEqual(self.evidence['rollout_audit']['record'], 'record')
        self.assertEqual(len(self.evidence['checks']), 1)
        self.invoke()  # Revalidate current state each time, not a cached success.
        self.assertEqual(self.reader_calls, 1)
        self.assertEqual(len(self.calls), 2)

    def test_provenance_mismatch_refuses_recreation(self):
        self.admit()
        for field in ['fixture_key', 'operator', 'package', 'registry', 'device', 'chain', 'origins']:
            with self.subTest(field=field):
                self.fixture = copy.deepcopy(self.disk)
                self.fixture['binding'][field] = 'changed'
                with self.assertRaisesRegex(RuntimeError, 'provenance'):
                    self.invoke()
        self.assertEqual(len(self.calls), 2)

    def test_checkpoint_read_failure_does_not_repeat_admission(self):
        original = self.client
        self.client = lambda *args: original(*args) if args[0] == 'object' else {'digest': args[-1], 'effects': {'status': {'status': 'success'}}}
        with self.assertRaisesRegex(RuntimeError, 'checkpoint unavailable'):
            self.invoke()
        self.fixture = copy.deepcopy(self.disk)
        self.client = original
        self.admit()
        self.assertEqual(len(self.calls), 2)

    def test_wrong_current_record_owner_never_admits(self):
        original = self.client
        def client(*args):
            result = original(*args)
            result['content']['fields']['creator'] = 'other-operator'
            return result
        self.client = client
        with self.assertRaisesRegex(RuntimeError, 'target identity changed'):
            self.invoke()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('admission_digest', self.fixture)

    def test_false_floor_or_incomplete_reader_result_never_proves_acceptance(self):
        self.admit()
        for proof in [{'lowest': '10', 'audit': {'complete': True}},
                      {'lowest': '11', 'audit': {'complete': False}}]:
            self.output = SimpleNamespace(returncode=0, stdout=json.dumps(proof), stderr='')
            with self.assertRaisesRegex(RuntimeError, 'did not prove'):
                self.invoke()
            self.assertNotIn('proof', self.fixture)
            self.assertEqual(len(self.calls), 2)

    def test_retire_only_after_proof_and_never_recreate_retired_record(self):
        with self.assertRaisesRegex(RuntimeError, 'retention'):
            self.invoke(retire=True)
        self.assertEqual(len(self.calls), 2)
        self.prove()
        self.invoke(retire=True)
        self.assertTrue(self.fixture['retired'])
        self.assertEqual(self.calls[-1][0], 'delete durable decision audit rollout')
        with self.assertRaisesRegex(RuntimeError, 'already retired'):
            self.invoke()
        self.assertEqual(len(self.calls), 3)

    def test_unknown_cleanup_reconciles_before_reading_deleted_object(self):
        self.admit(); self.prove(); self.invoke()
        self.fixture['pending_operation'] = {'label': 'delete durable decision audit rollout'}
        self.output = SimpleNamespace(returncode=1, stdout='', stderr='object deleted')
        self.invoke()
        self.assertEqual(self.reader_calls, 0)
        self.assertTrue(self.fixture['retired'])

    def test_unknown_cleanup_without_proof_refused(self):
        self.admit()
        self.fixture['pending_operation'] = {'label': 'delete durable decision audit rollout'}
        with self.assertRaisesRegex(RuntimeError, 'no preceding'):
            self.invoke()
        self.assertEqual(len(self.calls), 2)

    def test_old_package_skips_without_mutation(self):
        audit.run(lambda *args: self.fail('must not mutate'), None, {'type_origins': {}}, None, {}, None)

    def test_new_package_refuses_ephemeral_only_state(self):
        with self.assertRaisesRegex(RuntimeError, 'durable fixture'):
            audit.run(None, None, self.manifest, None, {}, None)

    def test_failed_or_wrong_origin_creation_is_not_adopted(self):
        original = self.step
        for alteration in ['failed', 'wrong-origin']:
            self.fixture = {}
            def step(*args):
                result = original(*args)
                if alteration == 'failed':
                    result['effects']['status']['status'] = 'failure'
                else:
                    result['objectChanges'][0]['objectType'] = 'other::deployment_manager::DeploymentRecord'
                return result
            self.step = step
            with self.assertRaises(RuntimeError):
                self.invoke()
            self.assertNotIn('record', self.fixture)
        self.step = original

    def test_only_explicit_retention_exit_is_scheduling_not_source_failure(self):
        from live_package_upgrade import failure_class
        self.output.returncode = 3
        with self.assertRaises(audit.RetentionPending) as error:
            self.invoke()
        self.assertEqual(failure_class(error.exception), 'infrastructure')
        self.output.returncode = 1
        with self.assertRaises(RuntimeError) as error:
            self.invoke()
        self.assertNotIsInstance(error.exception, audit.RetentionPending)
        self.assertEqual(failure_class(error.exception), 'source')
        self.assertEqual(len(self.calls), 2)


class CheckpointedMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cache.json'
        self.fixture = {'pending_operation': {'label': 'stage', 'tx': {'digest': 'digest'}}}
        self.state = {'fixtures': {'key': {'rollout_audit': self.fixture}}}
        self.args = dict(client=None, sign=None, mutate=lambda *a, **k: self.fail('must not submit'),
            ledger=SimpleNamespace(evidence=[]), operator='operator', chain=None)

    def run_step(self):
        return audit.checkpointed_step(self.path, self.state, self.fixture,
            'stage', ['--move-call', 'package::module::function'], ['package::module::function'], **self.args)

    def test_successful_effects_adopted_without_submission(self):
        result = {'digest': 'digest', 'effects': {'status': {'status': 'success'}}}
        with patch.object(audit.reconciler, 'reconcile', return_value=('executed', result)):
            self.assertEqual(self.run_step(), result)
        self.assertNotIn('pending_operation', self.fixture)

    def test_unknown_or_tampered_result_retains_marker(self):
        for message in ['outcome not indexed', 'tampered provenance']:
            with self.subTest(message=message), patch.object(audit.reconciler, 'reconcile', side_effect=audit.reconciler.Uncertain(message)):
                with self.assertRaisesRegex(audit.reconciler.Uncertain, message):
                    self.run_step()
                self.assertEqual(json.loads(self.path.read_text())['fixtures']['key']['rollout_audit']['pending_operation']['tx']['digest'], 'digest')

    def test_missing_chain_result_never_resubmits_unknown_bytes(self):
        def reconciliation(marker, **kw):
            return 'executed', kw['resubmit']({'digest': 'digest'})
        with patch.object(audit.reconciler, 'reconcile', side_effect=reconciliation):
            with self.assertRaisesRegex(audit.reconciler.Uncertain, 'not indexed'):
                self.run_step()
        self.assertIn('pending_operation', self.fixture)

    def test_changed_stage_never_reconciles_wrong_intent(self):
        self.fixture['pending_operation']['label'] = 'another-stage'
        with patch.object(audit.reconciler, 'reconcile', side_effect=AssertionError('must not reconcile')):
            with self.assertRaisesRegex(audit.reconciler.Uncertain, 'differs'):
                self.run_step()

    def test_new_stage_uses_exact_persisted_bytes_and_full_bounded_budget(self):
        self.fixture.clear()
        persisted = []
        def submitter(client, ptb, **kw):
            def submit(budget):
                self.assertEqual(budget, 20_000_000)
                kw['persist']({'digest': 'signed-identity', 'gas_budget': budget})
                on_disk = json.loads(self.path.read_text())
                self.assertEqual(on_disk['fixtures']['key']['rollout_audit']['pending_operation']['tx']['digest'], 'signed-identity')
                persisted.append(True)
                return {'digest': 'signed-identity'}
            return submit
        def mutate(label, submit, **kw):
            self.assertTrue(kw['require_full'])
            return submit(kw['gas'])
        self.args['mutate'] = mutate
        with patch.object(audit.toolchain, 'ptb_submitter', side_effect=submitter):
            self.assertEqual(self.run_step()['digest'], 'signed-identity')
        self.assertEqual(persisted, [True])

    def test_pending_reservation_is_held_once_and_refuses_bad_owner(self):
        from live_package_upgrade import held_reservations
        operator = '0x' + '1' * 64
        self.fixture.update(binding={'operator': operator})
        self.fixture['pending_operation']['tx']['gas_budget'] = 20_000_000
        self.state['fixtures']['alias'] = self.state['fixtures']['key']
        self.assertEqual(held_reservations(self.state, operator), 20_000_000)
        with self.assertRaisesRegex(RuntimeError, 'another operator'):
            held_reservations(self.state, '0x' + '2' * 64)
        self.fixture['pending_operation']['tx']['gas_budget'] = 100_000_000
        with self.assertRaisesRegex(RuntimeError, 'gas reservation'):
            held_reservations(self.state, operator)
