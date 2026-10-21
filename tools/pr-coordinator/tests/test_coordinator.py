import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('coordinator',Path(__file__).resolve().parents[1]/'coordinator.py')
c = importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


def fixture():
    pr = dict(baseIncluded=True,number=49,headRefOid='a'*40,baseRefOid='b'*40,state='OPEN',isDraft=False,mergeable='MERGEABLE',comments=[],reviews=[],threads=[],statusCheckRollup=[{'__typename':'CheckRun','status':'COMPLETED','conclusion':'SUCCESS'}])
    owner = dict(remote={'url':'https://github.com/owner/repo/issues/1'},status='review',agent='Codex',publish_pending=False)
    review = dict(head=pr['headRefOid'],feedback=c.feedback(pr),family='Claude',result=dict(verdict='approve',acceptance_complete=True,model='test',evidence='verified'))
    return pr,owner,review


class Gates(unittest.TestCase):
    def test_public_testnet_requires_explicit_validated_project_grant(self):
        self.assertIn('not authorized',c.network_authorization({}))
        grant=dict(network='testnet',chain_identifier='4c78adac',wallet='/dedicated/client.yaml',lock_resource='network:suifleet-testnet')
        message=c.network_authorization({'testnet_authorization':grant})
        self.assertIn('automatic public Sui testnet testing only',message)
        self.assertIn('network:suifleet-testnet',message)
        self.assertIn('Mainnet',message)
        for field,value in [('network','mainnet'),('chain_identifier','35834a8a'),('lock_resource','')]:
            with self.subTest(field=field),self.assertRaises(ValueError):
                c.network_authorization({'testnet_authorization':{**grant,field:value}})

    def test_stopped_quarantine_frees_family_but_preserves_pr_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            store=c.Store(Path(directory)/'jobs.sqlite3')
            pr=fixture()[0]
            first=store.reserve('project',pr,'Codex','fix',Path(directory))
            store.db.execute("UPDATE jobs SET status='quarantined' WHERE id=?",(first,))
            self.assertIsNone(store.reserve('project',pr,'Claude','review',Path(directory)))
            self.assertIsNotNone(store.reserve('project',{**pr,'number':50},'Codex','fix',Path(directory)))

    def test_collect_quarantines_only_proven_stopped_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            store=c.Store(Path(directory)/'jobs.sqlite3')
            job=store.reserve('project',fixture()[0],'Codex','fix',Path(directory))
            store.db.execute("UPDATE jobs SET status='orphaned',pid=123,started='old' WHERE id=?",(job,))
            coordinator=c.Coordinator.__new__(c.Coordinator)
            coordinator.store=store; coordinator.children={}; coordinator.publish_results=Mock()
            with patch.object(c,'worker_stopped',return_value=False):
                coordinator.collect({'id':'project'},[])
            self.assertEqual(store.db.execute('SELECT status FROM jobs').fetchone()[0],'orphaned')
            with patch.object(c,'worker_stopped',return_value=True):
                coordinator.collect({'id':'project'},[])
            self.assertEqual(store.db.execute('SELECT status FROM jobs').fetchone()[0],'quarantined')

    def test_stopped_orphans_back_off_and_cap_cross_pr_cascade(self):
        for kind in ('fix', 'review'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'jobs.sqlite3'
                store = c.Store(path)
                coordinator = c.Coordinator.__new__(c.Coordinator)
                coordinator.store=store; coordinator.children={}; coordinator.publish_results=Mock()
                claims = []
                with patch.object(c.time,'time') as now, patch.object(c,'worker_stopped',return_value=True):
                    for tick in range(20):
                        now.return_value = tick*10000
                        job = store.reserve('project',{**fixture()[0],'number':tick},'Codex',kind,Path(directory))
                        if job is None: continue
                        claims.append(job)
                        store.db.execute("UPDATE jobs SET status='orphaned',pid=123,started='old' WHERE id=?", (job,))
                        coordinator.collect({'id':'project'}, [])
                        self.assertIsNone(store.reserve('other',{**fixture()[0],'number':100+tick},'Codex',kind,Path(directory)))
                        attempts, deadline = store.db.execute('SELECT attempts,retry_after FROM quarantine_backoff').fetchone()
                        self.assertEqual(attempts, len(claims))
                        self.assertEqual(deadline, tick*10000 + 300*2**(len(claims)-1))
                    self.assertEqual(len(claims), 3)
                    self.assertIsNone(c.Store(path).reserve('other',{**fixture()[0],'number':999},'Codex',kind,Path(directory)))
                    # The other family/category can still make progress.
                    self.assertIsNotNone(store.reserve('other',fixture()[0],'Claude',kind,Path(directory)))

    def test_worker_stopped_requires_identity_and_complete_session_scan(self):
        self.assertFalse(c.worker_stopped(None,None))
        with patch.object(c,'process_identity',return_value='same'):
            self.assertFalse(c.worker_stopped(999,'same'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            child=root/'123'; child.mkdir()
            (child/'stat').write_text('123 (child) S 1 999 999 '+'0 '*16)
            with patch.object(c,'process_identity',return_value=None),patch.object(c.Path,'iterdir',return_value=iter([child])):
                self.assertFalse(c.worker_stopped(999,'old'))
            (child/'stat').write_text('123 (child) Z 1 999 999 '+'0 '*16)
            with patch.object(c,'process_identity',return_value=None),patch.object(c.Path,'iterdir',return_value=iter([child])):
                self.assertTrue(c.worker_stopped(999,'old'))
        with patch.object(c,'process_identity',return_value=None),patch.object(c.Path,'iterdir',side_effect=PermissionError):
            self.assertFalse(c.worker_stopped(999,'old'))

    def test_long_feedback_build_does_not_occupy_readonly_review_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            store=c.Store(Path(directory)/'jobs.sqlite3')
            pr=fixture()[0]
            self.assertIsNotNone(store.reserve('project',pr,'Codex','fix',Path(directory)/'fix'))
            other={**pr,'number':50}
            self.assertIsNotNone(store.reserve('project',other,'Codex','review',Path(directory)/'review'))
            self.assertIsNone(store.reserve('project',pr,'Claude','review',Path(directory)/'same-pr'))
            self.assertIsNone(store.reserve('project',{**pr,'number':51},'Codex','review',Path(directory)/'second-review'))
            self.assertIsNone(store.reserve('project',{**pr,'number':52},'Codex','fix',Path(directory)/'second-fix'))

    def test_configurable_repairs_preserve_per_pr_exclusion_and_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'jobs.sqlite3'
            store=c.Store(path, {'Codex': {'fix': 2}})
            pr=fixture()[0]
            first=store.reserve('one',pr,'Codex','fix',Path(directory))
            self.assertIsNotNone(first)
            store.db.execute("UPDATE jobs SET status='orphaned' WHERE id=?",(first,))
            other={**pr,'number':50}
            self.assertIsNotNone(store.reserve('one',other,'Codex','fix',Path(directory)))
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(directory)))
            self.assertIsNone(store.reserve('one',{**pr,'number':51},'Codex','fix',Path(directory)))
            restarted=c.Store(path, {'Codex': {'fix': 1}})
            self.assertIsNone(restarted.reserve('two',{**pr,'number':52},'Codex','fix',Path(directory)))
            self.assertEqual(restarted.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('reserved','orphaned')").fetchone()[0],2)
            self.assertIsNotNone(store.reserve('one',{**pr,'number':51},'Codex','review',Path(directory)))

    def test_concurrent_reservations_obey_configured_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'jobs.sqlite3'
            c.Store(path)
            barrier=threading.Barrier(4)
            results=[]
            def reserve(number):
                store=c.Store(path, {'Codex': {'fix': 2}})
                barrier.wait()
                results.append(store.reserve('one',{**fixture()[0],'number':number},'Codex','fix',Path(directory)))
                store.db.close()
            threads=[threading.Thread(target=reserve,args=(number,)) for number in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
            self.assertEqual(len(results),4)
            self.assertEqual(sum(result is not None for result in results),2)

    def test_invalid_review_worker_limits_rejected(self):
        for limits in [{'Other':{'fix':2}}, {'Codex':{'unknown':2}}, {'Codex':{'fix':True}}, {'Claude':{'review':0}}, {'Codex':{'fix':17}}, {'Codex':[]}, ['Codex']]:
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                c.Store(Path(directory)/'state.sqlite3', limits)

    def test_acceptance_scope_change_requires_new_independent_review(self):
        pr,owner,review=fixture()
        pr['_acceptance_policy']={'tickets':{'SF-008':{'acceptance_issues':['https://example.test/11']}}}
        self.assertIsNotNone(c.gate(pr,owner,review))
        review['feedback']=c.feedback(pr)
        self.assertIsNone(c.gate(pr,owner,review))
        review['result']['verdict']='changes_requested'
        self.assertIsNotNone(c.gate(pr,owner,review))

    def test_testnet_status_required_even_before_workflow_is_queued(self):
        pr, owner, review = fixture()
        pr['_testnet_required'] = True
        review['feedback'] = c.feedback(pr)
        self.assertIn('testnet', c.gate(pr, owner, review))
        for state, base in [('pending', pr['baseRefOid']), ('failure', pr['baseRefOid']), ('success', 'old')]:
            pr['_testnet_status'] = dict(state=state, description='base:' + base)
            review['feedback'] = c.feedback(pr)
            self.assertIn('testnet', c.gate(pr, owner, review))
        pr['_testnet_status'] = dict(state='success', description='base:' + pr['baseRefOid'])
        review['feedback'] = c.feedback(pr)
        self.assertIsNone(c.gate(pr, owner, review))

    def test_current_independent_review_passes(self):
        self.assertIsNone(c.gate(*fixture()))

    def test_head_base_comments_and_reviews_invalidate(self):
        for field,value in [('headRefOid','new'),('baseRefOid','new'),('comments',[{'id':'x','body':'fix bug','updatedAt':'new'}]),('reviews',[{'id':'x','state':'CHANGES_REQUESTED','body':'fix','commit':{'oid':'a'*40},'author':{'login':'human'}}])]:
            pr,owner,review=fixture();pr[field]=value
            self.assertIsNotNone(c.gate(pr,owner,review),field)

    def test_blockers_fail_closed(self):
        for field,value in [('state','CLOSED'),('isDraft',True),('mergeable','UNKNOWN'),('mergeable','CONFLICTING'),('threads',[{'isResolved':False}]),('statusCheckRollup',[]),('statusCheckRollup',[{'__typename':'CheckRun','status':'COMPLETED','conclusion':'SKIPPED'}]),('statusCheckRollup',[{'__typename':'CheckRun','status':'IN_PROGRESS','conclusion':None}]),('statusCheckRollup',[{'__typename':'StatusContext','state':'FAILURE'}])]:
            pr,owner,review=fixture();pr[field]=value;review['feedback']=c.feedback(pr)
            self.assertIsNotNone(c.gate(pr,owner,review),field)

    def test_ownership_acceptance_and_self_review(self):
        for field,value in [('status','in_progress'),('publish_pending',True),('agent','Claude')]:
            pr,owner,review=fixture();owner[field]=value;self.assertIsNotNone(c.gate(pr,owner,review))
        for field,value in [('verdict','changes_requested'),('acceptance_complete',False)]:
            pr,owner,review=fixture();review['result'][field]=value;self.assertIsNotNone(c.gate(pr,owner,review))

    def test_formal_changes_requested_must_be_resolved(self):
        pr,owner,review=fixture();pr['reviews']=[{'id':'r','state':'CHANGES_REQUESTED','author':{'login':'human'},'commit':{'oid':pr['headRefOid']}}];review['feedback']=c.feedback(pr)
        self.assertIn('GitHub',c.gate(pr,owner,review))
        pr['reviews'].append({'id':'r2','state':'APPROVED','author':{'login':'human'},'commit':{'oid':pr['headRefOid']}});review['feedback']=c.feedback(pr)
        self.assertIsNone(c.gate(pr,owner,review))

    def test_publication_does_not_trigger_infinite_review_loop(self):
        pr,owner,review=fixture();pr['comments']=[{'id':'x','body':c.MARKER+' approved','updatedAt':'new'}];pr['reviews']=[{'id':'y','state':'APPROVED','body':c.MARKER+' approved','author':{'login':'therealtime'}}]
        pr['_receipts'] = {item['id']:c.publication_hash(item) for item in pr['comments']+pr['reviews']}
        self.assertIsNone(c.gate(pr,owner,review))
        pr['comments'].append({'id':'spoof','body':c.MARKER+' hidden request','updatedAt':'new'})
        self.assertIsNotNone(c.gate(pr,owner,review))


class Reservations(unittest.TestCase):
    def test_concurrent_model_reservation_is_atomic_across_projects(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.sqlite3';c.Store(path);barrier=threading.Barrier(2);results=[]
            def acquire(project):
                store=c.Store(path);barrier.wait();results.append(store.reserve(project,fixture()[0],'Claude','review',Path(d)));store.db.close()
            threads=[threading.Thread(target=acquire,args=(p,)) for p in ('one','two')]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(sum(r is not None for r in results),1)
            # Restart preserves a live job; no duplicate CLI process.
            self.assertIsNone(c.Store(path).reserve('three',fixture()[0],'Claude','review',Path(d)))

    def test_pr_exclusion_and_completed_same_input_deduplication(self):
        with tempfile.TemporaryDirectory() as d:
            store=c.Store(Path(d)/'state.sqlite3');pr=fixture()[0];job=store.reserve('one',pr,'Claude','review',Path(d))
            self.assertIsNone(store.reserve('one',pr,'Codex','fix',Path(d)))
            store.db.execute("UPDATE jobs SET status='done',publication_done=1,result=? WHERE id=?",(json.dumps(fixture()[2]['result']),job))
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))
            pr['headRefOid']='new';self.assertIsNotNone(store.reserve('one',pr,'Claude','review',Path(d)))

    def test_cli_wrapper_records_success_and_failure_across_daemon_restarts(self):
        import subprocess,sys
        worker=Path(c.__file__).with_name('worker.py')
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'prompt.txt').write_text('test')
            for code in (0,7):
                (directory/'command.json').write_text(json.dumps([sys.executable,'-c',f'import sys; sys.exit({code})']))
                result=subprocess.run([sys.executable,str(worker),d])
                self.assertEqual(result.returncode,code);self.assertEqual(json.loads((directory/'exit.json').read_text())['returncode'],code)
                self.assertIn('started',json.loads((directory/'process.json').read_text()))



class IntegrationRaces(unittest.TestCase):
    def coordinator(self,d):
        return c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))

    def test_changed_base_stops_before_merge_or_push(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();pr.update(baseRefName='main',title='test')
            with patch.object(c,'run',side_effect=['','changed']) as command:
                with self.assertRaisesRegex(RuntimeError,'base changed'):
                    coordinator.merge({'id':'test','root':d},pr,owner,review)
                self.assertFalse(any('push' in call.args[0] for call in command.call_args_list))

    def test_feedback_changed_stops_before_preparing_merge(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();pr.update(baseRefName='main',title='test')
            changed=copy.deepcopy(pr);changed['comments']=[{'id':'new','body':'blocker','updatedAt':'now'}]
            with patch.object(c,'run',side_effect=['',pr['baseRefOid']]) as command,patch.object(coordinator,'pr',return_value=changed):
                with self.assertRaisesRegex(RuntimeError,'PR changed'):
                    coordinator.merge({'id':'test','root':d},pr,owner,review)
                self.assertFalse(any('merge' in call.args[0] or 'push' in call.args[0] for call in command.call_args_list))

    def check_failed_resume_recovery(self, change=None):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d)
            coordinator.config={'workers':{'Codex':['worker']}}
            coordinator.children={}
            pr,owner,_=fixture()
            owner.update(key='T1',token='private',worktree=d,branch='owner',model='test',evidence='',spec={})
            state=copy.deepcopy(owner)
            calls=[]
            def ticket(project,command,*args):
                calls.append(command)
                if command == 'show': return json.dumps(state)
                if command == 'resume':
                    state.update(status='in_progress',publish_pending=True)
                    raise RuntimeError('mirror unavailable')
                if command == 'publish': state['publish_pending']=False
                return ''
            coordinator.ticket=ticket
            coordinator.pr=Mock(return_value=pr)
            coordinator.publish_results=Mock()
            coordinator.prompt=Mock(return_value='test prompt')
            def run(args,**kwargs):
                if args[:2] == ['git','status']: return ''
                if args[:2] == ['git','branch']: return 'owner'
                if args[:2] == ['git','rev-parse']: return pr['headRefOid']
                return 'test-auth'
            with patch.object(c,'run',side_effect=run),patch.object(c.subprocess,'Popen') as popen,patch.object(c,'process_identity',return_value='identity'):
                with self.assertRaisesRegex(RuntimeError,'mirror unavailable'):
                    coordinator.launch({'id':'test','github_account':'test'},pr,owner,'fix')
                row=coordinator.store.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['status'],'reserved')
                self.assertNotIn('private',row['launch_intent'])
                self.assertEqual(calls.count('resume'),1)
                popen.assert_not_called()
                # An unrelated reservation cannot overlap the pending launch.
                self.assertIsNone(coordinator.store.reserve('test',pr,'Claude','review',Path(d)))
                original_directory=Path(row['directory'])
                original_feedback=row['feedback']
                if change == 'base': pr['baseRefOid']='c'*40
                if change == 'checks': pr['statusCheckRollup'][0]['conclusion']='FAILURE'
                if change in ('comment','collision'): pr['comments'].append(dict(id='new',body='new feedback',updatedAt='now'))
                if change == 'collision':
                    coordinator.store.db.execute("INSERT INTO jobs(project,pr,family,kind,head,feedback,status,directory,created) VALUES(?,?,?,?,?,?,'done',?,0)",('test',pr['number'],'Codex','fix',pr['headRefOid'],c.feedback(pr),str(Path(d)/'terminal')))

                coordinator.store.db.execute('UPDATE jobs SET retry_after=0')
                popen.return_value.pid=123
                coordinator.collect({'id':'test','github_account':'test'},[])
                self.assertEqual(json.loads((original_directory/'snapshot.json').read_text())['baseRefOid'],pr['baseRefOid'])
                recovered=coordinator.store.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(recovered['directory'],str(original_directory))
                self.assertEqual(recovered['feedback'],original_feedback)
                self.assertEqual(json.loads((original_directory/'snapshot.json').read_text())['statusCheckRollup'],pr['statusCheckRollup'])
                self.assertEqual(json.loads((original_directory/'snapshot.json').read_text())['comments'],pr['comments'])
                self.assertEqual(calls.count('resume'),1)
                self.assertEqual(calls.count('publish'),1)
                popen.assert_called_once()
                self.assertEqual(coordinator.store.db.execute('SELECT status FROM jobs WHERE id=?',(row['id'],)).fetchone()[0],'running')
                coordinator.collect({'id':'test','github_account':'test'},[])
                popen.assert_called_once()

    def test_failed_resume_mirror_recovers_without_replaying_mutation(self):
        self.check_failed_resume_recovery()

    def test_pending_resume_recovers_with_fresh_base_checks_and_feedback(self):
        for change in ('base','checks','comment','collision'):
            with self.subTest(change=change):
                self.check_failed_resume_recovery(change)

    def test_pending_collection_retains_sanitized_recovery_reason(self):
        for changed_head in (False,True):
            with self.subTest(changed_head=changed_head),tempfile.TemporaryDirectory() as d:
                coordinator=self.coordinator(d)
                pr,owner,_=fixture()
                job=coordinator.store.reserve('test',pr,'Codex','fix',Path(d))
                coordinator.store.db.execute('UPDATE jobs SET launch_intent=? WHERE id=?',
                                             (json.dumps({'key':'T1'}),job))
                coordinator.ticket=Mock(return_value=json.dumps(owner))
                fresh=copy.deepcopy(pr)
                if changed_head: fresh['headRefOid']='c'*40
                coordinator.pr=Mock(return_value=fresh)
                coordinator.launch=Mock(side_effect=RuntimeError('secret remote diagnostic'))
                coordinator.publish_results=Mock()
                coordinator.collect({'id':'test'},[])
                row=coordinator.store.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['status'],'reserved')
                self.assertNotIn('secret',row['error'])
                if changed_head:
                    coordinator.launch.assert_not_called()
                    self.assertIn('pending launch head changed',row['error'])
                else:
                    coordinator.launch.assert_called_once()
                    self.assertEqual(row['error'],'collection deferred: RuntimeError')

    def test_uncertain_spawn_is_never_automatically_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d)
            pr,owner,_=fixture()
            owner.update(key='T1',token='private',worktree=d,branch='owner',model='test',evidence='',spec={})
            coordinator.ticket=Mock(return_value=json.dumps(owner))
            coordinator.prompt=Mock(return_value='test')
            def run(args,**kwargs):
                if args[:2] == ['git','status']: return ''
                if args[:2] == ['git','branch']: return 'owner'
                if args[:2] == ['git','rev-parse']: return pr['headRefOid']
                return 'test-auth'
            with patch.object(c,'run',side_effect=run),patch.object(c.subprocess,'Popen',side_effect=OSError('uncertain spawn')) as popen:
                with self.assertRaises(OSError):
                    coordinator.launch({'id':'test','github_account':'test'},pr,owner,'fix')
                row=coordinator.store.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['status'],'orphaned')
                self.assertIsNone(row['launch_intent'])
                coordinator.launch({'id':'test','github_account':'test'},pr,owner,'fix')
                popen.assert_called_once()

    def test_pending_launch_rejects_changed_claim_and_worker_evidence(self):
        for change in ('token','branch','worktree','dirty','head','receipt','pid'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                coordinator=self.coordinator(d)
                pr,owner,_=fixture()
                owner.update(key='T1',token='private',worktree=d,branch='owner')
                intent=dict(claim=c.hashlib.sha256(b'private').hexdigest(),key='T1',branch='owner',worktree=d,head=pr['headRefOid'])
                directory=coordinator.state/'test'/f"pr-{pr['number']}-fix-{pr['headRefOid'][:12]}-{c.feedback(pr)[:12]}"
                job=coordinator.store.reserve('test',pr,'Codex','fix',directory)
                coordinator.store.db.execute('UPDATE jobs SET launch_intent=? WHERE id=?',(json.dumps(intent),job))
                row=dict(coordinator.store.db.execute('SELECT * FROM jobs').fetchone())
                # launch derives its private directory from the exact snapshot.
                directory=coordinator.state/'test'/f"pr-{pr['number']}-fix-{pr['headRefOid'][:12]}-{c.feedback(pr)[:12]}"
                directory.mkdir(parents=True)
                if change in ('token','branch','worktree'): owner[change]='changed'
                if change == 'receipt': (directory/'process.json').write_text('{}')
                if change == 'pid': row['pid']=123
                coordinator.ticket=Mock(return_value=json.dumps({**owner,'status':'in_progress'}))
                def run(args,**kwargs):
                    if args[:2] == ['git','status']: return 'dirty' if change == 'dirty' else ''
                    if args[:2] == ['git','branch']: return 'owner'
                    return 'changed' if change == 'head' else pr['headRefOid']
                with patch.object(c,'run',side_effect=run),patch.object(c.subprocess,'Popen') as popen:
                    with self.assertRaises(RuntimeError):
                        coordinator.launch({'id':'test'},pr,owner,'fix',recovery=row)
                    popen.assert_not_called()
                    self.assertEqual([x.args[1] for x in coordinator.ticket.call_args_list],['show'])

    def test_dirty_owner_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=self.coordinator(d);pr,owner,review=fixture();owner.update(key='T1',token='local',worktree=d,branch='owner')
            with patch.object(coordinator,'ticket',return_value=json.dumps(owner)) as ticket,patch.object(c,'run',return_value='dirty'):
                with self.assertRaisesRegex(RuntimeError,'dirty'):
                    coordinator.launch({'id':'test'},pr,owner,'fix')
                self.assertEqual(ticket.call_count,1)
                self.assertNotIn('resume',ticket.call_args.args)

class RetryTests(unittest.TestCase):
    def test_backoff_persists_and_does_not_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as d:
            store=c.Store(Path(d)/'state.sqlite3');pr=fixture()[0]
            job=store.reserve('one',pr,'Claude','review',Path(d))
            store.db.execute("UPDATE jobs SET status='retry',retry_after=? WHERE id=?",(c.time.time()+3600,job))
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))
            store.db.execute("UPDATE jobs SET retry_after=0 WHERE id=?",(job,))
            self.assertEqual(store.reserve('one',pr,'Claude','review',Path(d)),job)
            self.assertIsNone(store.reserve('one',pr,'Claude','review',Path(d)))

