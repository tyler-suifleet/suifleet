"""Offline orchestration regressions; no wallet, RPC, or remote mutations."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rollout_audit_acceptance as audit


class AuditAcceptanceTests(unittest.TestCase):
    def test_existing_budget_and_cleanup(self):
        transactions = []
        def transaction(label, *args, **kwargs):
            transactions.append((label, args, kwargs))
            return dict(digest='digest', objectChanges=[dict(type='created', objectId='record', objectType='pkg::deployment_manager::DeploymentRecord')])
        manifest = dict(call_package_id='package', type_origins={'deployment_manager::DecisionKey': 'new-origin'}, shared_objects={'registry': {'id': 'registry'}})
        for returncode in (0, 1):
            transactions.clear()
            with tempfile.TemporaryDirectory() as root, patch.object(audit.subprocess, 'run', return_value=SimpleNamespace(returncode=returncode, stdout=json.dumps({'lowest': '11', 'audit': {'complete': True}}), stderr='retention has not advanced')) as run:
                evidence = {'checks': []}
                if returncode:
                    with self.assertRaisesRegex(AssertionError, 'retention'):
                        audit.run(transaction, lambda *args: {'checkpoint': '10'}, manifest, 'device', evidence, root)
                    self.assertNotIn('rollout_audit', evidence)
                else:
                    audit.run(transaction, lambda *args: {'checkpoint': '10'}, manifest, 'device', evidence, root)
                    self.assertEqual(evidence['rollout_audit']['checkpoint'], '10')
                    self.assertEqual(len(evidence['checks']), 1)
                self.assertEqual(len(transactions), 3)
                self.assertTrue(transactions[-1][0].startswith('delete'))
                self.assertTrue(all(t[2]['budget'] == 20_000_000 for t in transactions))
                self.assertEqual(run.call_args.kwargs['timeout'], 330)
                self.assertEqual(json.loads((Path(root) / 'rollout-audit-input.json').read_text())['revision'], 2)

    def test_old_package_skips_without_mutation(self):
        audit.run(lambda *args: self.fail('must not mutate'), None, {'type_origins': {}}, None, {}, None)
