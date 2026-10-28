"""Offline encrypted-artifact transport tests; no GitHub or chain mutation."""
import sys
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_fixture_cache import KEEP, MAX_PAGES, NAME, prune, restore


class FixtureArtifacts(unittest.TestCase):
    def artifact(self, run=7, expired=False, created='2026-10-15T07:28:18Z', artifact_id=11):
        return {'id':artifact_id,'name':NAME,'expired':expired,'workflow_run':{'id':run},'created_at':created}

    def producer(self, **changes):
        return {'id':7,'head_branch':'main','path':'.github/workflows/testnet.yml',
                'event':'workflow_run','repository':{'full_name':'a/b'},**changes}

    def test_verified_ciphertext_restore_preserves_private_modes(self):
        def get(path):
            if '/actions/artifacts?' in path:
                return {'total_count':1,'artifacts':[self.artifact()]}
            self.assertEqual(path,'repos/a/b/actions/runs/7')
            return self.producer()
        def download(run, name, destination):
            self.assertEqual((run,name),(7,NAME))
            (destination/'fixture.bin').write_bytes(b'opaque-authenticated-ciphertext')
        with tempfile.TemporaryDirectory() as tmp:
            destination=Path(tmp)/'state'
            result=restore('a/b',destination,get,download)
            self.assertTrue(result['restored'])
            self.assertEqual(result['artifact'],11)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((destination/'fixture.bin').stat().st_mode),0o600)

    def test_fork_other_workflow_and_foreign_repository_are_never_downloaded(self):
        for change in ({'head_branch':'fork'}, {'path':'.github/workflows/ci.yml'},
                       {'event':'pull_request'}, {'repository':{'full_name':'fork/b'}}):
            def get(path):
                return {'total_count':1,'artifacts':[self.artifact()]} if '/artifacts?' in path else self.producer(**change)
            download=Mock()
            with tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(restore('a/b',Path(tmp)/'state',get,download)['restored'])
            download.assert_not_called()

    def test_missing_or_expired_artifact_is_a_cold_fixture_not_fake_reuse(self):
        for artifacts in ([],[self.artifact(expired=True)]):
            get=Mock(return_value={'total_count':len(artifacts),'artifacts':artifacts})
            download=Mock()
            with tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(restore('a/b',Path(tmp)/'state',get,download)['restored'])
            download.assert_not_called()
            self.assertEqual(get.call_count,1)

    def test_bad_download_shape_or_symlink_fails_before_decryption(self):
        for kind in ('extra','symlink'):
            def get(path):
                return {'total_count':1,'artifacts':[self.artifact()]} if '/artifacts?' in path else self.producer()
            def download(run,name,destination):
                if kind=='symlink': (destination/'fixture.bin').symlink_to('/dev/null')
                else:
                    (destination/'fixture.bin').write_bytes(b'ciphertext')
                    (destination/'unexpected.json').write_text('{}')
            with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RuntimeError):
                restore('a/b',Path(tmp)/'state',get,download)

    def test_api_failure_is_not_silently_replaced_by_publication(self):
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RuntimeError):
            restore('a/b',Path(tmp)/'state',Mock(side_effect=RuntimeError('API unavailable')),Mock())

    def test_newer_creation_wins_over_response_and_id_order_across_pages(self):
        # Observed in production: an older run's retry uploaded newer state,
        # with a lower artifact ID and a later position in the API response.
        older=self.artifact(run=7,artifact_id=11955229354)
        newer=self.artifact(run=8,artifact_id=11954559410,created='2026-10-15T07:32:10Z')
        for paginated in (False,True):
            def get(path):
                if '/artifacts?' in path:
                    if not paginated: return {'total_count':2,'artifacts':[older,newer]}
                    fillers=[self.artifact(run=9,expired=True,artifact_id=100+i) for i in range(99)]
                    return {'total_count':101,'artifacts':[newer] if 'page=2' in path else [older,*fillers]}
                self.assertEqual(path,'repos/a/b/actions/runs/8')
                return self.producer(id=8)
            def download(run,name,destination):
                self.assertEqual(run,8)
                (destination/'fixture.bin').write_bytes(b'newer-authenticated-state')
            with tempfile.TemporaryDirectory() as tmp:
                result=restore('a/b',Path(tmp)/'state',get,download)
                self.assertEqual(result['artifact'],11954559410)

    def test_newest_foreign_producer_is_skipped_for_latest_trusted_state(self):
        def get(path):
            if '/artifacts?' in path:
                return {'total_count':2,'artifacts':[self.artifact(),self.artifact(run=8,created='2026-10-15T07:32:10Z',artifact_id=12)]}
            return self.producer(head_branch='foreign') if path.endswith('/8') else self.producer()
        def download(run,name,destination):
            self.assertEqual(run,7)
            (destination/'fixture.bin').write_bytes(b'trusted-ciphertext')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(restore('a/b',Path(tmp)/'state',get,download)['run'],7)

    def test_missing_malformed_or_non_utc_creation_time_fails_closed(self):
        for value in (None,17,'yesterday','2026-10-15T07:32:10+01:00'):
            artifact=self.artifact();artifact['created_at']=value
            get=Mock(return_value={'total_count':1,'artifacts':[artifact]});download=Mock()
            with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RuntimeError):
                restore('a/b',Path(tmp)/'state',get,download)
            download.assert_not_called()

    def test_existing_destination_or_excessive_unknown_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination=Path(tmp)/'state';destination.mkdir();(destination/'fixture.bin').write_bytes(b'existing')
            with self.assertRaises(RuntimeError): restore('a/b',destination,Mock(),Mock())
        for total in (1000,MAX_PAGES*100+1):
            with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RuntimeError):
                restore('a/b',Path(tmp)/'state',Mock(return_value={'total_count':total,'artifacts':[]}),Mock())

    def test_workflow_never_exports_a_readonly_runtime_cache(self):
        workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        self.assertIn('cache-from: type=gha',workflow)
        self.assertNotIn('cache-to:',workflow)
        self.assertNotIn('actions/cache/save',workflow)
        self.assertIn('name: ${{ needs.prepare.outputs.fixture }}',workflow)
        self.assertIn('path: encrypted-live-state/fixture.bin',workflow)
        self.assertNotIn('path: live-state',workflow)
        self.assertIn('cp scripts/testnet_fixture_cache.py "$RUNNER_TEMP/testnet_fixture_cache.py"',workflow)