class ResultTests(unittest.TestCase):
    def test_evidence_arrays_preserve_actual_review_without_changing_verdict(self):
        result=c.normalize_result(dict(verdict='blocked',acceptance_complete=False,model='GPT-6',evidence=['source reviewed','missing release acceptance']))
        self.assertEqual(result['verdict'],'blocked');self.assertFalse(result['acceptance_complete']);self.assertIn('missing release acceptance',result['evidence'])
        for invalid in ([],[''],[{'untrusted':'object'}],None):
            with self.assertRaises(ValueError): c.normalize_result(dict(verdict='approve',acceptance_complete=True,model='GPT-6',evidence=invalid))

class FullCycleTests(unittest.TestCase):
    def build(self,d,lag=False):
        pr,owner,review=fixture();pr.update(headRefName='work/test',baseRefName='main',title='test',mergeCommit=None)
        owner.update(key='T1',token='local-claim',branch=pr['headRefName'],spec={'priority':'P0'})
        project={'id':'test','root':d,'repository':'owner/repo'}
        coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[project]},Path(d))
        job=coordinator.store.reserve('test',pr,'Claude','review',Path(d)/'review')
        coordinator.store.db.execute("UPDATE jobs SET status='done',publication_done=1,result=? WHERE id=?",(json.dumps(review['result']),job))
        state={'pushed':False,'lag':lag,'complete':False,'commands':[],'tickets':[]}
        commit='c'*40;identity='Tyler Bush <tyler.jonathan.bush@gmail.com>'
        def current():
            result=copy.deepcopy(pr)
            if state['pushed']:
                result['baseRefOid']=commit;result['baseIncluded']=False
                if not state['lag']:result.update(state='MERGED',mergeCommit={'oid':commit})
            return result
        def command(args,cwd=None,env=None):
            state['commands'].append(args)
            if args[:3]==['git','rev-parse','origin/main']:return pr['baseRefOid']
            if args[:3]==['git','rev-parse','HEAD']:return commit
            if args[:3]==['git','merge-base','--is-ancestor']:
                if not state['pushed']: raise c.subprocess.CalledProcessError(1,args)
                return ''
            if args[:2]==['git','ls-remote']:return pr['baseRefOid']+'\trefs/heads/main\n'
            if args[:2]==['git','show'] or (args[:2]==['git','log'] and '--format=%B' not in args):return identity+'|'+identity+'\n'
            if args[:2]==['git','merge']:
                self.assertEqual(env['GIT_AUTHOR_NAME'],'Tyler Bush');self.assertEqual(env['GIT_COMMITTER_EMAIL'],'tyler.jonathan.bush@gmail.com')
            if args[:2]==['git','push']:
                self.assertEqual(args,['git','push','origin','HEAD:refs/heads/main'])
                row=coordinator.store.db.execute('SELECT * FROM integrations').fetchone()
                self.assertEqual(row['commit_id'],commit);self.assertEqual(row['reconciled'],0)
                state['pushed']=True
            return ''
        def ticket(project,*args):
            state['tickets'].append(args)
            if args[0]=='show':return json.dumps(owner)
            if args[0]=='complete':
                self.assertEqual(current()['state'],'MERGED')
                self.assertIn('--expect-token',args);self.assertIn('--merged-commit',args);self.assertIn(commit,args);self.assertIn('origin/main',args)
                state['complete']=True;owner['status']='done'
            return '{}'
        coordinator.discover_merged=lambda *args:None
        patches=[patch.object(c,'run',side_effect=command),patch.object(coordinator,'owners',return_value=[owner]),patch.object(coordinator,'pr',side_effect=lambda *args:current()),patch.object(coordinator,'prs',side_effect=lambda *args:[] if state['pushed'] and not state['lag'] else [current()]),patch.object(coordinator,'ticket',side_effect=ticket),patch.object(coordinator,'gh',side_effect=lambda *args:json.dumps({'state':'closed' if state['pushed'] else 'open'})),patch.object(coordinator,'launch')]
        return coordinator,state,patches

    def test_successful_merge_verifies_identity_records_intent_before_push_and_completes_ticket(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as d,ExitStack() as stack:
            coordinator,state,patches=self.build(d)
            for p in patches:stack.enter_context(p)
            coordinator.tick(True)
            self.assertTrue(state['pushed']);self.assertTrue(state['complete'])
            self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],1)
            self.assertGreaterEqual(sum(args[0]=='show' for args in state['tickets']),4)
            self.assertEqual(sum(args[:2]==['git','push'] for args in state['commands']),1)

    def test_delayed_github_merge_notification_does_not_dispatch_a_second_worker(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as d,ExitStack() as stack:
            coordinator,state,patches=self.build(d,lag=True)
            mocks=[stack.enter_context(p) for p in patches]
            coordinator.tick(True);self.assertTrue(state['pushed']);self.assertFalse(state['complete'])
            coordinator.tick(True);mocks[-1].assert_not_called()
            self.assertEqual(sum(args[:2]==['git','push'] for args in state['commands']),1)
            state['lag']=False;coordinator.tick(True)
            self.assertTrue(state['complete']);self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],1)

    def test_exit_zero_without_result_relaunches_after_backoff_without_resetting_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},directory)
            pr=fixture()[0];job=coordinator.store.reserve('test',pr,'Claude','review',directory)
            coordinator.store.db.execute("UPDATE jobs SET status='running',pid=999999,started='missing' WHERE id=?",(job,))
            (directory/'exit.json').write_text('{"returncode":0}')
            with patch.object(c,'process_identity',return_value=None):
                coordinator.collect({'id':'test'},[])
                deadline=coordinator.store.db.execute('SELECT retry_after FROM jobs').fetchone()[0]
                with patch.object(c.time,'time',return_value=deadline+1):
                    coordinator.collect({'id':'test'},[])
                    self.assertEqual(coordinator.store.db.execute('SELECT retry_after FROM jobs').fetchone()[0],deadline)
                    self.assertEqual(coordinator.store.reserve('test',pr,'Claude','review',directory),job)

    def test_launch_failure_does_not_abort_remaining_prs(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,state,patches=self.build(d)
            pr,owner,_=fixture();pr.update(headRefName='work',baseRefName='main');owner.update(branch='work',spec={'priority':'P0'})
            next_pr={**pr,'number':50}
            coordinator.store.db.execute('DELETE FROM jobs')
            visited=[]
            def launch(project,pr,*args):
                visited.append(pr['number'])
                if pr['number']==49:raise RuntimeError('one broken checkout')
            with patch.object(coordinator,'owners',return_value=[owner]),patch.object(coordinator,'prs',return_value=[pr,next_pr]),patch.object(coordinator,'collect'),patch.object(coordinator,'ensure_closing_reference'),patch.object(coordinator,'launch',side_effect=launch):coordinator.tick(True)
            self.assertEqual(visited,[49,50])

    def test_branch_without_current_base_cannot_merge(self):
        pr,owner,review=fixture();pr['baseIncluded']=False
        self.assertIn('integrate current base',c.gate(pr,owner,review))



class PushFailureTests(unittest.TestCase):
    build = FullCycleTests.build
    def test_prepared_but_unpushed_commit_is_retried_without_dispatching_worker(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as d,ExitStack() as stack:
            coordinator,state,patches=self.build(d)
            mocks=[stack.enter_context(p) for p in patches]
            original=mocks[0].side_effect
            failed=[False]
            def command(args,**kwargs):
                if args[:2]==['git','push'] and not failed[0]:
                    failed[0]=True;raise c.subprocess.CalledProcessError(128,args)
                return original(args,**kwargs)
            mocks[0].side_effect=command
            coordinator.tick(True)
            self.assertFalse(state['pushed']);self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],0)
            coordinator.tick(True)
            mocks[-1].assert_not_called();self.assertTrue(state['complete'])



