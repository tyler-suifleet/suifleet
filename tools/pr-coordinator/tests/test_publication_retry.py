"""Pending Review ownership publication is retried before PR scheduling."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_coordinator import c, fixture


def failure(stderr):
    return subprocess.CalledProcessError(1, ['ticket', 'publish'], stderr=stderr)


class PendingReviewPublication(unittest.TestCase):
    def scan(self, directory, prs, owners, publish=None):
        """Wire a real Coordinator to in-memory ticket state and recorded launches."""
        project = {'id': 'test', 'root': directory, 'repository': 'owner/repo'}
        engine = c.Coordinator({'workers': {'Codex': ['true'], 'Claude': ['true']}, 'projects': [project]}, Path(directory))
        state = {owner['key']: copy.deepcopy(owner) for owner in owners}
        calls, launched = [], []
        def ticket(project, command, *args, timeout=None):
            calls.append((command, *args))
            if command == 'show': return json.dumps(state[args[0]])
            if command == 'publish':
                if publish: publish(state[args[0]])
                ticket_state = state[args[0]]
                ticket_state.update(publish_pending=False, published_revision=ticket_state['revision'])
                return json.dumps(ticket_state)
            raise AssertionError(f'unexpected ticket mutation {command}')
        engine.ticket = ticket
        engine.owners = lambda project: [copy.deepcopy(value) for value in state.values()]
        engine.collect = lambda *args: None
        engine.discover_merged = lambda *args: None
        engine.prs = lambda project: prs
        engine.ensure_closing_reference = lambda *args: None
        engine.launch = lambda project, pr, owner, kind: launched.append((pr['number'], kind, copy.deepcopy(owner)))
        return engine, state, calls, launched

    def pending(self, key='T1', branch='work/t1', number=49, **changes):
        pr, owner, _ = fixture()
        pr.update(number=number, headRefName=branch, baseRefName='main', title='test')
        owner.update({'key': key, 'token': 'private-'+key, 'agent': 'Codex', 'model': 'test', 'session': 's-'+key,
                      'branch': branch, 'worktree': '/tmp/'+key, 'spec': {'priority': 'P1'},
                      'revision': 11, 'published_revision': 10, 'publish_pending': True, **changes})
        return pr, owner

    def test_successful_publication_rereads_exact_ownership_before_scheduling(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            engine, state, calls, launched = self.scan(d, [pr], [owner])
            engine.tick(True)
            self.assertEqual(calls, [('show', 'T1'), ('publish', 'T1'), ('show', 'T1')])
            self.assertEqual(len(launched), 1)
            number, kind, scheduled = launched[0]
            self.assertEqual((number, kind), (49, 'review'))
            self.assertFalse(scheduled['publish_pending'])
            self.assertEqual((scheduled['token'], scheduled['branch'], scheduled['revision']), ('private-T1', 'work/t1', 11))
            self.assertEqual(state['T1']['status'], 'review')
            self.assertIsNone(engine.store.db.execute('SELECT * FROM ownership_publication').fetchone())

    def test_failed_publication_backs_off_preserves_claim_and_sanitizes(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            def broken(ticket_state):
                raise failure('HTTP 401 Bad credentials token=ghp_secret')
            engine, state, calls, launched = self.scan(d, [pr], [owner], publish=broken)
            with patch.object(c.time, 'time', return_value=1000.0), patch('builtins.print') as output:
                engine.tick(True)
            self.assertEqual(launched, [])
            self.assertTrue(state['T1']['publish_pending'])
            self.assertEqual((state['T1']['token'], state['T1']['status']), ('private-T1', 'review'))
            row = engine.store.db.execute('SELECT * FROM ownership_publication').fetchone()
            self.assertEqual((row['attempts'], row['retry_after'], row['revision']), (1, 1060.0, 11))
            self.assertEqual(row['error'], 'failed (exit 1)')
            self.assertNotIn('private-T1', row['claim'])
            logged = ' '.join(str(arg) for call in output.call_args_list for arg in call.args)
            self.assertNotIn('ghp_secret', logged); self.assertNotIn('private-T1', logged)
            self.assertIn('claim retained', logged)
            # Backoff is persisted across restart and no publish is attempted early.
            engine.store = c.Store(Path(d)/'reviews.sqlite3')
            calls.clear()
            with patch.object(c.time, 'time', return_value=1059.0): engine.tick(True)
            self.assertNotIn(('publish', 'T1'), calls)
            with patch.object(c.time, 'time', return_value=1061.0): engine.tick(True)
            self.assertEqual(calls.count(('publish', 'T1')), 1)
            row = engine.store.db.execute('SELECT * FROM ownership_publication').fetchone()
            self.assertEqual((row['attempts'], row['retry_after']), (2, 1061.0 + 120))
            self.assertEqual(launched, [])

    def test_backoff_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            def broken(ticket_state): raise failure('HTTP 422')
            engine, state, calls, launched = self.scan(d, [pr], [owner], publish=broken)
            now = 0.0
            for _ in range(12):
                now += 10_000
                with patch.object(c.time, 'time', return_value=now): engine.tick(True)
            row = engine.store.db.execute('SELECT * FROM ownership_publication').fetchone()
            self.assertEqual(row['attempts'], 12)
            self.assertEqual(row['retry_after'] - now, 3600)

    def test_transient_publication_failure_retries_once_in_scan(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            attempts = []
            def flaky(ticket_state):
                attempts.append(1)
                if len(attempts) == 1: raise failure('connection reset by peer')
            engine, state, calls, launched = self.scan(d, [pr], [owner], publish=flaky)
            with patch.object(c.time, 'sleep') as sleep:
                engine.tick(True)
            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once()
            self.assertEqual([entry[0] for entry in launched], [49])
            # Persistent transient failure is still bounded to two attempts per scan.
            pr2, owner2 = self.pending('T2', 'work/t2', 50)
            def down(ticket_state): raise failure('connection reset by peer')
            engine, state, calls, launched = self.scan(d, [pr2], [owner2], publish=down)
            with patch.object(c.time, 'sleep'):
                engine.tick(True)
            self.assertEqual(calls.count(('publish', 'T2')), 2)
            self.assertEqual(launched, [])
            self.assertEqual(engine.store.db.execute("SELECT error FROM ownership_publication WHERE ticket='T2'").fetchone()[0], 'transient (exit 1)')

    def test_ownership_change_before_publication_is_not_published(self):
        for change in ({'revision': 12}, {'token': 'replacement'}, {'status': 'in_progress'},
                       {'branch': 'other'}, {'worktree': '/elsewhere'}, {'session': 'other'},
                       {'expired': True}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                pr, owner = self.pending()
                engine, state, calls, launched = self.scan(d, [pr], [owner])
                listed = copy.deepcopy(owner)
                engine.owners = lambda project: [copy.deepcopy(listed)]
                state['T1'].update(change)
                engine.tick(True)
                self.assertNotIn(('publish', 'T1'), calls)
                self.assertEqual(launched, [])
                self.assertTrue(state['T1']['publish_pending'])
                self.assertIsNone(engine.store.db.execute('SELECT * FROM ownership_publication').fetchone())

    def test_ownership_change_during_publication_defers_scheduling(self):
        for change in ({'revision': 12}, {'token': 'replacement'}, {'status': 'in_progress'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                pr, owner = self.pending()
                engine, state, calls, launched = self.scan(d, [pr], [owner], publish=lambda value: value.update(change))
                engine.tick(True)
                self.assertEqual(calls.count(('publish', 'T1')), 1)
                self.assertEqual(launched, [])

    def test_failing_ticket_does_not_block_unrelated_ready_pr(self):
        with tempfile.TemporaryDirectory() as d:
            bad_pr, bad = self.pending('T1', 'work/t1', 49, spec={'priority': 'P0'})
            good_pr, good = self.pending('T2', 'work/t2', 50, publish_pending=False, published_revision=11)
            def publish(ticket_state):
                if ticket_state['key'] == 'T1': raise failure('HTTP 502')
            engine, state, calls, launched = self.scan(d, [bad_pr, good_pr], [bad, good], publish=publish)
            with patch.object(c.time, 'sleep'):
                engine.tick(True)
            self.assertEqual([entry[0] for entry in launched], [50])
            self.assertNotIn(('publish', 'T2'), calls)
            self.assertTrue(state['T1']['publish_pending'])

    def test_active_implementation_and_dry_run_are_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending(status='in_progress')
            engine, state, calls, launched = self.scan(d, [pr], [owner])
            engine.tick(True)
            self.assertEqual(calls, [])
            self.assertEqual(launched, [])
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            engine, state, calls, launched = self.scan(d, [pr], [owner])
            engine.tick(False)
            self.assertEqual(calls, [])
            self.assertTrue(state['T1']['publish_pending'])

    def test_new_revision_restarts_bounded_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            pr, owner = self.pending()
            def broken(ticket_state): raise failure('HTTP 401')
            engine, state, calls, launched = self.scan(d, [pr], [owner], publish=broken)
            with patch.object(c.time, 'time', return_value=1000.0): engine.tick(True)
            state['T1']['revision'] = 12
            calls.clear()
            with patch.object(c.time, 'time', return_value=1001.0): engine.tick(True)
            self.assertEqual(calls.count(('publish', 'T1')), 1)
            row = engine.store.db.execute('SELECT * FROM ownership_publication').fetchone()
            self.assertEqual((row['revision'], row['attempts']), (12, 1))

    def test_failure_classification_is_sanitized(self):
        self.assertEqual(c.publication_failure(subprocess.TimeoutExpired(['ticket'], 300)), 'timeout')
        self.assertEqual(c.publication_failure(failure('CoordinationError: No GitHub publication snapshot; refresh before publishing')),
                         'No GitHub publication snapshot; refresh before publishing')
        self.assertEqual(c.publication_failure(failure('HTTP 429 secondary rate limit')), 'GitHub rate limited')
        self.assertEqual(c.publication_failure(failure('i/o timeout private-detail')), 'transient (exit 1)')
        self.assertEqual(c.publication_failure(ValueError('private-detail')), 'ValueError')


if __name__ == '__main__':
    unittest.main()
