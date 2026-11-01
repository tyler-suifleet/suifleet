import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordinator as c
from batch_integration import BatchController


class BatchRecoveryMode(unittest.TestCase):
    def engine(self, root):
        engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},root/'state')
        engine.batches.installed=root/'installed.json'
        engine.batches.git=Mock(return_value='')
        return engine

    def project(self):
        return dict(id='p',repository='a/b',root='/repo',integration_batch=dict(
            enabled=True,suspend_until_installed='a'*40,suspension_reason='bootstrap repair'))

    def test_pending_installed_revision_resumes_only_after_real_ancestry_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=self.engine(root);project=self.project()
            engine.batches.installed.write_text(json.dumps({'protocol':1,'revision':'b'*40}))
            engine.batches.git.side_effect=subprocess.CalledProcessError(1,['git'])
            self.assertFalse(engine.batches.enabled(project))
            engine.batches.installed.write_text(json.dumps({'protocol':1,'revision':'c'*40}))
            engine.batches.git.side_effect=None
            self.assertTrue(engine.batches.enabled(project))
            engine.batches.git.assert_called_with(project,'--no-replace-objects','merge-base','--is-ancestor','a'*40,'c'*40)
            calls=engine.batches.git.call_count
            self.assertTrue(engine.batches.enabled(project));self.assertEqual(engine.batches.git.call_count,calls)
            project['integration_batch']['suspend_until_installed']='d'*40
            engine.batches.git.side_effect=subprocess.CalledProcessError(1,['git'])
            self.assertFalse(engine.batches.enabled(project))

    def test_missing_malformed_unrelated_or_unreadable_installation_stays_paused(self):
        values=[None,'{bad',json.dumps({'revision':'b'*40}),json.dumps({'protocol':1,'revision':'bad'}),
                json.dumps({'protocol':2,'revision':'b'*40}),json.dumps({'protocol':True,'revision':'b'*40}),json.dumps([])]
        for value in values:
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);engine=self.engine(root)
                if value is not None:engine.batches.installed.write_text(value)
                self.assertFalse(engine.batches.enabled(self.project()))
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));engine.batches.installed.write_text(json.dumps({'protocol':1,'revision':'b'*40}))
            engine.batches.git.side_effect=subprocess.CalledProcessError(128,['git'])
            self.assertFalse(engine.batches.enabled(self.project()))

    def test_explicit_disable_never_resumes_and_normal_mode_does_not_need_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));project=self.project();project['integration_batch']['enabled']=False
            self.assertFalse(engine.batches.enabled(project));engine.batches.git.assert_not_called()
            project['integration_batch']={'enabled':True}
            self.assertTrue(engine.batches.enabled(project));engine.batches.git.assert_not_called()

    def test_bad_temporary_intent_fails_closed(self):
        for field,value in [('suspend_until_installed','main'),('suspend_until_installed',True),
                            ('suspension_reason',''),('suspension_reason','x'*501)]:
            with self.subTest(field=field,value=value),tempfile.TemporaryDirectory() as tmp:
                engine=self.engine(Path(tmp));project=self.project();project['integration_batch'][field]=value
                with self.assertRaises(ValueError):engine.batches.enabled(project)

    def test_real_git_ancestry_not_equal_strings_or_branch_tips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=root/'repo';repo.mkdir();engine=self.engine(root)
            def git(*args):
                return subprocess.check_output(['git',*args],cwd=repo,text=True,stderr=subprocess.DEVNULL).strip()
            git('init','-q');git('config','user.name','Tyler Bush');git('config','user.email','tyler.jonathan.bush@gmail.com')
            git('commit','--allow-empty','-qm','base');base=git('rev-parse','HEAD')
            git('commit','--allow-empty','-qm','repair');repair=git('rev-parse','HEAD')
            git('commit','--allow-empty','-qm','installed merge');installed=git('rev-parse','HEAD')
            project=self.project();project['root']=str(repo);project['integration_batch']['suspend_until_installed']=repair
            engine.batches.git=lambda _,*args:git(*args)
            engine.batches.installed.write_text(json.dumps({'protocol':1,'revision':base}))
            self.assertFalse(engine.batches.enabled(project))
            # Merely having the repair in the checkout does not authorize resume.
            self.assertEqual(git('rev-parse','HEAD'),installed)
            engine.batches.installed.write_text(json.dumps({'protocol':1,'revision':installed}))
            self.assertTrue(engine.batches.enabled(project))

    def test_temporary_pause_freezes_everything_except_exact_repair_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));project=self.project()
            self.assertEqual(engine.batches.pause_status(project,{'number':1,'headRefOid':'b'*40}),'waiting-installed-repair')
            self.assertIsNone(engine.batches.pause_status(project,{'number':2,'headRefOid':'a'*40}))

    def test_explicit_disable_fences_live_batch_members_and_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));project=self.project();project['integration_batch']={'enabled':False}
            engine.store.db.execute('INSERT INTO integration_batches(project,id,status,pr,members,created,updated) VALUES(?,?,?,?,?,?,?)',
                                    ('p','batch','awaiting_ci',99,json.dumps([{'pr':1}]),1,1))
            for number in (1,99):
                self.assertEqual(engine.batches.pause_status(project,{'number':number}),'disabled-with-active-batch')
            self.assertIsNone(engine.batches.pause_status(project,{'number':2}))
            engine.store.db.execute("UPDATE integration_batches SET status='invalidated'")
            self.assertIsNone(engine.batches.pause_status(project,{'number':1}))

    def test_disabled_mode_still_writes_fresh_public_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));project=self.project();project['integration_batch']={'enabled':False}
            pr=dict(number=1,headRefOid='a'*40,headRefName='work/source',baseRefOid='b'*40,baseRefName='main',state='OPEN',
                    isDraft=False,mergeable='MERGEABLE',statusCheckRollup=[])
            threads={'data':{'repository':{'pullRequest':{'reviewThreads':{'nodes':[], 'pageInfo':{'hasNextPage':False}}}}}}
            engine.gh=Mock(side_effect=[json.dumps(pr),json.dumps({'object':{'sha':'b'*40}}),json.dumps(threads),'[[]]','[[]]'])
            engine.immutable_read=Mock(side_effect=[{'merge_base_commit':{'sha':'b'*40}},{'tree':[]}])
            engine.observe_pr=Mock()
            value=engine.pr(project,1)
            engine.observe_pr.assert_called_once_with(project,value)

    def test_no_fallback_rebase_or_review_launch_for_frozen_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=self.engine(Path(tmp));project=self.project()
            pr=dict(number=1,headRefOid='b'*40,headRefName='work/source',baseRefOid='c'*40,baseRefName='main',state='OPEN',
                    isDraft=False,baseIncluded=False,mergeable='MERGEABLE',statusCheckRollup=[],reviews=[],comments=[],threads=[])
            owner=dict(key='T1',branch='work/source',status='review',agent='Codex',publish_pending=False)
            engine.owners=Mock(return_value=[owner]);engine.collect=Mock();engine.discover_merged=Mock()
            engine.prs=Mock(return_value=[pr]);engine.launch=Mock();engine.merge=Mock()
            engine.tick_project(project,True)
            engine.launch.assert_not_called();engine.merge.assert_not_called()




if __name__=='__main__':unittest.main()