class IssueClosureTests(unittest.TestCase):
    def setup_case(self,d):
        coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))
        project={'id':'test','root':d,'repository':'owner/repo'}
        owner={'key':'T1','branch':'work/test','status':'review','token':'local','remote':{'url':'https://github.com/owner/repo/issues/6'}}
        pr={'number':47,'state':'MERGED','headRefOid':'a'*40,'headRefName':'work/test','baseRefName':'main','mergeCommit':{'oid':'c'*40}}
        return coordinator,project,owner,pr

    def test_native_closing_keyword_preserves_description_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=self.setup_case(d);pr.update(state='OPEN',body='Details\n\nRefs #6.')
            with patch.object(coordinator,'gh') as gh:
                coordinator.ensure_closing_reference(project,pr,owner)
                self.assertIn('body=Details\n\nRefs #6.\n\nCloses #6.',gh.call_args.args[1])
                pr['body']='Details\n\nCloses #6.';gh.reset_mock();coordinator.ensure_closing_reference(project,pr,owner);gh.assert_not_called()

    def test_unmerged_or_foreign_issue_cannot_be_closed(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=self.setup_case(d)
            with patch.object(coordinator,'gh') as gh:
                pr['state']='OPEN'
                with self.assertRaises(ValueError):coordinator.close_merged_issue(project,pr,owner)
                pr['state']='MERGED';owner['remote']['url']='https://github.com/other/repo/issues/6'
                with self.assertRaises(ValueError):coordinator.close_merged_issue(project,pr,owner)
                gh.assert_not_called()

    def test_explicit_closure_is_verified_and_repeated_closed_state_needs_no_patch(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=self.setup_case(d)
            with patch.object(coordinator,'gh',side_effect=[json.dumps({'state':'open'}),'{}',json.dumps({'state':'closed'})]) as gh:
                coordinator.close_merged_issue(project,pr,owner)
                self.assertIn('state=closed',gh.call_args_list[1].args[1])
            with patch.object(coordinator,'gh',return_value=json.dumps({'state':'closed'})) as gh:
                coordinator.close_merged_issue(project,pr,owner)
                self.assertFalse(any('--method' in call.args[1] for call in gh.call_args_list))

    def test_external_merge_discovered_without_automation_receipt_then_reconciled(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=self.setup_case(d)
            source=dict(number=47,merged_at='now',base={'ref':'main'},head={'ref':'work/test','sha':pr['headRefOid'],'repo':{'full_name':'owner/repo'}},merge_commit_sha=pr['mergeCommit']['oid'])
            with patch.object(coordinator,'gh',side_effect=[json.dumps([[source,{**source,'number':48,'merged_at':None}]]), '[[]]']),patch.object(c,'run',return_value=pr['headRefOid']+' refs/heads/work/test'):
                coordinator.discover_merged(project,[owner])
            self.assertEqual(coordinator.store.db.execute('SELECT COUNT(*) FROM integrations').fetchone()[0],1)
            with patch.object(c,'run',return_value=''),patch.object(coordinator,'ticket') as ticket,patch.object(coordinator,'close_merged_issue') as close:
                coordinator.reconcile(project,pr,owner,None)
                self.assertEqual(ticket.call_args.args[1],'complete');close.assert_called_once()
            self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],1)

    def test_failed_closure_keeps_reconciliation_pending_for_done_claim(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=self.setup_case(d);owner['status']='done'
            coordinator.store.db.execute('INSERT INTO integrations VALUES(?,?,?,?,0)',('test',47,pr['headRefOid'],pr['mergeCommit']['oid']))
            with patch.object(c,'run',return_value=''),patch.object(coordinator,'ticket'),patch.object(coordinator,'close_merged_issue',side_effect=RuntimeError('transient API error')):
                with self.assertRaises(RuntimeError):coordinator.reconcile(project,pr,owner,None)
            self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],0)
            with patch.object(c,'run',return_value=''),patch.object(coordinator,'ticket') as ticket,patch.object(coordinator,'close_merged_issue'):
                coordinator.reconcile(project,pr,owner,None)
                self.assertEqual(ticket.call_args.args[1],'publish')
            self.assertEqual(coordinator.store.db.execute('SELECT reconciled FROM integrations').fetchone()[0],1)



