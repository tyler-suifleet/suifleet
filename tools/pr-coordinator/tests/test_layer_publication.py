"""Offline tests for the dedicated standalone promotion route."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import (
    CORE_REPOSITORY, LAYER_PROJECT, LAYER_REPOSITORY, LAYER_REPOSITORY_ID,
    LAYER_REFS, LAYER_SERIES, LAYER_WORKFLOW, LAYER_WORKFLOW_BLOB, LayerPublication,
    feedback, validate_layer_config,
)

HEAD, BASE, CORE = '1' * 40, '2' * 40, '3' * 40
ISSUE = f'https://github.com/{CORE_REPOSITORY}/issues/1142'
IDENTITY = dict(name='Tyler Bush', email='tyler.jonathan.bush@gmail.com')
BRANCH = 'publication/reviewed-layer'
ROOT = Path(__file__).resolve().parents[3]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.project = dict(
            id=LAYER_PROJECT, root=str(ROOT), repository=LAYER_REPOSITORY,
            github_account='maintainer', review_github_account='reviewer',
            layer_publication=dict(core_ticket='SF-340', core_pr=21, core_revision=CORE,
                                   core_root=str(ROOT), token_file='/private/token',
                                   app_id=5432, pr_author='preparer', pulls={'kirkstone': 4}),
        )
        self.coordinator = Mock()
        self.route = LayerPublication(self.coordinator, self.project)
        self.route.read = Mock()
        self.pr = dict(number=4, headRefOid=HEAD, baseRefOid=BASE, headRefName=BRANCH,
                       baseRefName='kirkstone', isCrossRepository=False, state='OPEN',
                       isDraft=False, baseIncluded=True, mergeable='MERGEABLE', threads=[],
                       comments=[], reviews=[], statusCheckRollup=[
                           dict(__typename='CheckRun', status='COMPLETED', conclusion='SUCCESS')])
        self.source = dict(
            user=dict(login='preparer'),
            head=dict(repo=dict(id=LAYER_REPOSITORY_ID), sha=HEAD, ref=BRANCH),
            base=dict(repo=dict(id=LAYER_REPOSITORY_ID), ref='kirkstone'),
        )
        self.entries = {LAYER_WORKFLOW: ('100644', 'blob', LAYER_WORKFLOW_BLOB)}
        self.commit = dict(author=IDENTITY, committer=IDENTITY, parents=[dict(sha=BASE)], message='Export')
        self.tree = dict(truncated=False, tree=[
            dict(path=LAYER_WORKFLOW, mode='100644', type='blob', sha=LAYER_WORKFLOW_BLOB),
        ])

    def test_only_dedicated_route_and_no_dispatch_slot(self):
        config = dict(projects=[self.project])
        validate_layer_config(config)
        for change in ({'id': 'other'}, {'repository': 'someone/meta-suifleet'},
                       {'integration_batch': {'enabled': True}},
                       {'local_infrastructure': {'enabled': True}}, {'review_github_account': 'PREPARER'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_layer_config(dict(projects=[{**self.project, **change}]))
        with self.assertRaises(ValueError):
            validate_layer_config(dict(projects=[self.project, self.project]))
        with self.assertRaises(ValueError):
            validate_layer_config({**config, 'implementation_slots': [{'project': LAYER_PROJECT}]})

    def test_checkout_identity_includes_push_url_and_github_id(self):
        url = f'https://github.com/{LAYER_REPOSITORY}.git'
        repo = dict(id=LAYER_REPOSITORY_ID, full_name=LAYER_REPOSITORY, default_branch='main',
                    archived=False, private=False)
        self.route.read.return_value = repo
        with patch('coordinator.run', side_effect=[str(ROOT), str(ROOT / '.git'), url, url]):
            self.route.identity()
        with patch('coordinator.run', side_effect=[str(ROOT), str(ROOT / '.git'), url, 'https://elsewhere.invalid']):
            with self.assertRaisesRegex(RuntimeError, 'origin identity'):
                self.route.identity()
        repo['id'] += 1
        with patch('coordinator.run', side_effect=[str(ROOT), str(ROOT / '.git'), url, url]):
            with self.assertRaisesRegex(RuntimeError, 'GitHub repository identity'):
                self.route.identity()

    def test_all_four_refs_require_sole_app_and_non_bypassable_guards(self):
        rules = [
            dict(type='update', ruleset_source_type='Repository', ruleset_id=1,
                 parameters=dict(update_allows_fetch_and_merge=False)),
            *[dict(type=kind, ruleset_id=2) for kind in ('creation', 'deletion', 'non_fast_forward')],
        ]
        update = dict(target='branch', enforcement='active', bypass_actors=[
            dict(actor_type='Integration', actor_id=5432, bypass_mode='always')])
        guard = dict(bypass_actors=[])
        missing = set()

        def read(path):
            if '/rules/branches/' in path:
                return [] if path.rsplit('/', 1)[1] in missing else rules
            return update if path.endswith('/1') else guard

        self.route.read.side_effect = read
        self.route.protections()
        for ref in LAYER_REFS:
            missing.add(ref)
            with self.assertRaisesRegex(RuntimeError, ref):
                self.route.protections()
            missing.clear()
        guard['bypass_actors'] = update['bypass_actors']
        with self.assertRaisesRegex(RuntimeError, 'sole coordinator'):
            self.route.protections()
        guard['bypass_actors'] = []
        update['bypass_actors'].append(dict(actor_type='RepositoryRole', actor_id=5, bypass_mode='always'))
        with self.assertRaisesRegex(RuntimeError, 'sole coordinator'):
            self.route.protections()

    def test_core_ticket_done_and_exact_merged_core_pr(self):
        registry = [dict(project_id='suifleet', repository=CORE_REPOSITORY, root=str(ROOT))]
        owner = dict(key='SF-340', project='suifleet', canonical_root=str(ROOT), status='done',
                     publish_pending=False, expired=False, agent='Codex', branch='work/suifleet/sf-340',
                     remote=dict(url=ISSUE))
        pr = dict(merged=True, base=dict(ref='main', repo=dict(full_name=CORE_REPOSITORY)),
                  head=dict(ref=owner['branch'], repo=dict(full_name=CORE_REPOSITORY)),
                  merge_commit_sha=CORE, body=f'Closes {ISSUE}')
        self.route.read.return_value = pr

        def check():
            with patch('coordinator.run', side_effect=[json.dumps(registry), json.dumps(owner)]):
                return self.route.core_owner()

        self.assertEqual(check()['status'], 'review')
        self.assertEqual(owner['status'], 'done')
        for key, bad in [('status', 'review'), ('publish_pending', True), ('expired', True)]:
            saved, owner[key] = owner[key], bad
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                check()
            owner[key] = saved
        pr['merge_commit_sha'] = HEAD
        with self.assertRaisesRegex(RuntimeError, 'pinned merged'):
            check()
        registry.append(dict(project_id='alias', repository=LAYER_REPOSITORY, root='/alias'))
        with self.assertRaisesRegex(RuntimeError, 'collides'):
            check()

    def test_candidate_requires_same_repo_exact_parent_and_complete_reviewed_tree(self):
        self.coordinator.pr.side_effect = lambda *args: deepcopy(self.pr)
        self.route.read.return_value = self.source
        self.route.series_run = Mock(return_value={'id': 8, 'attempt': 2})
        self.coordinator.immutable_read.side_effect = lambda _p, endpoint: (
            self.tree if '/trees/' in endpoint else self.commit)
        args = ('kirkstone', 4, self.entries)
        self.assertEqual(self.route.candidate(*args)['_layer_publication']['core_revision'], CORE)
        self.source['head']['repo']['id'] += 1
        with self.assertRaisesRegex(RuntimeError, 'PR identity'):
            self.route.candidate(*args)
        self.source['head']['repo']['id'] -= 1
        self.commit['parents'][0]['sha'] = CORE
        with self.assertRaisesRegex(RuntimeError, 'exact current base'):
            self.route.candidate(*args)
        self.commit['parents'][0]['sha'] = BASE
        self.tree['tree'].append(dict(path='extra', mode='100644', type='blob', sha=HEAD))
        with self.assertRaisesRegex(RuntimeError, 'reviewed core export'):
            self.route.candidate(*args)

    def test_only_latest_matching_successful_run_with_every_series_passes(self):
        jobs = [dict(name=f'Layer and packages ({series})', head_sha=HEAD, run_id=8,
                     status='completed', conclusion='success') for series in LAYER_SERIES]
        run = dict(id=8, run_attempt=2, path=LAYER_WORKFLOW + '@refs/pull/4/merge', event='pull_request', head_sha=HEAD,
                   head_repository=dict(id=LAYER_REPOSITORY_ID), status='completed', conclusion='success',
                   pull_requests=[dict(number=4, base=dict(ref='kirkstone', sha=BASE), head=dict(sha=HEAD))])
        inventory = dict(total_count=1, workflow_runs=[run])
        attempt = dict(total_count=3, jobs=jobs)
        self.route.read.side_effect = lambda path: attempt if '/attempts/2/jobs?' in path else inventory
        self.assertEqual(self.route.series_run(self.pr), {'id': 8, 'attempt': 2})
        jobs[0]['conclusion'] = 'skipped'
        self.assertIsNone(self.route.series_run(self.pr))
        jobs[0]['conclusion'] = 'success'
        inventory['workflow_runs'].append({**run, 'id': 9, 'status': 'queued'})
        self.assertIsNone(self.route.series_run(self.pr))
        inventory['workflow_runs'].pop()
        run['pull_requests'][0]['base']['sha'] = CORE
        self.assertIsNone(self.route.series_run(self.pr))

    def test_review_routes_exact_head_and_rechecks_before_publication(self):
        approved = {**self.pr, '_layer_publication': {'series_run': {'id': 8, 'attempt': 1}}}
        owner = dict(status='review', agent='Codex', publish_pending=False)
        self.route.candidate = Mock(return_value=approved)
        self.route.identity = Mock()
        self.route.protections = Mock()
        self.route.core_owner = Mock(return_value=owner)
        self.route.publish = Mock()
        self.coordinator.store.review.return_value = None
        self.assertFalse(self.route.advance('kirkstone', 4, owner, {}, True))
        self.coordinator.launch.assert_called_once_with(self.project, approved, owner, 'review')
        self.route.publish.assert_not_called()
        receipt = dict(head=HEAD, family='Claude', feedback=feedback(approved),
                       result=dict(verdict='approve', acceptance_complete=True))
        self.coordinator.store.review.return_value = receipt
        moved = {**approved, '_layer_publication': {'series_run': {'id': 8, 'attempt': 2}}}
        self.route.candidate.side_effect = [approved, moved]
        self.assertFalse(self.route.advance('kirkstone', 4, owner, {}, True))
        self.route.publish.assert_not_called()
        self.route.candidate.side_effect = [approved, approved]
        self.assertTrue(self.route.advance('kirkstone', 4, owner, {}, True))
        self.route.publish.assert_called_once_with(approved)

    def test_publish_uses_exact_ref_lease_and_rejects_moved_source(self):
        self.pr['_layer_publication'] = {'series_run': {'id': 8, 'attempt': 2}}
        with tempfile.TemporaryDirectory() as tmp:
            credential = Path(tmp) / 'token'
            credential.write_text('ghs_synthetic')
            credential.chmod(0o600)
            self.route.route['token_file'] = str(credential)
            with patch('coordinator.run', return_value=f'{HEAD}\trefs/pull/4/head') as git:
                self.route.publish(self.pr)
                self.assertIn(f'--force-with-lease=refs/heads/kirkstone:{BASE}', git.call_args.args[0])
                self.assertEqual(git.call_args.args[0][-1], f'{HEAD}:refs/heads/kirkstone')
                self.assertNotIn('ghs_synthetic', str(git.call_args.args))
            with patch('coordinator.run', return_value=f'{CORE}\trefs/pull/4/head') as git:
                with self.assertRaisesRegex(RuntimeError, 'head moved'):
                    self.route.publish(self.pr)
                git.assert_called_once()

    def test_ref_lease_rejects_a_real_competing_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'
            remote = Path(tmp) / 'remote.git'

            def git(*args, cwd=tmp):
                return subprocess.check_output(['git', *args], cwd=cwd, text=True,
                                               stderr=subprocess.PIPE).strip()

            git('init', '--bare', str(remote))
            git('init', str(root))
            git('config', 'user.name', IDENTITY['name'], cwd=root)
            git('config', 'user.email', IDENTITY['email'], cwd=root)
            git('remote', 'add', 'origin', str(remote), cwd=root)
            git('commit', '--allow-empty', '-m', 'Initial', cwd=root)
            base = git('rev-parse', 'HEAD', cwd=root)
            git('push', 'origin', f'{base}:refs/heads/kirkstone', cwd=root)
            git('commit', '--allow-empty', '-m', 'Candidate', cwd=root)
            head = git('rev-parse', 'HEAD', cwd=root)
            git('push', 'origin', f'{head}:refs/pull/4/head', cwd=root)
            git('checkout', '--detach', base, cwd=root)
            git('commit', '--allow-empty', '-m', 'Competing update', cwd=root)
            competitor = git('rev-parse', 'HEAD', cwd=root)
            git('push', 'origin', f'{competitor}:refs/heads/kirkstone', cwd=root)
            credential = Path(tmp) / 'token'
            credential.write_text('ghs_synthetic')
            credential.chmod(0o600)
            self.project['root'] = str(root)
            self.route.route['token_file'] = str(credential)
            candidate = {**self.pr, 'headRefOid': head, 'baseRefOid': base,
                         '_layer_publication': {'series_run': {'id': 8, 'attempt': 1}}}
            with self.assertRaises(subprocess.CalledProcessError):
                self.route.publish(candidate)
            self.assertEqual(git('rev-parse', 'refs/heads/kirkstone', cwd=remote), competitor)

    def test_export_entries_from_real_committed_core_match_workflow(self):
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        self.route.route['core_revision'] = revision
        entries = self.route.export_entries()
        workflow = subprocess.check_output([
            'git', 'rev-parse', revision + ':suifleet-edge/meta-suifleet/' + LAYER_WORKFLOW,
        ], cwd=ROOT, text=True).strip()
        self.assertEqual(entries[LAYER_WORKFLOW], ('100644', 'blob', workflow))
        self.assertIn('export-provenance.json', entries)
        self.assertEqual(entries['ci/build.sh'][:2], ('100755', 'blob'))


if __name__ == '__main__':
    unittest.main()