def stamp(minute):
    return f'2026-10-{1+minute//1440:02d}T{minute//60%24:02d}:{minute%60:02d}:00Z'


class History:
    """Paginated GitHub artifact API fake with per-run producer metadata."""
    def __init__(self, artifacts, runs=None, fail=None):
        self.artifacts, self.runs, self.fail = list(artifacts), runs or {}, fail or (lambda path: None)
        self.paths, self.deleted = [], []

    def get(self, path):
        self.paths.append(path)
        self.fail(path)
        if '/actions/artifacts?' in path:
            page = int(path.rsplit('page=', 1)[1])
            return {'total_count':len(self.artifacts),'artifacts':self.artifacts[(page-1)*100:page*100]}
        run_id = int(path.rsplit('/', 1)[1])
        return self.runs.get(run_id) or {'id':run_id,'head_branch':'main','path':'.github/workflows/testnet.yml',
                                         'event':'schedule','repository':{'full_name':'a/b'}}

    def delete(self, artifact_id):
        self.deleted.append(artifact_id)
        self.artifacts = [a for a in self.artifacts if a['id'] != artifact_id]


def item(index, run=None, created=None, **changes):
    return {'id':1000+index,'name':NAME,'expired':False,'workflow_run':{'id':run or 5000+index},
            'created_at':created or stamp(index),**changes}