class ReviewOwnershipTests(unittest.TestCase):
    def test_review_is_valid_for_merge_but_active_or_changed_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))
            project={'repository':'owner/repo'}
            owner={'key':'T1','token':'claim','status':'review','remote':{'url':'https://github.com/owner/repo/issues/6'}}
            with patch.object(coordinator,'ticket',return_value=json.dumps(owner)),patch.object(coordinator,'gh',return_value='{"state":"open"}'):
                coordinator.assert_review_owner(project,owner)
            for changed in ({**owner,'status':'in_progress'},{**owner,'token':'different'},{**owner,'publish_pending':True}):
                with patch.object(coordinator,'ticket',return_value=json.dumps(changed)):
                    with self.assertRaises(RuntimeError):coordinator.assert_review_owner(project,owner)

    def test_dependency_regression_blocks_integration(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))
            owner={'key':'T1','token':'claim','status':'review','spec':{'depends_on':['T0']}}
            with patch.object(coordinator,'ticket',side_effect=[json.dumps(owner),json.dumps({'status':'review'})]):
                with self.assertRaisesRegex(RuntimeError,'dependency'):coordinator.assert_review_owner({},owner)


class PublicationTests(unittest.TestCase):
    def test_formal_review_failure_preserves_result_and_never_reposts_comment(self):
        for failures in (1,3):
            with self.subTest(failures=failures),tempfile.TemporaryDirectory() as d:
                directory=Path(d);pr,_,review=fixture()
                coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},directory)
                project={'id':'test','repository':'owner/repo','review_github_account':'reviewer'}
                job=coordinator.store.reserve('test',pr,'Claude','review',directory)
                coordinator.store.db.execute("UPDATE jobs SET status='running',pid=999999,started='old' WHERE id=?",(job,))
                (directory/'exit.json').write_text('{"returncode":0}')
                (directory/'result.json').write_text(json.dumps(review['result']))
                comments=[];reviews=[]
                def gh(project,args):
                    body=next(a[5:] for a in args if a.startswith('body='))
                    if '/comments' in args[1]:
                        comments.append(body);return json.dumps({'node_id':'comment','body':body})
                    reviews.append(body)
                    if len(reviews)<=failures:raise RuntimeError('formal review unavailable')
                    return json.dumps({'node_id':'review','body':body,'state':'APPROVED','commit_id':pr['headRefOid']})
                with patch.object(coordinator,'pr',return_value=pr),patch.object(coordinator,'gh',side_effect=gh),patch.object(c,'process_identity',return_value=None):
                    coordinator.collect(project,[])
                    for attempt in range(failures):
                        row=coordinator.store.db.execute('SELECT * FROM jobs').fetchone()
                        self.assertEqual(row['status'],'done')
                        self.assertEqual(coordinator.store.review('test',pr['number'])['result'],review['result'])
                        # Restart between publication attempts; no worker rerun.
                        coordinator=c.Coordinator(coordinator.config,directory)
                        with patch.object(coordinator,'pr',return_value=pr),patch.object(coordinator,'gh',side_effect=gh),patch.object(c.time,'time',return_value=row['retry_after']+1):
                            coordinator.collect(project,[])
                    self.assertEqual(len(comments),1)
                    self.assertEqual(len(reviews),failures+1)
                    self.assertEqual(coordinator.store.db.execute('SELECT publication_done FROM jobs').fetchone()[0],1)

    def test_schema_upgrade_does_not_republish_legacy_done_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'reviews.sqlite3'
            db=c.sqlite3.connect(path)
            db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, project TEXT, pr INTEGER, family TEXT, kind TEXT, head TEXT, feedback TEXT, status TEXT, directory TEXT, pid INTEGER, started TEXT, result TEXT, error TEXT, created REAL)')
            db.execute("INSERT INTO jobs(id,status) VALUES(1,'done')")
            db.commit();db.close()
            store=c.Store(path)
            self.assertEqual(store.db.execute('SELECT publication_done FROM jobs').fetchone()[0],1)
            store.db.close()
            self.assertEqual(c.Store(path).db.execute('SELECT publication_done FROM jobs').fetchone()[0],1)

    def test_refetch_failure_keeps_done_result_and_blocks_gate_until_published(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);pr,owner,review=fixture()
            coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},directory)
            job=coordinator.store.reserve('test',pr,'Claude','review',directory)
            coordinator.store.db.execute("UPDATE jobs SET status='done',result=? WHERE id=?",(json.dumps(review['result']),job))
            with patch.object(coordinator,'pr',side_effect=RuntimeError('refetch unavailable')):
                coordinator.collect({'id':'test'},[])
            collected=coordinator.store.review('test',pr['number'])
            self.assertEqual(collected['result'],review['result'])
            self.assertIn('publication',c.gate(pr,owner,collected))
            self.assertEqual(coordinator.store.db.execute('SELECT status FROM jobs').fetchone()[0],'done')

    def test_reused_pid_does_not_orphan_retryable_review(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);coordinator=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},directory)
            job=coordinator.store.reserve('test',fixture()[0],'Claude','review',directory)
            coordinator.store.db.execute("UPDATE jobs SET status='running',pid=123,started='old' WHERE id=?",(job,))
            (directory/'exit.json').write_text('{"returncode":1}')
            with patch.object(c,'process_identity',return_value='reused'):coordinator.collect({'id':'test'},[])
            self.assertEqual(coordinator.store.db.execute('SELECT status FROM jobs').fetchone()[0],'retry')

    def test_external_old_merge_does_not_complete_reused_branch(self):
        with tempfile.TemporaryDirectory() as d:
            coordinator,project,owner,pr=IssueClosureTests().setup_case(d)
            source=dict(number=47,merged_at='now',base={'ref':'main'},head={'ref':'work/test','sha':pr['headRefOid'],'repo':{'full_name':'owner/repo'}},merge_commit_sha=pr['mergeCommit']['oid'])
            for open_prs,tip in (([[source]],pr['headRefOid']),([[]],'new-tip')):
                with patch.object(coordinator,'gh',side_effect=[json.dumps([[source]]),json.dumps(open_prs)]),patch.object(c,'run',return_value=tip+' refs/heads/work/test'):
                    coordinator.discover_merged(project,[owner])
                self.assertEqual(coordinator.store.db.execute('SELECT COUNT(*) FROM integrations').fetchone()[0],0)



