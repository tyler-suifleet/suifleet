"""A completed blocked repair must not leave an unowned expiring implementation."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_coordinator import c, fixture


class RepairHandoffs(unittest.TestCase):
    def setup(self, directory, verdict='blocked'):
        root = Path(directory)
        engine = c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']}, 'projects':[]},root)
        pr = {**fixture()[0], 'headRefName':'work/test'}
        owner = dict(key='SF-1',token='private-claim',branch='work/test',worktree=directory,
                     session='s',agent='Codex',model='m',status='in_progress',expired=False,publish_pending=False)
        project = dict(id='test',root=directory)
        result = dict(verdict=verdict,acceptance_complete=False,model='m',evidence='External publisher outage; retained gates.')
        job = engine.store.reserve('test',pr,'Codex','fix',root)
        intent = {k:owner[k] for k in ('key','branch','worktree')}
        intent['claim'] = hashlib.sha256(owner['token'].encode()).hexdigest()
        engine.store.db.execute("UPDATE jobs SET status='done',pid=123,started='start',repair_claim=?,result=? WHERE id=?",
                                (json.dumps(intent),json.dumps(result),job))
        (root/'exit.json').write_text('{"returncode":0}')
        (root/'process.json').write_text('{"pid":123,"started":"start"}')
        (root/'snapshot.json').write_text(json.dumps({'ticket':{'agent':'Codex','model':'m'}}))
        calls = []
        def ticket(project, action, *args):
            calls.append(action)
            if action=='show':return json.dumps(owner)
            if action=='locks':return '[]'
            if action=='heartbeat':owner['expired']=False
            if action=='review':owner['status']='review'
            if action=='publish':owner['publish_pending']=False
            return '{}'
        engine.ticket = ticket
        engine.pr = lambda *args: pr
        return engine,project,owner,calls,job

    def run_handoff(self, engine, project, owner, helpers=(), stopped=True, dirty=False):
        with patch.object(c,'worker_stopped',return_value=stopped), patch.object(c,'claim_processes',return_value=helpers), \
             patch.object(c,'run',side_effect=lambda args,**kwargs:('work/test' if 'branch' in args else (' M keep.txt' if dirty else ''))), \
             patch.object(c.subprocess,'Popen') as spawn:
            engine.reconcile_repair_handoffs(project,[owner])
            spawn.assert_not_called()

    def test_blocked_and_fixed_results_handoff_once_without_approval(self):
        for verdict in ('blocked','fixed'):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as directory:
                engine,p,o,calls,job=self.setup(directory,verdict)
                self.run_handoff(engine,p,o);self.run_handoff(engine,p,o)
                self.assertEqual(o['status'],'review')
                self.assertEqual(calls.count('review'),1)
                row=engine.store.db.execute('SELECT status,result FROM jobs WHERE id=?',(job,)).fetchone()
                self.assertEqual(row['status'],'done')
                self.assertEqual(json.loads(row['result'])['verdict'],verdict)
                self.assertFalse(json.loads(row['result'])['acceptance_complete'])

    def test_expired_but_proven_stopped_claim_is_only_renewed_for_terminal_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            e,p,o,calls,_=self.setup(directory);o['expired']=True
            self.run_handoff(e,p,o)
            self.assertEqual(o['status'],'review')
            self.assertEqual([x for x in calls if x in ('heartbeat','review')],['heartbeat','review'])

    def test_live_helpers_dirty_work_wrong_owner_and_uncertain_exit_never_mutate(self):
        for condition in ('helper','process','dirty','owner','receipt','resource'):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                e,p,o,calls,_=self.setup(directory)
                if condition=='owner':o['token']='replacement'
                if condition=='receipt':(Path(directory)/'exit.json').unlink()
                if condition=='resource':
                    original=e.ticket
                    e.ticket=lambda project,action,*args:json.dumps([{'session':'s','expires':c.time.time()+100}]) if action=='locks' else original(project,action,*args)
                self.run_handoff(e,p,o,helpers=[123] if condition=='helper' else (),stopped=condition!='process',dirty=condition=='dirty')
                self.assertEqual(o['status'],'in_progress')
                self.assertNotIn('review',calls);self.assertNotIn('heartbeat',calls)

    def test_mirror_failure_retries_publication_after_restart_without_replaying_review(self):
        with tempfile.TemporaryDirectory() as directory:
            e,p,o,calls,job=self.setup(directory)
            original=e.ticket
            def failure(project,action,*args):
                value=original(project,action,*args)
                if action=='review':
                    o['publish_pending']=True
                    raise subprocess.CalledProcessError(1,['private'])
                return value
            e.ticket=failure
            self.run_handoff(e,p,o)
            self.assertEqual(o['status'],'review')
            self.assertEqual(e.store.db.execute('SELECT state FROM repair_handoffs').fetchone()[0],'pending')
            restarted=c.Coordinator(e.config,Path(directory));restarted.ticket=original;restarted.pr=e.pr
            self.run_handoff(restarted,p,o)
            self.assertEqual(calls.count('review'),1);self.assertEqual(calls.count('publish'),1)
            self.assertEqual(restarted.store.db.execute('SELECT state FROM repair_handoffs').fetchone()[0],'complete')