class LargeHistory(unittest.TestCase):
    def download(self, expected):
        def download(run, name, destination):
            self.assertEqual((run, name), (expected, NAME))
            (destination/'fixture.bin').write_bytes(b'ciphertext')
        return download

    def test_more_than_500_artifacts_restore_the_newest_trusted_snapshot(self):
        for count in (501, 650, 1234):
            # API newest-first, but a rerun's newer upload has an old run and low ID on the last page.
            artifacts = [item(i) for i in reversed(range(1, count))]
            artifacts.append(item(0, run=4242, created=stamp(count + 5)))
            history = History(artifacts)
            with tempfile.TemporaryDirectory() as tmp:
                result = restore('a/b', Path(tmp)/'state', history.get, self.download(4242))
            self.assertEqual((result['run'], result['artifact']), (4242, 1000))
            self.assertEqual(sum('/artifacts?' in p for p in history.paths), -(-count // 100))

    def test_unrelated_expired_and_untrusted_newer_artifacts_are_skipped(self):
        artifacts = [item(i) for i in range(600)]
        artifacts += [item(700, name='testnet-effects-x'), item(701, expired=True), item(702),
                      item(703, expired=None)]
        history = History(artifacts, {5702: {'id':5702,'head_branch':'feature','path':'.github/workflows/testnet.yml',
                                             'event':'workflow_run','repository':{'full_name':'a/b'}}})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(restore('a/b', Path(tmp)/'state', history.get, self.download(5599))['artifact'], 1599)

    def test_malformed_ambiguous_or_failed_discovery_never_falls_back_to_publication(self):
        base = [item(i) for i in range(250)]
        def shifting():
            history = History(base)
            original = history.get
            def get(path):
                listing = original(path)
                if 'page=2' in path: listing['total_count'] += 1
                return listing
            history.get = get
            return history
        def duplicated():
            history = History(base)
            history.artifacts = base[:150] + base[50:150]
            return history
        def short():
            history = History(base)
            original = history.get
            history.get = lambda path: {**original(path), 'artifacts': original(path)['artifacts'][:99]}
            return history
        def malformed(response):
            return lambda: type('M', (), {'get': staticmethod(lambda path: response)})()
        def failing(fragment):
            def boom(path):
                if fragment in path: raise RuntimeError('API unavailable')
            return lambda: History(base, fail=boom)
        cases = [shifting, duplicated, short, failing('page=3'), failing('/actions/runs/'),
                 malformed([]), malformed({'total_count':'1','artifacts':[]}),
                 malformed({'total_count':1,'artifacts':{}}), malformed({'total_count':-1,'artifacts':[]}),
                 malformed({'total_count':1,'artifacts':['x']}), malformed({'total_count':1,'artifacts':[{'id':True}]}),
                 malformed({'total_count':1,'artifacts':[item(1, workflow_run=None)]}),
                 malformed({'total_count':1,'artifacts':[item(1, workflow_run={'id':'7'})]})]
        for case in cases:
            download = Mock()
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
                restore('a/b', Path(tmp)/'state', case().get, download)
            download.assert_not_called()

    def test_transient_pagination_shift_is_rescanned_once(self):
        history = History([item(i) for i in range(150)])
        original, calls = history.get, []
        def get(path):
            listing = original(path)
            if 'page=2' in path and not calls:
                calls.append(path)
                listing['total_count'] += 1
            return listing
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(restore('a/b', Path(tmp)/'state', get, self.download(5149))['artifact'], 1149)

    def test_download_failure_is_not_a_cold_fixture(self):
        history = History([item(i) for i in range(501)])
        def download(run, name, destination):
            raise RuntimeError('encrypted fixture artifact download failed')
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            restore('a/b', Path(tmp)/'state', history.get, download)


class Retention(unittest.TestCase):
    def test_prunes_only_trusted_fixture_snapshots_older_than_retained_history(self):
        untrusted = {5003: {'id':5003,'head_branch':'feature','path':'.github/workflows/testnet.yml',
                            'event':'workflow_run','repository':{'full_name':'a/b'}},
                     5004: {'id':5004,'head_branch':'main','path':'.github/workflows/ci.yml',
                            'event':'workflow_run','repository':{'full_name':'a/b'}},
                     5005: {'id':5005,'head_branch':'main','path':'.github/workflows/testnet.yml',
                            'event':'pull_request','repository':{'full_name':'a/b'}},
                     5006: {'id':5006,'head_branch':'main','path':'.github/workflows/testnet.yml',
                            'event':'schedule','repository':{'full_name':'fork/b'}},
                     # Untrusted snapshots inside the newest range must not count as retained.
                     5995: {'id':5995,'head_branch':'feature','path':'.github/workflows/testnet.yml',
                            'event':'workflow_run','repository':{'full_name':'a/b'}}}
        artifacts = [item(i) for i in reversed(range(1000))]
        artifacts += [item(2000, name='other'), item(2001, expired=True, created=stamp(1)),
                      item(2002, run=777, created=stamp(2))]  # the current run is never deleted
        history = History(artifacts, untrusted)
        deleted = set()
        for _ in range(40):
            result = prune('a/b', 777, history.get, history.delete)
            deleted.update(result['deleted'])
            if not result['deleted']: break
        remaining = {a['id'] for a in history.artifacts}
        kept = {1000+i for i in range(989, 1000) if i != 995}
        self.assertEqual(set(result['kept']), kept)
        self.assertEqual(len(kept), KEEP)
        self.assertTrue(kept | {1995, 1003, 1004, 1005, 1006, 3000, 3001, 3002} <= remaining)
        self.assertEqual(remaining - kept, {1995, 1003, 1004, 1005, 1006, 3000, 3001, 3002})
        self.assertFalse(deleted & (kept | {1995, 1003, 1004, 1005, 1006, 3000, 3001, 3002}))
        with tempfile.TemporaryDirectory() as tmp:
            download = lambda run, name, destination: (destination/'fixture.bin').write_bytes(b'ciphertext')
            self.assertEqual(restore('a/b', Path(tmp)/'state', history.get, download)['artifact'], 1999)

    def test_each_run_bounds_deletions_and_deletes_oldest_first(self):
        history = History([item(i) for i in range(200)])
        result = prune('a/b', 1, history.get, history.delete, max_deletes=7)
        self.assertEqual(result['deleted'], [1000+i for i in range(7)])

    def test_fewer_than_retained_trusted_snapshots_deletes_nothing(self):
        history = History([item(i) for i in range(KEEP)] + [item(50, expired=True)])
        self.assertEqual(prune('a/b', 1, history.get, history.delete)['deleted'], [])
        self.assertEqual(history.deleted, [])

    def test_lookup_bound_before_retained_set_is_verified_deletes_nothing(self):
        history = History([item(i) for i in range(30)],
                          {5000+i: {'id':5000+i} for i in range(20, 30)})
        self.assertEqual(prune('a/b', 1, history.get, history.delete, max_lookups=5)['deleted'], [])
        self.assertEqual(history.deleted, [])

    def test_ambiguous_history_or_delete_failure_stops_retention(self):
        history = History([item(i) for i in range(150)])
        original = history.get
        def shifting(path):
            listing = original(path)
            if 'page=2' in path: listing['total_count'] += 1
            return listing
        with self.assertRaises(RuntimeError): prune('a/b', 1, shifting, history.delete)
        self.assertEqual(history.deleted, [])
        def failing(artifact_id):
            raise RuntimeError('encrypted fixture artifact deletion failed')
        failing = Mock(side_effect=failing)
        with self.assertRaises(RuntimeError): prune('a/b', 1, history.get, failing)
        failing.assert_called_once_with(1000)
        with self.assertRaises(ValueError): prune('a/b', 1, history.get, history.delete, keep=0)

    def test_retention_job_is_isolated_main_sourced_and_serialized(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        header, jobs = workflow.split('\njobs:\n', 1)
        validate, pruning = jobs.split('\n  prune-fixture-history:\n', 1)
        self.assertNotIn('actions: write', header)
        self.assertNotIn('actions: write', validate)
        self.assertNotIn('concurrency:', header)
        self.assertNotIn('\n    concurrency:', validate.split('  validate:',1)[0])
        self.assertIn('group: ${{ needs.prepare.outputs.concurrency }}', validate)
        self.assertIn('group: ${{ needs.prepare.outputs.concurrency }}', pruning)
        self.assertIn('      run: ${{ steps.plan.outputs.run }}', validate)
        self.assertIn("needs.validate.result == 'success' && needs.validate.outputs.run == 'true'", pruning)
        self.assertIn("github.ref == 'refs/heads/main'", pruning)
        self.assertIn('    permissions:\n      contents: read\n      actions: write\n    steps:', pruning)
        self.assertIn('ref: ${{ github.workflow_sha }}', pruning)
        self.assertIn('persist-credentials: false', pruning)
        self.assertIn('run: python3 scripts/testnet_fixture_cache.py --prune', pruning)
        for forbidden in ('secrets.', 'environment:', 'head_sha', 'github.sha', 'docker', 'upload-artifact', 'statuses'):
            self.assertNotIn(forbidden, pruning)
        self.assertGreaterEqual(KEEP, 5)
