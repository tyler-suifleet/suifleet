import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_evaluation_reporting import EvaluationRun, LIMIT


class DurableEvaluation(TestCase):
    def test_existing_run_never_resets_or_replays(self):
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(wallet=Path(directory)/'client.yaml', runtime=Path(directory), compiler='sui', network_cli='sui')
            run = EvaluationRun(args)
            run.ledger.grant(123)
            run.pending('publish', dict(digest='unsigned-digest', sender='agent', gas_budget=1000))
            saved = json.loads(run.journal.read_text())
            self.assertEqual(saved['allowance_used_mist'], 123)
            self.assertEqual(saved['limit_mist'], LIMIT)
            with self.assertRaises(RuntimeError):
                EvaluationRun(args)
            self.assertEqual(json.loads(run.journal.read_text()), saved)
            with self.assertRaises(AssertionError):
                run.pending('publish', dict(digest='replacement', sender='agent', gas_budget=1000))

    def test_persistence_failure_prevents_signing(self):
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(wallet=Path(directory)/'client.yaml', runtime=Path(directory), compiler='sui', network_cli='sui')
            run = EvaluationRun(args)
            with patch.object(run, 'save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    run.pending('publish', dict(digest='digest', sender='agent', gas_budget=1000))

    def test_acceptance_continuation_carries_charges_and_rejects_unknown(self):
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(wallet=Path(directory)/'client.yaml', runtime=Path(directory), compiler='sui', network_cli='sui', accept_release=True, journal_native=False)
            prior = dict(limit_mist=LIMIT, chain='4c78adac', release_verified=True,
                         allowance_used_mist=900_000_000, transactions=[], operations={
                             'publish':dict(status='success',digest='known')})
            journal = Path(directory)/'run.json'
            journal.write_text(json.dumps(prior))
            restored = EvaluationRun(args)
            self.assertEqual(restored.ledger.used, 900_000_000)
            self.assertIs(restored.ledger.evidence, restored.evidence['transactions'])
            for change in ({'limit_mist':LIMIT+1}, {'chain':'mainnet'},
                           {'operations':{'publish':dict(status='unsigned_verified',digest='unknown')}},
                           {'acceptance_started':True}):
                journal.write_text(json.dumps({**prior, **change}))
                with self.assertRaises(AssertionError): EvaluationRun(args)
