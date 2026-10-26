import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('acceptance_watch', Path(__file__).resolve().parents[1]/'acceptance_watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class Engine:
    def __init__(self, directory):
        self.store = SimpleNamespace(db=sqlite3.connect(':memory:'))
        self.store.db.row_factory = sqlite3.Row
        self.store.db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,project,pr,kind,head,feedback,status)')
        self.owner = dict(status='review', agent='Claude', worktree=str(directory),
                          branch='owner', token='secret-test-token', expired=False, publish_pending=False)
        self.comments, self.launches = [], 0
        (directory/'docs/validation').mkdir(parents=True)
        (directory/'docs/validation/sf082-testnet-native.json').write_text(json.dumps(
            dict(fleet_data=dict(writerId='writer', deviceId='device'))))
        (directory/'docs/validation/sf129-testnet.json').write_text('{}')

    def ticket(self, *args):
        return json.dumps(self.owner)

    def pr(self, *args):
        return dict(state='OPEN', headRefName='owner', headRefOid='head', comments=self.comments[:])

    def gh(self, project, argv):
        self.comments.append(dict(body=argv[-1]))

    def launch(self, project, pr, owner, kind):
        self.launches += 1
        self.store.db.execute('INSERT INTO jobs VALUES(1,?,?,?,?,?,?)',
            ('suifleet', 279, kind, pr['headRefOid'], fingerprint(pr), 'running'))


def fingerprint(pr):
    return json.dumps(pr['comments'], sort_keys=True)


class AcceptanceWatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.engine = Engine(self.directory)
        self.journal = self.directory/'journal.json'
        self.project = dict(id='suifleet', repository='owner/repo')
        self.plan = dict(epoch=563, anchors=[dict(writerId='writer', deviceId='device', endEpoch=563)])
        self.modules = patch.dict(sys.modules, coordinator=SimpleNamespace(feedback=fingerprint))
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.clean = patch.object(watch.subprocess, 'run', return_value=SimpleNamespace(stdout=''))
        self.clean.start()
        self.addCleanup(self.clean.stop)

    def run_watch(self, execute=True):
        return watch.tick(self.engine, self.project, self.journal, 'SF-129', 279, 563,
                          execute, lambda *args: self.plan)

    def test_epoch_and_eligible_anchors_both_required(self):
        for plan in (dict(epoch=562, anchors=[]), dict(epoch=563, anchors=[])):
            self.plan = plan
            self.assertEqual(self.run_watch()['phase'], 'waiting_epoch')
        self.assertEqual(self.engine.launches, 0)
        self.assertFalse(self.engine.comments)

    def test_ready_dispatches_once_across_restart_without_storing_token(self):
        self.assertEqual(self.run_watch()['phase'], 'dispatched')
        self.assertEqual(self.run_watch()['phase'], 'dispatched')
        self.assertEqual(self.engine.launches, 1)
        self.assertEqual(len(self.engine.comments), 1)
        self.assertNotIn(self.engine.owner['token'], self.journal.read_text())

    def test_probe_only_never_comments_or_launches(self):
        self.assertEqual(self.run_watch(False)['phase'], 'ready')
        self.assertFalse(self.engine.comments)
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.run_watch()['phase'], 'dispatched')

    def test_active_or_changed_owner_is_not_resumed(self):
        for status in ('in_progress', 'blocked'):
            self.engine.owner['status'] = status
            self.assertEqual(self.run_watch()['phase'], 'waiting_owner')
        self.engine.owner['status'] = 'review'
        self.engine.store.db.execute("INSERT INTO jobs VALUES(1,'suifleet',279,'review','head','x','reserved')")
        self.assertEqual(self.run_watch()['phase'], 'waiting_job')
        self.assertFalse(self.engine.comments)

    def test_malformed_unrelated_or_unexpired_proofs_do_not_dispatch(self):
        for plan in (dict(epoch=True, anchors=[]), dict(epoch=563),
                     dict(epoch=563, anchors=[dict(writerId='writer', deviceId='other', endEpoch=563)]),
                     dict(epoch=563, anchors=[dict(writerId='writer', deviceId='device', endEpoch=564)])):
            self.plan = plan
            with self.assertRaises(ValueError):
                self.run_watch()
        self.assertEqual(self.engine.launches, 0)

    def test_pending_launch_is_adopted_or_fenced_never_replayed(self):
        base = dict(ticket='SF-129', pr=279, minimum_epoch=563, phase='launching', head='head', feedback='f')
        watch.atomic(self.journal, base)
        self.assertEqual(self.run_watch()['phase'], 'operator_required')
        self.assertEqual(self.engine.launches, 0)
        watch.atomic(self.journal, base)
        self.engine.store.db.execute("INSERT INTO jobs VALUES(1,'suifleet',279,'fix','head','f','reserved')")
        self.assertEqual(self.run_watch()['phase'], 'dispatched')
        self.assertEqual(self.engine.launches, 0)
        self.engine.store.db.execute("UPDATE jobs SET status='failed'")
        watch.atomic(self.journal, base)
        self.assertEqual(self.run_watch()['phase'], 'operator_required')
        self.assertEqual(self.engine.launches, 0)

    def test_changed_journal_condition_refused(self):
        watch.atomic(self.journal, dict(ticket='other', pr=279, minimum_epoch=563, phase='dispatched'))
        with self.assertRaises(ValueError):
            self.run_watch()

    def test_dirty_tree_or_wrong_branch_does_not_launch(self):
        self.clean.stop()
        with patch.object(watch.subprocess, 'run', return_value=SimpleNamespace(stdout=' M x')):
            with self.assertRaisesRegex(ValueError, 'dirty'):
                self.run_watch()
        self.clean.start()
        with patch.object(self.engine, 'pr', return_value=dict(state='OPEN', headRefName='other')):
            with self.assertRaisesRegex(ValueError, 'ownership'):
                self.run_watch()
        self.assertEqual(self.engine.launches, 0)

    def test_existing_evidence_requests_no_worker_and_makes_no_approval(self):
        (self.directory/'docs/validation/sf129-testnet.json').write_text('{"reclaim":{"indices":[0]}}')
        self.assertEqual(self.run_watch()['phase'], 'evidence_present')
        self.assertEqual(self.engine.launches, 0)

    def test_read_only_probe_has_no_wallet_or_write_arguments(self):
        fixture = dict(fleet_data={key: 'x' for key in ('schema','registryId','packageId',
            'typeOrigin','fleetId','budgetId','storageId','deviceId')}, packages=dict(storage=dict(package='storage')))
        fixture['fleet_data']['chainIdentifier'] = '4c78adac'
        with patch.object(watch.subprocess, 'run', return_value=SimpleNamespace(stdout='{"epoch":562,"anchors":[]}')) as run:
            watch.probe(self.directory, fixture)
            args, kwargs = run.call_args
            self.assertEqual(args[0][0], 'node')
            self.assertNotIn('--wallet', args[0])
            self.assertNotIn('--execute', args[0])
            self.assertEqual(kwargs['timeout'], 60)
        fixture['fleet_data']['chainIdentifier'] = 'mainnet'
        with self.assertRaises(ValueError):
            watch.probe(self.directory, fixture)

    def test_cli_error_does_not_disclose_tokens_or_erase_launch_fence(self):
        config = self.directory/'config.json'
        config.write_text('{"projects":[{"id":"suifleet"}]}')
        state = self.directory/'state'
        state.mkdir()
        journal = state/'sf129-expiry-watch.json'
        watch.atomic(journal, dict(phase='launching', ticket='SF-129', pr=279, minimum_epoch=563))
        def fail(*args):
            raise RuntimeError('secret-test-token')
        out = io.StringIO()
        argv = ['watch', '--config', str(config), '--state', str(state), '--runtime', str(self.directory)]
        with patch.dict(sys.modules, coordinator=SimpleNamespace(Coordinator=fail)), patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                watch.main()
        self.assertNotIn('secret-test-token', out.getvalue())
        self.assertEqual(json.loads(journal.read_text())['phase'], 'launching')
