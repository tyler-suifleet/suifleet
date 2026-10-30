import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordinator as coordinator_module
from batch_integration import BatchController, canonical, manifest_digest


class Fixture:
    def __init__(self, root, conflict=False):
        self.root = root
        self.remote, self.repo = root/'remote.git', root/'repo'
        self.git('init', '--bare', '-q', str(self.remote), cwd=root)
        self.git('clone', '-q', str(self.remote), str(self.repo), cwd=root)
        self.git('config', 'user.name', 'Tyler Bush')
        self.git('config', 'user.email', 'tyler.jonathan.bush@gmail.com')
        self.git('checkout', '-qb', 'main')
        (self.repo/'common').write_text('base\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'main')
        self.heads = []
        for n in (1, 2):
            self.git('checkout', '-qb', f'work/{n}', self.base)
            (self.repo/('common' if conflict else f'feature-{n}')).write_text(f'feature{n}\n')
            self.git('add', '.'); self.git('commit', '-qm', f'feature {n}')
            self.heads.append(self.git('rev-parse', 'HEAD'))
            self.git('push', '-q', 'origin', f'work/{n}')
        self.git('checkout', '-q', 'main')
        self.state = root/'state'; self.state.mkdir()
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.execute('CREATE TABLE jobs(project TEXT,pr INTEGER,kind TEXT,status TEXT)')
        self.db.execute('CREATE TABLE publications(project TEXT,pr INTEGER,node_id TEXT PRIMARY KEY,hash TEXT)')
        self.db.execute('CREATE TABLE integrations(project TEXT,pr INTEGER,head TEXT,commit_id TEXT,reconciled INTEGER,PRIMARY KEY(project,pr))')
        self.store = types.SimpleNamespace(db=self.db, review=self.review)
        self.project = dict(id='test', root=str(self.repo), repository='owner/repo',review_github_account='reviewer',
                            integration_batch=dict(enabled=True,max_members=2,collection_seconds=0))
        self.owners = []
        self.source_prs = {}
        self.receipts = {}
        for n, head in enumerate(self.heads, 1):
            owner = dict(key=f'T-{n}',agent='Codex',model='test-model',session=f'session-{n}',
                         branch=f'work/{n}',token=f'private-{n}',status='review',expired=False,
                         publish_pending=False,spec=dict(acceptance=[f'acceptance {n}']))
            self.owners.append(owner)
            pr = self.make_pr(n,head,f'work/{n}')
            pr['_review_mode'] = 'source'
            self.source_prs[n] = pr
            self.receipts[n] = dict(job=n,family='Claude',head=head,base=self.base,
                source_feedback=coordinator_module.source_feedback(pr),evidence_hash='d'*64)
        self.formal_reviews = {1:[],2:[]}
        self.approval_posts = []
        self.candidate_prs = {}
        self.approvals = {}
        self.launched, self.reconciled, self.events, self.review_assertions = [], [], [], []
        self.controller = BatchController(self)

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git',*args],cwd=cwd or self.repo,text=True,stderr=subprocess.DEVNULL).strip()

    def make_pr(self, number, head, branch):
        return dict(number=number,headRefOid=head,headRefName=branch,baseRefName='main',baseRefOid=self.base,
                    baseIncluded=True,state='OPEN',isDraft=False,isCrossRepository=False,mergeable='MERGEABLE',
                    title=f'Feature {number}',body='',comments=[],reviews=[],threads=[],statusCheckRollup=[],
                    _testnet_required=True,_testnet_status=dict(state='pending',description='base:'+self.base))

    def owner(self, project, pr, owners):
        return next((o for o in owners if o['branch']==pr['headRefName']),None)

    def ticket(self, project, command, key, *args):
        owner = next(o for o in self.owners if o['key']==key)
        if command == 'show':return json.dumps(owner)
        if command == 'heartbeat':
            raise RuntimeError('Only implementation/blocked leases need heartbeats')
        raise AssertionError(command)

    def assert_review_owner(self, project, owner):
        self.review_assertions.append(owner['key'])
        if owner['status']!='review' or owner['expired'] or owner['publish_pending']:
            raise RuntimeError('bad ownership')

    def source_receipt(self, project, pr, owner):
        receipt=self.receipts.get(pr['number'])
        if not receipt or receipt['source_feedback']!=coordinator_module.source_feedback(pr):return None
        return copy.deepcopy(receipt)

    def pr(self, project, number):
        pr=copy.deepcopy(self.source_prs.get(number) or self.candidate_prs[number])
        if number in self.source_prs:
            pr['reviews'] += [dict(id=r['node_id'],state=r['state'],body=r['body'],author=dict(login=r['user']['login']),commit=dict(oid=r['commit_id'])) for r in self.formal_reviews[number]]
            pr['_receipts']={r[0]:r[1] for r in self.db.execute('select node_id,hash from publications where project=? and pr=?',('test',number))}
        tip=self.git('rev-parse','refs/heads/main',cwd=self.remote)
        pr['baseRefOid']=tip
        if pr['state']=='OPEN':
            present=subprocess.run(['git','merge-base','--is-ancestor',pr['headRefOid'],tip],cwd=self.repo,stderr=subprocess.DEVNULL).returncode==0
            if present:
                pr.update(state='MERGED',mergeCommit=dict(oid=pr['headRefOid']))
        return pr

    def gh(self, project, args):
        self.events.append(args)
        endpoint=args[1]
        fields=dict(a.split('=',1) for a in args if '=' in a and not a.startswith('--'))
        if endpoint=='user':return json.dumps(dict(login=project['github_account']))
        if '/reviews' in endpoint:
            n=int(endpoint.split('/pulls/')[1].split('/')[0])
            if 'POST' in args:
                assert project['github_account']=='reviewer'
                count=sum(map(len,self.formal_reviews.values()))+1
                record=dict(id=1000+count,node_id=f'approval-{count}',body=fields['body'],
                            commit_id=fields['commit_id'],state='APPROVED',user=dict(login='reviewer'))
                self.formal_reviews[n].append(record);self.approval_posts.append(record)
                return json.dumps(record)
            prior=[dict(id=500+i,node_id=r['id'],body=r.get('body',''),commit_id=r.get('commit',{}).get('oid'),
                        state=r['state'],user=dict(login=r['author']['login'])) for i,r in enumerate(self.source_prs[n]['reviews'])]
            return json.dumps([prior+self.formal_reviews[n]])
        if '/pulls/' in endpoint and '--method' not in args:
            n=int(endpoint.rsplit('/',1)[1]);return json.dumps(dict(state='open',user=dict(login='owner'),
                head=dict(sha=self.source_prs[n]['headRefOid'],repo=dict(full_name='owner/repo'))))
        if '--method' in args and args[args.index('--method')+1]=='POST':
            assert endpoint=='repos/owner/repo/pulls'
            n=100+len(self.candidate_prs)
            head=self.git('rev-parse','refs/heads/'+fields['head'],cwd=self.remote)
            pr=self.make_pr(n,head,fields['head']);self.candidate_prs[n]=pr
            return json.dumps(dict(number=n))
        if '--method' in args:
            n=int(endpoint.rsplit('/',1)[-1]);self.candidate_prs[n]['state']=fields['state'].upper();return '{}'
        return json.dumps([dict(number=n,head=dict(sha=p['headRefOid']),base=dict(ref='main'))
                           for n,p in self.candidate_prs.items() if p['state']=='OPEN'])

    def review(self, project, number):
        return self.approvals.get(number)

    def launch(self, project, pr, owner, kind):
        self.launched.append((copy.deepcopy(pr),copy.deepcopy(owner),kind))

    def reconcile(self, project, pr, owner, review):
        if pr['number'] not in self.reconciled:self.reconciled.append(pr['number'])
        owner['status']='done'
        self.db.execute('update integrations set reconciled=1 where project=? and pr=?',('test',pr['number']))

    def row(self):return self.controller.rows(self.project)[-1]
    def tick(self):return self.controller.tick(self.project,[self.pr(self.project,n) for n in self.source_prs],self.owners,True)

    def green(self):
        row=self.row();p=self.candidate_prs[row['pr']]
        p['statusCheckRollup']=[dict(__typename='CheckRun',name='check',workflowName='CI',status='COMPLETED',conclusion='SUCCESS')]
        p['_testnet_status']=dict(state='success',description='base:'+row['base'])

    def approve(self):
        row=self.row();pr=self.controller.candidate(self.project,row)
        self.approvals[row['pr']]=dict(id=777,head=pr['headRefOid'],feedback=coordinator_module.feedback(pr),
            family='Claude',result=dict(verdict='approve',acceptance_complete=True,model='test-reviewer',evidence='combined review'))


class BatchTests(unittest.TestCase):
    def fixture(self, conflict=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return Fixture(Path(temp.name),conflict)

    def test_disabled_has_no_external_effect(self):
        f=self.fixture();f.project['integration_batch']['enabled']=False
        self.assertEqual(f.tick(),set());self.assertEqual(f.controller.rows(f.project),[])

    def test_already_present_head_does_not_fence_unrelated_batch_members(self):
        f=self.fixture();f.git('push','-q','origin',f.heads[0]+':refs/heads/main')
        f.base=f.heads[0]
        for pr in f.source_prs.values():pr['baseRefOid']=f.base
        f.tick();row=f.row()
        self.assertEqual(row['status'],'awaiting_ci')
        self.assertEqual([m['pr'] for m in json.loads(row['members'])],[2])

    def test_two_sources_one_combined_gate_and_publication(self):
        f=self.fixture();self.assertEqual(f.tick(),{1,2});row=f.row()
        self.assertEqual(row['status'],'awaiting_ci');self.assertEqual(len(f.candidate_prs),1)
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.tick();self.assertFalse(f.launched)
        f.green();f.tick();self.assertEqual(len(f.launched),1)
        self.assertEqual(f.launched[0][1]['key'],'integration:'+row['id'])
        self.assertNotIn('token',f.launched[0][1])
        f.approve();f.tick();row=f.row()
        self.assertEqual(row['status'],'done');self.assertEqual(f.reconciled,[1,2])
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),row['head'])
        members=json.loads(row['members']);previous=f.base
        for m in members:
            self.assertEqual(f.git('show','-s','--format=%P',m['commit']).split(),[previous,m['head']])
            self.assertEqual(f.git('show','-s','--format=%an <%ae>|%cn <%ce>',m['commit']),
                'Tyler Bush <tyler.jonathan.bush@gmail.com>|Tyler Bush <tyler.jonathan.bush@gmail.com>')
            previous=m['commit']
        self.assertEqual(row['manifest_digest'],manifest_digest('test',row['id'],f.base,row['head'],members))
        self.assertEqual(f.db.execute('select count(*) from batch_activation_commits').fetchone()[0],2)
        self.assertEqual(f.db.execute('select count(*) from integrations where reconciled=1').fetchone()[0],3)
        self.assertEqual(f.db.execute("select count(*) from batch_member_approvals where status='applied'").fetchone()[0],2)
        self.assertTrue(f.review_assertions)
        self.assertEqual([f.git('rev-parse',f'work/{n}') for n in (1,2)],f.heads)

    def test_review_claim_validation_replaces_heartbeat_in_both_paths(self):
        f=self.fixture()
        # Match ticket's actual contract: Review has no renewable lease.
        with self.assertRaisesRegex(RuntimeError, 'Only implementation/blocked'):
            f.ticket(f.project, 'heartbeat', 'T-1', '--token', 'private-1')
        self.assertEqual(f.tick(), {1,2})
        self.assertEqual(f.row()['status'], 'awaiting_ci')
        f.review_assertions.clear()
        f.controller.validate_members(f.project, f.row())
        self.assertEqual(f.review_assertions, ['T-1','T-2'])
        self.assertFalse(f.reconciled)

    def test_invalid_review_owner_does_not_block_other_candidates(self):
        for field, value in [('status','in_progress'), ('expired',True), ('publish_pending',True)]:
            with self.subTest(field=field):
                f=self.fixture()
                original=f.assert_review_owner
                def validate(project, owner):
                    if owner['key']=='T-1':
                        changed=copy.deepcopy(owner);changed[field]=value
                        return original(project, changed)
                    return original(project, owner)
                with patch.object(f, 'assert_review_owner', side_effect=validate):
                    self.assertEqual(f.tick(), {2})
                self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])], [2])
                self.assertEqual(f.row()['status'], 'awaiting_ci')
                self.assertFalse(f.reconciled)

    def test_manifest_revalidates_review_ownership_before_publication(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve()
        original=f.assert_review_owner
        def validate(project, owner):
            if owner['key']=='T-1':raise RuntimeError('review ownership changed or is unpublished')
            return original(project, owner)
        with patch.object(f, 'assert_review_owner', side_effect=validate):
            f.tick()
        self.assertEqual(f.row()['status'], 'invalidated')
        self.assertFalse(f.reconciled)
        self.assertFalse(f.approval_posts)
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)

    def test_combined_only_failure_splits_without_source_mutation(self):
        f=self.fixture();f.tick();row=f.row()
        p=f.candidate_prs[row['pr']]
        p['statusCheckRollup']=[dict(__typename='CheckRun',name='check',workflowName='CI',status='COMPLETED',conclusion='FAILURE',detailsUrl='https://github.com/owner/repo/actions/runs/10/job/20')]
        f.tick();self.assertEqual(f.row()['status'],'failed');self.assertFalse(f.reconciled)
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.tick();self.assertEqual(len(json.loads(f.row()['members'])),1)
        self.assertEqual([p['headRefOid'] for p in f.source_prs.values()],f.heads)

    def test_conflict_is_excluded_but_other_source_can_proceed(self):
        f=self.fixture(True);f.tick();row=f.row();members=json.loads(row['members'])
        self.assertEqual([m['pr'] for m in members],[1])
        ctx=f.controller.repair_context(f.project,f.source_prs[2])
        self.assertIn('_batch_conflict',ctx)
        self.assertEqual(ctx['_batch_conflict']['candidate_base'],f.base)
        f.green();f.tick();f.approve();f.tick();self.assertEqual(f.reconciled,[1])

    def test_changed_head_feedback_owner_and_main_invalidate(self):
        for change in ('head','feedback','owner','main'):
            with self.subTest(change=change):
                f=self.fixture();f.tick();old=f.row()
                if change=='head':f.source_prs[1]['headRefOid']='a'*40
                elif change=='feedback':f.source_prs[1]['comments'].append(dict(id='new',body='new request',updatedAt='now'))
                elif change=='owner':f.owners[0]['token']='changed'
                else:
                    (f.repo/'external').write_text('external')
                    f.git('add','.');f.git('commit','-qm','external main');f.git('push','-q','origin','main')
                f.tick();self.assertEqual(f.row()['status'],'invalidated')
                self.assertEqual(f.candidate_prs[old['pr']]['state'],'CLOSED')
                self.assertFalse(f.reconciled)

    def test_missing_testnet_and_changed_review_never_publish(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve()
        row=f.row();f.candidate_prs[row['pr']]['_testnet_status']['description']='base:'+'e'*40
        f.tick();self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.green();f.candidate_prs[row['pr']]['comments'].append(dict(id='q',body='new concern',updatedAt='now'))
        f.tick();self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)

    def test_publication_uncertainty_never_blindly_repushes(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();original=f.controller.git
        attempts=[]
        def stop(project,*args,**kwargs):
            if args[:2]==('push','origin') and args[-1].endswith(':refs/heads/main'):
                attempts.append(args);raise subprocess.CalledProcessError(1,['git','push'])
            return original(project,*args,**kwargs)
        with patch.object(f.controller,'git',side_effect=stop):f.tick()
        self.assertEqual(f.row()['status'],'publish_pending')
        with patch.object(f.controller,'git',side_effect=stop):f.tick()
        self.assertEqual(len(attempts),1);self.assertFalse(f.reconciled)

    def test_restart_after_push_reconciles_without_second_push(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();original=f.controller.git
        attempts=[]
        def crash(project,*args,**kwargs):
            value=original(project,*args,**kwargs)
            if args[:2]==('push','origin') and args[-1].endswith(':refs/heads/main'):
                attempts.append(args);raise RuntimeError('lost response after successful push')
            return value
        with patch.object(f.controller,'git',side_effect=crash):f.tick()
        self.assertEqual(f.row()['status'],'publish_pending')
        f.controller=BatchController(f);f.tick()
        self.assertEqual(f.row()['status'],'done');self.assertEqual(f.reconciled,[1,2]);self.assertEqual(len(attempts),1)
        f.tick();self.assertEqual(f.reconciled,[1,2])

    def test_ambiguous_candidate_post_is_adopted_not_reposted(self):
        f=self.fixture();original=f.gh;posts=[]
        def lose(project,args):
            result=original(project,args)
            if 'POST' in args:posts.append(args);raise RuntimeError('response lost')
            return result
        with patch.object(f,'gh',side_effect=lose):f.tick()
        self.assertEqual(f.row()['status'],'pr_creating')
        f.controller=BatchController(f);f.tick()
        self.assertEqual(f.row()['status'],'awaiting_ci');self.assertEqual(len(posts),1)

    def test_single_candidate_failure_is_stable_owner_repair_context(self):
        f=self.fixture();f.project['integration_batch']['max_members']=1;f.tick();row=f.row()
        f.candidate_prs[row['pr']]['statusCheckRollup']=[dict(__typename='CheckRun',name='check',workflowName='CI',
            status='COMPLETED',conclusion='FAILURE',detailsUrl='https://github.com/owner/repo/actions/runs/10/job/20')]
        f.tick();ctx=f.controller.repair_context(f.project,f.source_prs[1])
        self.assertEqual(ctx['_batch_failure']['candidate_head'],row['head'])
        self.assertEqual(ctx['_batch_failure']['checks'][0]['conclusion'],'FAILURE')
        changed=copy.deepcopy(f.source_prs[1]);changed['headRefOid']='a'*40
        self.assertEqual(f.controller.repair_context(f.project,changed),{})
        f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])

    def test_candidate_checkout_disables_hooks_and_configured_filters(self):
        f=self.fixture();marker=f.root/'must-not-run'
        f.git('checkout','-q','work/1')
        (f.repo/'.gitattributes').write_text('*.txt filter=canary\n')
        (f.repo/'value.txt').write_text('value\n')
        f.git('add','.');f.git('commit','-qm','attributes fixture');f.git('push','-q','origin','work/1')
        head=f.git('rev-parse','HEAD');f.heads[0]=head;f.source_prs[1]['headRefOid']=head
        f.receipts[1].update(head=head,source_feedback=coordinator_module.source_feedback(f.source_prs[1]))
        f.git('checkout','-q','main')
        f.git('config','filter.canary.smudge',f'echo BAD > {marker}; cat')
        f.git('config','filter.canary.clean',f'echo BAD > {marker}; cat')
        f.git('config','filter.canary.required','true')
        for name in ('post-checkout','pre-merge-commit','post-merge'):
            hook=f.repo/'.git/hooks'/name;hook.write_text(f'#!/bin/sh\necho BAD > {marker}\n');hook.chmod(0o755)
        f.tick();self.assertEqual(f.row()['status'],'awaiting_ci')
        self.assertFalse(marker.exists())

    def test_collection_deadline_and_namespace_are_bounded(self):
        f=self.fixture();f.project['integration_batch'].update(max_members=4,collection_seconds=120)
        f.controller.clock=lambda:1000
        f.tick();self.assertEqual(f.row()['status'],'collecting');self.assertFalse(f.candidate_prs)
        f.controller.clock=lambda:1121
        f.tick();self.assertEqual(f.row()['status'],'awaiting_ci')
        for bad in [0,9,True]:
            f.project['integration_batch']['max_members']=bad
            with self.assertRaises(ValueError):f.tick()

    def test_tampered_manifest_and_same_family_approval_fail_closed(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();row=f.row()
        f.approvals[row['pr']]['family']='Codex'
        f.tick();self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.approvals[row['pr']]['family']='Claude'
        members=json.loads(row['members']);members[0]['title']='tampered'
        f.db.execute('update integration_batches set members=? where project=? and id=?',
                     (canonical(members),'test',row['id']))
        f.tick();self.assertNotEqual(f.row()['status'],'done')
        self.assertIn('manifest digest mismatch', f.row()['error'])
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)

    def test_pr_creation_uncertainty_with_no_server_object_is_fenced(self):
        f=self.fixture();original=f.gh;posts=[]
        def lose(project,args):
            if 'POST' in args:posts.append(args);raise RuntimeError('request outcome unavailable')
            return original(project,args)
        with patch.object(f,'gh',side_effect=lose):f.tick()
        self.assertEqual(f.row()['status'],'pr_creating')
        f.tick();self.assertEqual(len(posts),1);self.assertEqual(f.row()['status'],'pr_creating')
        self.assertIn('ambiguous',f.row()['error'])

    def test_unpublished_build_failure_retries_safely(self):
        f=self.fixture();original=f.controller.git
        def fail(project,*args,**kwargs):
            if args[:2]==('worktree','add'):raise RuntimeError('unexpected local failure')
            return original(project,*args,**kwargs)
        with patch.object(f.controller,'git',side_effect=fail):f.tick()
        self.assertEqual(f.row()['status'],'build_uncertain')
        f.tick();self.assertEqual(len(f.controller.rows(f.project)),1)
        self.assertEqual(f.row()['status'],'awaiting_ci')

    def change_commit(self, f, message, foreign=False):
        f.git('checkout', '-q', 'work/1')
        args = ['commit', '--amend', '-qm', message]
        if foreign:
            args += ['--author', 'Other User <other@example.com>']
        f.git(*args)
        head = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', '--force', 'origin', 'work/1')
        f.git('checkout', '-q', 'main')
        f.heads[0] = head
        f.source_prs[1]['headRefOid'] = head
        f.receipts[1].update(head=head, source_feedback=coordinator_module.source_feedback(f.source_prs[1]))

    def test_authorship_rejections_route_repair_without_churn(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                f=self.fixture()
                self.change_commit(f, 'feature\n\nCo-Authored-By: Model' if not foreign else 'feature', foreign)
                # Leave only the rejected member to reproduce repeated empty builds.
                del f.source_prs[2]
                f.tick()
                self.assertEqual(f.row()['status'], 'failed')
                self.assertFalse(Path(f.row()['directory']).exists())
                context=f.controller.repair_context(f.project, f.pr(f.project,1))
                self.assertIn('authorship' if not foreign else 'author/committer', context['_batch_conflict']['reason'])
                for _ in range(4):
                    self.assertEqual(f.tick(), set())
                self.assertEqual(len(f.controller.rows(f.project)), 1)
                self.assertEqual(f.git('rev-parse','main',cwd=f.remote), f.base)
                self.assertEqual(f.git('worktree','list','--porcelain').count('worktree '), 1)
                self.assertFalse(f.candidate_prs)

    def test_rejected_member_does_not_prevent_valid_member_publication(self):
        f=self.fixture();self.change_commit(f, 'feature\n\nCo-Authored-By: Model')
        f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])
        f.green();f.tick();f.approve();f.tick()
        self.assertEqual(f.row()['status'],'done')
        self.assertEqual(f.reconciled,[2])
        self.assertNotIn('co-authored-by:', f.git('log','--format=%B',f.base+'..'+f.row()['head']).lower())
        self.assertEqual(subprocess.run(['git','merge-base','--is-ancestor',f.heads[0],f.row()['head']],cwd=f.repo).returncode,1)

    def test_terminal_worktrees_are_removed(self):
        for outcome in ('done', 'failed', 'invalidated'):
            with self.subTest(outcome=outcome):
                f=self.fixture();f.tick();directory=Path(f.row()['directory'])
                self.assertTrue(directory.exists())
                if outcome=='done':
                    f.green();f.tick();f.approve()
                elif outcome=='failed':
                    f.candidate_prs[f.row()['pr']]['statusCheckRollup']=[dict(__typename='CheckRun',name='check',workflowName='CI',status='COMPLETED',conclusion='FAILURE',detailsUrl='https://github.com/owner/repo/actions/runs/10/job/20')]
                else:
                    f.owners[0]['token']='changed'
                f.tick()
                self.assertEqual(f.row()['status'], outcome)
                self.assertFalse(directory.exists())
                self.assertEqual(f.git('worktree','list','--porcelain').count('worktree '),1)

    def test_transient_member_read_and_launch_errors_keep_completed_candidate(self):
        for target in ('pr', 'launch'):
            with self.subTest(target=target):
                f=self.fixture();f.tick();f.green();before=f.row()
                with patch.object(f, target, side_effect=subprocess.CalledProcessError(1, ['gh'])):
                    if target=='pr':
                        # Snapshot collection is outside tick's state transition.
                        prs=list(f.source_prs.values())
                        f.controller.tick(f.project,prs,f.owners,True)
                    else:
                        f.tick()
                after=f.row()
                self.assertEqual(after['pr'],before['pr'])
                self.assertEqual(after['head'],before['head'])
                self.assertIn(after['status'], ('awaiting_ci','awaiting_review'))
                self.assertEqual(f.candidate_prs[before['pr']]['state'],'OPEN')
                self.assertIn('CalledProcessError', after['error'])
                f.tick();f.approve();f.tick()
                self.assertEqual(f.row()['status'],'done')
                self.assertEqual(len(f.candidate_prs),1)

    def test_interrupted_build_with_existing_worktree_recovers_without_new_epoch(self):
        f=self.fixture();original=f.controller.git
        def interrupt(project,*args,**kwargs):
            value=original(project,*args,**kwargs)
            if args[:2]==('worktree','add'):
                raise KeyboardInterrupt()
            return value
        with patch.object(f.controller,'git',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):f.tick()
        self.assertEqual(f.row()['status'],'building')
        self.assertTrue(Path(f.row()['directory']).exists())
        f.controller=BatchController(f);f.tick()
        self.assertEqual(f.row()['status'],'awaiting_ci')
        self.assertEqual(len(f.controller.rows(f.project)),1)

    def test_unexpected_published_branch_fences_build_recovery(self):
        f=self.fixture();original=f.controller.git
        with patch.object(f.controller,'build',side_effect=RuntimeError('interrupted')):
            f.tick()
        f.git('push','origin',f.base+':refs/heads/'+f.row()['branch'])
        f.tick()
        self.assertEqual(f.row()['status'],'building')
        self.assertIn('publication evidence',f.row()['error'])
        self.assertFalse(f.candidate_prs)

    def test_batches_split_by_owner_family_and_reviewer_is_independent(self):
        f=self.fixture();f.owners[0]['agent']='Claude';f.receipts[1]['family']='Codex'
        f.tick();row=f.row()
        self.assertEqual([m['pr'] for m in json.loads(row['members'])],[1])
        self.assertEqual(f.controller.synthetic_owner(row)['agent'],'Claude')
        f.green();f.tick();f.approve()
        # A same-family approval cannot publish the Claude-owned source.
        f.tick();self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.approvals[row['pr']]['family']='Codex'
        f.tick();self.assertEqual(f.row()['status'],'done')
        f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])
        self.assertEqual(f.controller.synthetic_owner(f.row())['agent'],'Codex')

    def add_request(self, f, author='human', known=False):
        review=dict(id='original-request',state='CHANGES_REQUESTED',body='Please fix the source',
                    author=dict(login=author),commit=dict(oid=f.heads[0]))
        f.source_prs[1]['reviews'].append(review)
        if known:
            f.db.execute('insert into publications values(?,?,?,?)',
                ('test',1,review['id'],coordinator_module.publication_hash(review)))
        f.receipts[1]['source_feedback']=coordinator_module.source_feedback(f.pr(f.project,1))
        return review

    def test_human_change_request_blocks_even_with_typed_source_proof(self):
        f=self.fixture();self.add_request(f)
        f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])
        self.assertFalse(f.approval_posts)

    def test_edited_or_other_account_coordinator_request_is_not_superseded(self):
        for change in ('edited','other-account'):
            with self.subTest(change=change):
                f=self.fixture();review=self.add_request(f,author='reviewer' if change=='edited' else 'old-reviewer',known=True)
                if change=='edited':review['body']='Edited by a human'
                f.receipts[1]['source_feedback']=coordinator_module.source_feedback(f.pr(f.project,1))
                f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])

    def test_known_request_superseded_only_after_complete_combined_acceptance(self):
        f=self.fixture();self.add_request(f,author='reviewer',known=True)
        f.tick();self.assertFalse(f.approval_posts)
        f.green();f.tick();self.assertFalse(f.approval_posts)
        f.approve();f.tick();self.assertEqual(f.row()['status'],'done')
        self.assertEqual(len(f.approval_posts),2)
        rows=f.controller.approval_rows(f.row())
        for row in rows:
            self.assertEqual(row['account'],'reviewer');self.assertEqual(row['status'],'applied')
            self.assertEqual(row['head'],f.heads[row['pr']-1])
            self.assertEqual(row['body_hash'],hashlib.sha256(row['body'].encode()).hexdigest())
            self.assertIn(f.row()['head'],row['body']);self.assertIn(f.row()['base'],row['body'])
            self.assertIn('job 777',row['body']);self.assertIn('does not claim an additional model review run',row['body'])

    def test_ambiguous_member_approval_adopts_exact_server_record_once(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();original=f.gh;lost=[]
        def lose(project,args):
            response=original(project,args)
            if '/reviews' in args[1] and 'POST' in args and not lost:
                lost.append(True);raise RuntimeError('approval response lost')
            return response
        with patch.object(f,'gh',side_effect=lose):f.tick()
        self.assertEqual(f.row()['status'],'approving_members');self.assertEqual(len(f.approval_posts),1)
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)
        f.controller=BatchController(f);f.tick()
        self.assertEqual(f.row()['status'],'done');self.assertEqual(len(f.approval_posts),2)
        self.assertEqual(f.reconciled,[1,2])

    def test_ambiguous_member_approval_without_server_record_is_fenced(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();original=f.gh;attempts=[]
        def lose(project,args):
            if '/reviews' in args[1] and 'POST' in args:
                attempts.append(args);raise RuntimeError('approval outcome unknown')
            return original(project,args)
        with patch.object(f,'gh',side_effect=lose):f.tick()
        f.tick();self.assertEqual(f.row()['status'],'approving_members');self.assertEqual(len(attempts),1)
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)

    def test_new_human_request_blocks_publication_despite_cached_pr_snapshot(self):
        f=self.fixture();f.tick();f.green();f.tick();f.approve();original=f.gh;pr_reader=f.pr
        cached={n:f.pr(f.project,n) for n in f.source_prs}
        def cached_pr(project,number):
            return copy.deepcopy(cached[number]) if number in cached else pr_reader(project,number)
        def human_request(project,args):
            response=original(project,args)
            if '/pulls/1/reviews' in args[1] and 'POST' in args:
                f.formal_reviews[1].append(dict(id=9999,node_id='human-review',state='CHANGES_REQUESTED',
                    body='Stop integration',commit_id=f.heads[0],user=dict(login='human')))
            return response
        with patch.object(f,'gh',side_effect=human_request),patch.object(f,'pr',side_effect=cached_pr):f.tick()
        self.assertEqual(f.row()['status'],'invalidated')
        self.assertEqual(f.git('rev-parse','main',cwd=f.remote),f.base)

    def test_priority_precedes_old_pr_number(self):
        f=self.fixture();f.project['integration_batch']['max_members']=1
        f.owners[0]['spec']['priority']='P2';f.owners[1]['spec']['priority']='P0'
        f.tick();self.assertEqual([m['pr'] for m in json.loads(f.row()['members'])],[2])

    def test_expired_claim_and_active_fix_are_never_selected(self):
        f=self.fixture();f.owners[0]['expired']=True
        f.db.execute("insert into jobs values('test',2,'fix','running')")
        self.assertEqual(f.tick(),set());self.assertEqual(f.controller.rows(f.project),[])


if __name__=='__main__':unittest.main()
