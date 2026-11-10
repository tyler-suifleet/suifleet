"""Manual-session reconciliation retains real process/claim proof; offline only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_integration import LocalInfrastructure


class ManualOriginFence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE jobs(id INTEGER, project TEXT, pr INTEGER, kind TEXT, status TEXT, repair_claim TEXT, directory TEXT, pid INTEGER, started TEXT, family TEXT, result TEXT)')
        self.owner = dict(token='private-test-claim', key='SF-246', branch='work/manual',
                          worktree=None, session='manual', agent='Codex')
        self.project = dict(id='test')
        self.pr = dict(number=617)
        self.c = SimpleNamespace(store=SimpleNamespace(db=self.db), state=self.path,
            batches=SimpleNamespace(reserved_members=lambda p: []),
            assert_review_owner=Mock(), ticket=Mock(return_value='[]'))
        self.lane = LocalInfrastructure(self.c)
        self.directory = self.path/'repair'; self.directory.mkdir()
        self.process = dict(pid=99999999, started='123')
        (self.directory/'process.json').write_text(json.dumps(self.process))
        (self.directory/'exit.json').write_text(json.dumps(dict(returncode=0)))
        claim = dict(claim=hashlib.sha256(self.owner['token'].encode()).hexdigest(),
                     key='SF-246', branch='work/manual', worktree=None)
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (1, 'test', 617, 'fix', 'done', json.dumps(claim), str(self.directory),
             self.process['pid'], '123', 'Codex', json.dumps(dict(verdict='fixed', model='actual-model', evidence='tested fix'))))
        self.helpers = patch('coordinator.claim_processes', return_value=[]).start()
        self.stopped = patch('coordinator.worker_stopped', return_value=True).start()
        self.addCleanup(patch.stopall)

    def fence(self):
        self.lane.fenced(self.project, self.pr, self.owner)

    def test_stopped_same_claim_repair_scans_older_helpers(self):
        self.fence()
        self.helpers.assert_called_once_with('private-test-claim', '0')
        self.stopped.assert_called_once_with(99999999, '123')
        self.c.assert_review_owner.assert_called_once()

    def test_rejects_missing_wrong_claim_and_nonterminal_proof(self):
        for column, value in [('repair_claim', '{}'), ('repair_claim', None),
                              ('status', 'failed'), ('family', 'Claude'),
                              ('result', '{bad'), ('started', 'different'), ('pid', 1)]:
            with self.subTest(column=column, value=value):
                self.db.execute('SAVEPOINT negative')
                self.db.execute(f'UPDATE jobs SET {column}=?', (value,))
                with self.assertRaisesRegex(RuntimeError, 'provenance'):
                    self.fence()
                self.db.execute('ROLLBACK TO negative')
        self.db.execute('DELETE FROM jobs')
        with self.assertRaisesRegex(RuntimeError, 'provenance'): self.fence()

    def test_receipt_missing_failed_or_changed(self):
        for name, content in [('exit.json', '{"returncode":1}'),
                              ('process.json', '{"pid":1,"started":"123"}')]:
            p=self.directory/name; original=p.read_text(); p.write_text(content)
            with self.assertRaisesRegex(RuntimeError, 'provenance'): self.fence()
            p.unlink()
            with self.assertRaisesRegex(RuntimeError, 'provenance'): self.fence()
            p.write_text(original)

    def test_live_unknown_helpers_or_original_process_rejected(self):
        self.stopped.return_value=False
        with self.assertRaisesRegex(RuntimeError, 'not stopped'): self.fence()
        self.stopped.return_value=True
        for helpers in [[dict(pid=4, started='1')], [dict(pid=4, uninspectable=True)]]:
            self.helpers.return_value=helpers
            with self.assertRaisesRegex(RuntimeError, 'not stopped'): self.fence()

    def test_live_lease_and_uncertain_job_rejected(self):
        for lease in [dict(project='test', ticket='SF-246', expired=False),
                      dict(project='test', session='manual', expired=False)]:
            self.c.ticket.return_value=json.dumps([lease])
            with self.assertRaisesRegex(RuntimeError, 'resource lease'): self.fence()
        self.c.ticket.return_value='[]'
        self.db.execute("UPDATE jobs SET status='orphaned'")
        with self.assertRaisesRegex(RuntimeError, 'active or uncertain'): self.fence()

    def test_original_receipt_remains_authoritative(self):
        p=self.path/'implementations/manual/process.json'; p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(pid=10, started='1')))
        self.fence()
        self.stopped.assert_called_once_with(10, '1')
        self.helpers.assert_called_once_with('private-test-claim', '1')
        self.c.ticket.assert_not_called()

    def test_newer_wrong_claim_cannot_use_historical_repair(self):
        self.db.execute('INSERT INTO jobs SELECT 2,project,pr,kind,status,?,directory,pid,started,family,result FROM jobs WHERE id=1', ('{}',))
        with self.assertRaisesRegex(RuntimeError, 'provenance'): self.fence()

    def test_missing_managed_receipt_never_uses_manual_fallback(self):
        for session in ['dispatch-missing', 'recovery-missing']:
            self.owner['session']=session
            with self.assertRaisesRegex(RuntimeError, 'process identity'): self.fence()
        self.owner['session']='manual'
        (self.path/'implementations/manual').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'process identity'): self.fence()
