"""Offline CI failure dispatch, durable bounds and unchanged approval gates."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_coordinator as previous
from test_coordinator import c, fixture


def failure(run=1, **fields):
    return dict(__typename='CheckRun', name='32-bit Linux x86 runtime',
                workflowName='CI', status='COMPLETED', conclusion='FAILURE',
                detailsUrl=f'https://github.com/owner/repo/actions/runs/{run}/job/{run}',
                startedAt=f'2026-10-22T10:{run:02d}:00Z',
                completedAt=f'2026-10-22T10:{run:02d}:30Z', **fields)


class CIRepairs(unittest.TestCase):
    def setup(self, directory):
        pr = fixture()[0]
        pr['statusCheckRollup'] = [failure()]
        engine, project, owner = previous.CheckFreshnessTests().setup_scan(directory, pr)
        owner.update(model='gpt-6.1-sol', expired=False)
        return engine, project, pr, owner

    def reserve(self, engine, project, pr):
        repair = {**pr, '_ci_repair': c.failed_checks(pr)}
        return engine.store.reserve(project['id'], repair, 'Codex', 'fix', Path('unused'))

    def finish(self, engine):
        engine.store.db.execute("UPDATE jobs SET status='done' WHERE kind='fix'")

    def test_pending_to_failed_launch_once_restart_and_full_feedback_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, owner = self.setup(d)
            pr['statusCheckRollup'][0].update(status='IN_PROGRESS', conclusion=None)
            fingerprint = c.feedback(pr)
            with patch.object(engine, 'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()
            pr['statusCheckRollup'][0].update(status='COMPLETED', conclusion='FAILURE')
            self.assertEqual(c.feedback(pr), fingerprint)
            launched = []
            def launch(project, current, owner, kind):
                self.assertEqual(kind, 'fix')
                self.assertEqual(current['comments'], pr['comments'])
                self.assertEqual(current['reviews'], pr['reviews'])
                self.assertEqual(current['threads'], pr['threads'])
                job = self.reserve(engine, project, current)
                if job:
                    launched.append(job)
            with patch.object(engine, 'launch', side_effect=launch), patch.object(engine, 'merge') as merge:
                engine.tick(True)
                engine.tick(True)
                merge.assert_not_called()
            self.finish(engine)
            engine = c.Coordinator(engine.config, Path(d))
            previous.CheckFreshnessTests().configure_scan(engine, pr, owner)
            with patch.object(engine, 'launch', side_effect=launch):
                engine.tick(True)
            self.assertEqual(len(launched), 1)
            self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 1)

    def test_backoff_three_attempt_cap_and_other_pr_progress(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, _ = self.setup(d)
            with patch.object(c.time, 'time', return_value=1000):
                self.assertIsNotNone(self.reserve(engine, project, pr))
            self.finish(engine)
            pr['statusCheckRollup'] = [failure(2)]
            with patch.object(c.time, 'time', return_value=1299):
                self.assertIsNone(self.reserve(engine, project, pr))
            for run, now in [(2, 1300), (3, 1900)]:
                pr['statusCheckRollup'] = [failure(run)]
                with patch.object(c.time, 'time', return_value=now):
                    self.assertIsNotNone(self.reserve(engine, project, pr))
                self.finish(engine)
            pr['statusCheckRollup'] = [failure(4)]
            with patch.object(c.time, 'time', return_value=999999):
                self.assertIsNone(self.reserve(engine, project, pr))
                repair = {**pr, '_ci_repair': c.failed_checks(pr)}
                self.assertIn('limit reached', engine.store.ci_repair_blocker('test', repair))
                self.assertIsNotNone(self.reserve(engine, project, {**pr, 'number':50}))

    def test_new_head_or_base_new_scope_same_run_no_timestamp_churn(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, _ = self.setup(d)
            self.assertIsNotNone(self.reserve(engine, project, pr))
            self.finish(engine)
            pr['statusCheckRollup'][0]['completedAt'] = 'later'
            self.assertIsNone(self.reserve(engine, project, pr))
            for field in ('headRefOid', 'baseRefOid'):
                pr[field] = 'new-' + field
                with patch.object(c.time, 'time', return_value=9999999999 if field == 'headRefOid' else 19999999999):
                    self.assertIsNotNone(self.reserve(engine, project, pr))
                self.finish(engine)

    def test_cancelled_pending_unavailable_and_superseded_do_not_launch(self):
        check = failure()
        for changes in ({'status':'IN_PROGRESS', 'conclusion':None},
                        {'conclusion':'CANCELLED'}, {'conclusion':'STARTUP_FAILURE'},
                        {'conclusion':'ACTION_REQUIRED'}, {'detailsUrl':None},
                        {'__typename':'StatusContext', 'state':'ERROR'}):
            self.assertEqual(c.failed_checks({'statusCheckRollup':[{**check, **changes}]}), [])
        for state in ('IN_PROGRESS', 'COMPLETED'):
            newest = {**failure(2), 'status':state, 'conclusion':'SUCCESS'}
            self.assertEqual(c.failed_checks({'statusCheckRollup':[check, newest]}), [])
            self.assertEqual(c.failed_checks({'statusCheckRollup':[newest, check]}), [])

    def test_missing_owner_draft_fork_active_expired_and_unpublished_never_launch(self):
        with tempfile.TemporaryDirectory() as d:
            engine, _, pr, owner = self.setup(d)
            for changes, ownership in [({'isDraft':True}, owner), ({'state':'CLOSED'}, owner),
                                       ({}, {**owner,'status':'in_progress'}),
                                       ({}, {**owner,'expired':True}), ({}, {**owner,'model':''})]:
                previous.CheckFreshnessTests().configure_scan(engine, {**pr, **changes}, ownership)
                with patch.object(engine,'launch') as launch:
                    engine.tick(True)
                    launch.assert_not_called()
            previous.CheckFreshnessTests().configure_scan(engine, pr, owner)
            engine.owners = lambda project: []
            with patch.object(engine,'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()
            previous.CheckFreshnessTests().configure_scan(engine, {**pr,'isCrossRepository':True}, owner)
            with patch.object(engine,'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()
            previous.CheckFreshnessTests().configure_scan(engine, pr, {**owner,'publish_pending':True})
            with patch.object(engine,'retry_review_publication',return_value=None), patch.object(engine,'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()

    def test_live_review_or_repair_reservation_blocks_duplicate(self):
        for kind in ('review', 'fix'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                engine, project, pr, _ = self.setup(d)
                first = engine.store.reserve('test', {**pr,'comments':[{'id':'x','body':'x'}]},
                                             'Claude', kind, Path(d))
                self.assertIsNotNone(first)
                self.assertIsNone(self.reserve(engine, project, pr))
                self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 0)

    def test_failed_run_visible_in_read_only_scan_without_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            engine, _, pr, _ = self.setup(d)
            with patch('sys.stdout', new_callable=io.StringIO) as out, patch.object(engine,'launch') as launch:
                engine.tick(False)
                launch.assert_not_called()
            row = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(row['ci_failures'][0]['detailsUrl'], failure()['detailsUrl'])
            self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 0)

    def test_owner_race_never_resumes_or_spawns(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, owner = self.setup(d)
            owner.update(worktree=d, session='original')
            repair = {**pr, '_ci_repair':c.failed_checks(pr)}
            commands = []
            def ticket(project, command, *args):
                commands.append(command)
                return json.dumps({**owner,'agent':'Claude'})
            with patch.object(engine,'ticket',side_effect=ticket), patch.object(c.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError,'ownership changed'):
                    engine.launch(project, repair, owner, 'fix')
                spawn.assert_not_called()
            self.assertEqual(commands, ['show'])
            self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 1)

    def test_failed_resume_mirror_retains_ci_evidence_and_asserts_on_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, owner = self.setup(d)
            owner.update(worktree=d, session='original', evidence='')
            current = dict(owner)
            commands = []
            def ticket(project, command, *args):
                commands.append(command)
                if command == 'show': return json.dumps(current)
                if command == 'resume':
                    current.update(status='in_progress', publish_pending=True)
                    raise RuntimeError('mirror unavailable')
                if command == 'publish': current['publish_pending'] = False
                return ''
            def run(args, **kwargs):
                if args[:2] == ['git','status']: return ''
                if args[:2] == ['git','branch']: return owner['branch']
                if args[:2] == ['git','rev-parse']: return pr['headRefOid']
                return 'fixture-auth'
            project['github_account'] = 'fixture'
            engine.ticket = ticket
            engine.pr = lambda *args: pr
            engine.publish_results = lambda *args: None
            repair = {**pr, '_ci_repair': c.failed_checks(pr)}
            with patch.object(c,'run',side_effect=run), patch.object(c.subprocess,'Popen') as spawn, patch.object(c,'process_identity',return_value='identity'):
                with self.assertRaisesRegex(RuntimeError,'mirror unavailable'):
                    engine.launch(project, repair, owner, 'fix')
                spawn.assert_not_called()
                engine.store.db.execute("UPDATE jobs SET retry_after=0 WHERE kind='fix'")
                spawn.return_value.pid = 123
                # setup_scan mocks collection; invoke the real recovery path.
                c.Coordinator.collect(engine, project, [])
                spawn.assert_called_once()
                row = engine.store.db.execute("SELECT * FROM jobs WHERE kind='fix'").fetchone()
                snapshot = json.loads((Path(row['directory'])/'snapshot.json').read_text())
                self.assertEqual(snapshot['_ci_repair'], repair['_ci_repair'])
                self.assertEqual(commands.count('resume'), 1)
                self.assertEqual(commands.count('publish'), 1)
                self.assertIn('assert', commands)
                self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 1)

    def test_budget_spans_new_heads_until_checks_pass(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, _ = self.setup(d)
            for run, now in [(1,1000),(2,1300),(3,1900)]:
                pr['headRefOid'] = str(run) * 40
                pr['statusCheckRollup'] = [failure(run)]
                with patch.object(c.time,'time',return_value=now):
                    self.assertIsNotNone(self.reserve(engine,project,pr))
                self.finish(engine)
            pr['headRefOid'] = '4' * 40
            with patch.object(c.time,'time',return_value=999999):
                self.assertIsNone(self.reserve(engine,project,pr))
            pr['statusCheckRollup'][0]['conclusion'] = 'SUCCESS'
            with patch.object(engine,'launch'):
                engine.tick(True)
            pr['statusCheckRollup'] = [failure(4)]
            self.assertIsNotNone(self.reserve(engine,project,pr))

    def test_changed_failure_before_launch_never_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, owner = self.setup(d)
            owner.update(worktree=d, session='original')
            engine.ticket = lambda *args: json.dumps(owner)
            engine.pr = lambda *args: {**pr, 'statusCheckRollup':[failure(2)]}
            repair = {**pr, '_ci_repair':c.failed_checks(pr)}
            with patch.object(c.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError,'evidence changed'):
                    engine.launch(project, repair, owner, 'fix')
                spawn.assert_not_called()
            self.assertEqual(engine.store.db.execute("SELECT status FROM jobs WHERE kind='fix'").fetchone()[0], 'failed')

    def test_two_connections_reserve_only_one_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            pr = fixture()[0]
            pr['_ci_repair'] = [failure()]
            path = Path(d)/'race.sqlite3'
            c.Store(path).db.close()
            barrier = threading.Barrier(2)
            results, errors = [], []
            def reserve():
                try:
                    store = c.Store(path)
                    barrier.wait()
                    results.append(store.reserve('test',pr,'Codex','fix',Path(d)))
                    store.db.close()
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=reserve) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
            self.assertFalse(errors)
            self.assertEqual(sum(result is not None for result in results), 1)
            store = c.Store(path)
            self.assertEqual(store.db.execute('SELECT attempts FROM ci_repair_budget').fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM ci_repairs').fetchone()[0], 1)

    def test_pending_and_cancelled_do_not_erase_reserved_failure(self):
        with tempfile.TemporaryDirectory() as d:
            engine, project, pr, _ = self.setup(d)
            self.assertIsNotNone(self.reserve(engine, project, pr))
            self.finish(engine)
            for conclusion in (None, 'CANCELLED', 'STARTUP_FAILURE'):
                pr['statusCheckRollup'][0].update(conclusion=conclusion)
                with patch.object(engine,'launch') as launch:
                    engine.tick(True)
                    launch.assert_not_called()
            pr['statusCheckRollup'] = [failure()]
            self.assertIsNone(self.reserve(engine, project, pr))


if __name__ == '__main__':
    unittest.main()