class CheckFreshnessTests(unittest.TestCase):
    def test_fingerprint_tracks_outcomes_not_order_or_display_metadata(self):
        pr,_,_=fixture()
        pr['statusCheckRollup']=[{'__typename':'CheckRun','name':'ci','workflowName':'CI','status':'IN_PROGRESS','conclusion':None},
                                {'__typename':'StatusContext','context':'external','state':'SUCCESS'}]
        pending=c.feedback(pr)
        pr['statusCheckRollup'][0].update(status='COMPLETED',conclusion='SUCCESS')
        successful=c.feedback(pr)
        self.assertNotEqual(pending,successful)
        pr['statusCheckRollup'].reverse()
        for check in pr['statusCheckRollup']:
            check.update(id='new-id',detailsUrl='https://example.test/new',targetUrl='https://example.test/new',startedAt='later',completedAt='later')
        self.assertEqual(successful,c.feedback(pr))
        pr['statusCheckRollup'].append({'__typename':'CheckRun','name':'another','status':'COMPLETED','conclusion':'SUCCESS'})
        self.assertNotEqual(successful,c.feedback(pr))

    def test_testnet_evidence_invalidates_prior_approval(self):
        pr,owner,review=fixture()
        pr.update(_testnet_required=True,_testnet_status={'state':'pending','description':'base:'+pr['baseRefOid']})
        pending=c.feedback(pr)
        pr['_testnet_status']['state']='success'
        ready=c.feedback(pr)
        self.assertNotEqual(pending,ready)
        review['feedback']=ready
        self.assertIsNone(c.gate(pr,owner,review))
        pr['_testnet_status'].update(id='new',target_url='https://example.test/run',updated_at='later')
        self.assertEqual(ready,c.feedback(pr))
        pr['_testnet_status']['description']='base:wrong'
        self.assertNotEqual(ready,c.feedback(pr))
        self.assertIsNotNone(c.gate(pr,owner,review))

    def setup_scan(self,directory,pr,verdict='blocked'):
        owner=fixture()[1]
        owner.update(key='T1',token='local',branch='work/test',spec={'priority':'P1'})
        pr.update(headRefName='work/test',baseRefName='main',title='test')
        project={'id':'test','root':directory,'repository':'owner/repo'}
        engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[project]},Path(directory))
        job=engine.store.reserve('test',pr,'Claude','review',Path(directory)/'old')
        result=dict(verdict=verdict,acceptance_complete=False,model='fixture',evidence='pending checks or genuine missing acceptance')
        engine.store.db.execute("UPDATE jobs SET status='done',publication_done=1,result=? WHERE id=?",(json.dumps(result),job))
        self.configure_scan(engine,pr,owner)
        return engine,project,owner

    def configure_scan(self,engine,pr,owner):
        engine.owners=lambda project:[owner]
        engine.collect=lambda *args:None
        engine.discover_merged=lambda *args:None
        engine.prs=lambda project:[pr]
        engine.ensure_closing_reference=lambda *args:None

    def test_completed_checks_dispatch_fresh_review_once_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            pr=fixture()[0]
            pr['statusCheckRollup'][0].update(status='IN_PROGRESS',conclusion=None)
            engine,project,owner=self.setup_scan(directory,pr)
            with patch.object(engine,'launch') as launch:
                engine.tick(True)
                launch.assert_not_called()
            pr['statusCheckRollup'][0].update(status='COMPLETED',conclusion='SUCCESS')
            launched=[]
            def launch(project,current,owner,kind):
                job=engine.store.reserve(project['id'],current,'Claude',kind,Path(directory)/'new')
                if job is not None:launched.append(job)
            with patch.object(engine,'launch',side_effect=launch):engine.tick(True)
            self.assertEqual(len(launched),1)
            engine=c.Coordinator(engine.config,Path(directory))
            self.configure_scan(engine,pr,owner)
            with patch.object(engine,'launch',side_effect=launch):engine.tick(True)
            self.assertEqual(len(launched),1)
            self.assertEqual(engine.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],2)

    def test_unready_checks_wait_but_actionable_repairs_still_run(self):
        cases=[
            [],[{'__typename':'CheckRun','status':'IN_PROGRESS','conclusion':None}],
            [{'__typename':'CheckRun','status':'COMPLETED','conclusion':'FAILURE'}],
            [{'__typename':'CheckRun','status':'COMPLETED','conclusion':'SKIPPED'}],
            [{'__typename':'CheckRun','status':'COMPLETED','conclusion':'CANCELLED'}],
            [{'__typename':'StatusContext','state':'PENDING'}],
            [{'__typename':'StatusContext','state':'FAILURE'}]]
        for checks in cases:
            with self.subTest(checks=checks),tempfile.TemporaryDirectory() as directory:
                pr=fixture()[0];pr['statusCheckRollup']=checks
                engine,project,owner=self.setup_scan(directory,pr)
                # Force stale feedback: no expensive review until checks succeed.
                pr['comments'].append({'id':'request','body':'recheck','updatedAt':'now'})
                with patch.object(engine,'launch') as launch:
                    engine.tick(True);launch.assert_not_called()
                    pr['baseIncluded']=False
                    engine.tick(True);self.assertEqual(launch.call_args.args[-1],'fix')
                pr['baseIncluded']=True
                engine.store.db.execute("UPDATE jobs SET result=?",(json.dumps(dict(verdict='changes_requested',acceptance_complete=False,model='fixture',evidence='fix source')),))
                with patch.object(engine,'launch') as launch:
                    engine.tick(True);self.assertEqual(launch.call_args.args[-1],'fix')

    def test_testnet_waits_for_current_base_and_genuine_acceptance_stays_blocked(self):
        for state,base in [(None,None),('pending','current'),('failure','current'),('success','old')]:
            with self.subTest(state=state,base=base),tempfile.TemporaryDirectory() as directory:
                pr=fixture()[0];pr['_testnet_required']=True
                engine,project,owner=self.setup_scan(directory,pr)
                if state:pr['_testnet_status']={'state':state,'description':'base:'+(pr['baseRefOid'] if base=='current' else 'old')}
                with patch.object(engine,'launch') as launch:
                    engine.tick(True);launch.assert_not_called()
                    pr['_testnet_status']={'state':'success','description':'base:'+pr['baseRefOid']}
                    engine.tick(True);self.assertEqual(launch.call_args.args[-1],'review')
        with tempfile.TemporaryDirectory() as directory:
            engine,_,_=self.setup_scan(directory,fixture()[0])
            with patch.object(engine,'launch') as launch:
                engine.tick(True);engine.tick(True);launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
