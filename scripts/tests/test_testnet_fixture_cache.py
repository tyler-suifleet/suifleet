"""Offline encrypted-artifact transport tests; no GitHub or chain mutation."""
import sys
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_fixture_cache import NAME, restore


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
                    return {'total_count':101,'artifacts':[newer] if 'page=2' in path else [older]}
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
                return {'total_count':2,'artifacts':[self.artifact(),self.artifact(run=8,created='2026-10-15T07:32:10Z')]}
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
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RuntimeError):
            restore('a/b',Path(tmp)/'state',Mock(return_value={'total_count':1000,'artifacts':[]}),Mock())

    def test_workflow_never_exports_a_readonly_runtime_cache(self):
        workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        self.assertIn('cache-from: type=gha',workflow)
        self.assertNotIn('cache-to:',workflow)
        self.assertNotIn('actions/cache/save',workflow)
        self.assertIn('name: encrypted-testnet-fixture',workflow)
        self.assertIn('path: encrypted-live-state/fixture.bin',workflow)
        self.assertNotIn('path: live-state',workflow)
        self.assertIn('cp scripts/testnet_fixture_cache.py "$RUNNER_TEMP/testnet_fixture_cache.py"',workflow)
