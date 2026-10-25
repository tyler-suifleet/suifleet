"""A pre-spawn repair intent owns recovery, without claiming a live worker."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import dispatcher as d
from coordinator import Store


class PendingRepairTransfer(unittest.TestCase):
    def test_verified_pending_intent_prevents_false_orphan_but_not_ambiguous_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'reviews.sqlite3')
            claim=dict(token='private',key='SF-1',branch='work/test',worktree='/work')
            intent=dict(claim=hashlib.sha256(claim['token'].encode()).hexdigest(),key=claim['key'],
                        branch=claim['branch'],worktree=claim['worktree'],head='head')
            store.db.execute("INSERT INTO jobs(project,kind,status,head,directory,launch_intent) VALUES('test','fix','reserved','head',?,?)",
                             (directory,json.dumps(intent)))
            worker=d.Dispatcher.__new__(d.Dispatcher);worker.state=root
            self.assertTrue(worker.replacement_repair({'id':'test'},claim,{'started':'100'}))
            # An unexpected receipt or a changed claim never proves pre-spawn transfer.
            (root/'process.json').write_text('{}')
            self.assertFalse(worker.replacement_repair({'id':'test'},claim,{'started':'100'}))
            (root/'process.json').unlink()
            self.assertFalse(worker.replacement_repair({'id':'test'},{**claim,'token':'other'},{'started':'100'}))
            store.db.execute('UPDATE jobs SET head=NULL,launch_intent=?',(json.dumps({**intent,'head':None}),))
            self.assertFalse(worker.replacement_repair({'id':'test'},claim,{'started':'100'}))
            store.db.execute('UPDATE jobs SET head=?',('head',))
            # Crossing Popen clears launch_intent. Missing process evidence then
            # remains uncertain and must not be treated as a safe pending launch.
            store.db.execute('UPDATE jobs SET launch_intent=NULL,repair_claim=?',(json.dumps(intent),))
            self.assertFalse(worker.replacement_repair({'id':'test'},claim,{'started':'100'}))

    def test_ci_metadata_does_not_hide_a_fenced_live_same_claim_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'reviews.sqlite3')
            claim=dict(token='private',key='SF-1',branch='work/test',worktree='/work')
            proof=dict(claim=hashlib.sha256(claim['token'].encode()).hexdigest(),key=claim['key'],branch=claim['branch'],
                       worktree=claim['worktree'],head='head',base='base',ci_repair=[{'name':'check'}])
            (root/'process.json').write_text('{"pid":123,"started":"200"}')
            store.db.execute("INSERT INTO jobs(project,kind,status,directory,pid,started,repair_claim) VALUES('test','fix','running',?,123,'200',?)",
                             (directory,json.dumps(proof)))
            worker=d.Dispatcher.__new__(d.Dispatcher);worker.state=root
            with patch.object(d,'process_identity',return_value='200'):
                self.assertTrue(worker.replacement_repair({'id':'test'},claim,{'started':'100'}))
                self.assertFalse(worker.replacement_repair({'id':'test'},{**claim,'branch':'different'},{'started':'100'}))
