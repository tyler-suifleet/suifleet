"""Offline safety regressions: never make remote mutations."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent
from testnet_ci import trusted_pr, plan


class TrustTests(unittest.TestCase):
    def test_live_gate_checkout_and_token_permissions(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/live-gates.yml').read_text()
        preflight, gate = workflow.split('\n  gate:\n', 1)
        self.assertNotIn('inputs.ref', workflow)
        self.assertNotIn('inputs.base_ref', workflow)
        self.assertIn('persist-credentials: false', gate)
        self.assertNotIn('          ref:', gate)
        self.assertNotIn('actions: write', gate)
        self.assertNotIn('actions: write', preflight.split('jobs:', 1)[0])
        self.assertIn('      actions: write', preflight)

    def test_dependency_build_step_has_no_signing_key(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        build = workflow.split('      - name: Build testnet dependencies without signing credentials', 1)[1].split('      - name:', 1)[0]
        run = workflow.split('      - name: Run bounded testnet acceptance', 1)[1].split('      - uses:', 1)[0]
        self.assertIn('pnpm --dir dapp install --frozen-lockfile', build)
        self.assertIn('cargo build --locked', build)
        self.assertNotIn('SUIFLEET_TESTNET_KEY', build)
        self.assertIn('-e SUIFLEET_TESTNET_KEY', run)
        self.assertIn('node scripts/ci_testnet.cjs', run)
        self.assertNotIn('pnpm ', run)
        self.assertNotIn('cargo build', run)

    def fixture(self):
        return {'number': 1, 'state': 'open', 'user': {'login': 'writer'},
                'base': {'ref': 'main', 'repo': {'full_name': 'a/b'}},
                'head': {'sha': 'a'*40, 'repo': {'full_name': 'a/b'}}}

    def test_fork_stale_sha_and_read_author_rejected(self):
        pr = self.fixture()
        self.assertTrue(trusted_pr(pr, 'a/b', 'a'*40, {'writer': 'write'}))
        self.assertFalse(trusted_pr(pr, 'a/b', 'b'*40, {'writer': 'write'}))
        self.assertFalse(trusted_pr(pr, 'a/b', 'a'*40, {'writer': 'read'}))
        pr['head']['repo']['full_name'] = 'fork/b'
        self.assertFalse(trusted_pr(pr, 'a/b', 'a'*40, {'writer': 'admin'}))

    def test_failed_or_unrelated_workflow_cannot_unlock_secrets(self):
        for conclusion, path in [('failure', '.github/workflows/ci.yml'), ('success', 'evil.yml')]:
            def get(url):
                if '/commits/' in url: return {'sha': 'a'*40}
                if '/runs/' in url:
                    return dict(conclusion=conclusion, status='completed', workflow_id=7,
                                repository={'full_name': 'a/b'})
                return {'path': path}
            self.assertIsNone(plan('workflow_run', {'workflow_run': {'id': 1}}, 'a/b', get))

    def test_cache_reuse_digest_ignores_build(self):
        import live_package_upgrade as live
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'source.move').write_text('same contracts')
            before = live.contracts_digest(root)
            (root/'build').mkdir()
            (root/'build'/'junk').write_text('changed build output')
            self.assertEqual(before, live.contracts_digest(root))

    def test_exact_sha_actor_and_main_binding(self):
        pr = self.fixture()
        run = dict(status='completed', conclusion='success', workflow_id=7,
                   repository={'full_name': 'a/b'}, event='pull_request',
                   head_sha='a'*40, actor={'login': 'writer'})
        def get(url):
            if url.endswith('/commits/main'): return {'sha': 'b'*40}
            if '/runs/' in url: return run
            if '/workflows/' in url: return {'path': '.github/workflows/ci.yml'}
            if '/permission' in url: return {'permission': 'write'}
            if url.endswith('/pulls'): return [{'number': 1}]
            if url.endswith('/pulls/1'): return pr
            raise AssertionError(url)
        self.assertEqual(plan('workflow_run', {'workflow_run': {'id': 1}}, 'a/b', get),
                         {'head': 'a'*40, 'base': 'b'*40, 'pr': '1'})
        pr['head']['sha'] = 'c'*40
        self.assertIsNone(plan('workflow_run', {'workflow_run': {'id': 1}}, 'a/b', get))

    def test_environment_has_no_testnet_approval_but_is_main_only(self):
        from testnet_ci import environment_ready
        env = dict(can_admins_bypass=False, protection_rules=[],
                   deployment_branch_policy=dict(custom_branch_policies=True, protected_branches=False))
        branches = {'total_count': 1, 'branch_policies': [{'id': 1, 'name': 'main', 'type': 'branch'}]}
        self.assertTrue(environment_ready(env, branches))
        self.assertFalse(environment_ready(env, {'branch_policies': [{'name': '*'}]}))
        env['protection_rules'] = [{'type': 'required_reviewers'}]
        self.assertFalse(environment_ready(env, branches))

    def test_ci_keystore_only_exists_in_anonymous_memory(self):
        import os
        import stat
        import tempfile
        from ci_testnet_wallet import execute
        with tempfile.TemporaryDirectory() as root:
            def child(command, check):
                config = (Path(root)/'client.yaml').read_text()
                fd_path = next(line.split('File: ')[1] for line in config.splitlines() if 'File:' in line)
                self.assertEqual(Path(fd_path).read_bytes(), b'offline-test-keystore')
                self.assertIn('memfd:', os.readlink(os.readlink(fd_path)))
                self.assertEqual(stat.S_IMODE(Path(fd_path).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((Path(root)/'client.yaml').stat().st_mode), 0o600)
                self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['client.yaml', 'sui.keystore'])
                return type('Result', (), {'returncode': 0})()
            self.assertEqual(execute(root, '/state', 'a'*40, b'offline-test-keystore', child), 0)

    def test_unchanged_custodied_fixture_reuses_state_without_mutation(self):
        import live_package_upgrade as live
        import stat
        import tempfile
        fixture = dict(upgrade_cap='0x1', device='0x2', owner_cap='0x3', device_cap='0x4',
                       group='0x5', registry='0x6', head_package='0x7', device_address='0xb')
        owners = {'0x1': {'AddressOwner':'0xa'}, '0x2': {'ObjectOwner':'0xc'},
                  '0x3': {'AddressOwner':'0xa'}, '0x4': {'AddressOwner':'0xb'},
                  '0x5': {'Shared':{}}, '0x6': {'Shared':{}}, '0x7': {'Immutable':True}}
        def client(command, object_id):
            self.assertEqual(command, 'object')
            return {'owner': owners[object_id]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            state = dict(chain='4c78adac', operator='0xa', fixtures={'content-hash':fixture})
            live.save_state(path, state)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            reused = live.load_state(path, '4c78adac', '0xa')['fixtures']['content-hash']
            self.assertTrue(live.fixture_alive(reused, '0xa', client))
            owners['0x3'] = {'AddressOwner':'0xd'}
            self.assertFalse(live.fixture_alive(reused, '0xa', client))
            self.assertEqual(live.load_state(path, '4c78adac', '0xd')['fixtures'], {})

class PublicEnvironmentTests(unittest.TestCase):
    def environment(self):
        return dict(can_admins_bypass=False, protection_rules=[],
                    deployment_branch_policy=dict(custom_branch_policies=True, protected_branches=False))

    def branches(self):
        return {'total_count': 1, 'branch_policies': [{'id': 7, 'name': 'main', 'type': 'branch'}]}

    def test_policy_read_does_not_use_the_restricted_authenticated_endpoint(self):
        from testnet_ci import verify_environment
        def workflow_token(path):
            if path.endswith('/deployment-branch-policies'):
                raise AssertionError('the observed Actions-token path is forbidden')
            self.assertEqual(path, 'repos/a/b/environments/live-package-upgrade')
            return self.environment()
        public = Mock(return_value=self.branches())
        proof = verify_environment('a/b', workflow_token, public)
        public.assert_called_once_with('repos/a/b/environments/live-package-upgrade/deployment-branch-policies')
        self.assertEqual(proof['branch'], 'main')
        self.assertFalse(proof['required_reviewers'])

    def test_public_request_never_forwards_workflow_credentials(self):
        import io
        from unittest.mock import patch
        from testnet_ci import public_api
        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            self.assertEqual(request.full_url, 'https://api.github.com/repos/a/b/environments/live-package-upgrade/deployment-branch-policies?per_page=100')
            self.assertFalse(request.has_header('Authorization'))
            self.assertNotIn('offline-token', str(request.header_items()))
            return io.BytesIO(json.dumps(self.branches()).encode())
        with patch.dict('os.environ', {'GH_TOKEN': 'offline-token', 'GITHUB_TOKEN': 'offline-token'}):
            self.assertEqual(public_api('repos/a/b/environments/live-package-upgrade/deployment-branch-policies', opener), self.branches())
        with self.assertRaises(ValueError):
            public_api('repos/a/b/environments/release/deployment-branch-policies', opener)

    def test_public_denial_or_unavailable_metadata_never_passes(self):
        from urllib.error import HTTPError, URLError
        from testnet_ci import public_api, verify_environment
        path = 'repos/a/b/environments/live-package-upgrade/deployment-branch-policies'
        for error in (HTTPError('https://api.github.com/', 403, 'denied', {}, None), URLError('offline')):
            with self.subTest(error=type(error).__name__), self.assertRaises(RuntimeError):
                public_api(path, Mock(side_effect=error))
        for branches in ({'total_count':2,'branch_policies':self.branches()['branch_policies']},
                         {'total_count':1,'branch_policies':[{'id':7,'name':'*','type':'branch'}]},
                         {'total_count':1,'branch_policies':[{'id':7,'name':'main','type':'tag'}]}):
            with self.subTest(branches=branches), self.assertRaises(RuntimeError):
                verify_environment('a/b', Mock(return_value=self.environment()), Mock(return_value=branches))

    def test_authenticated_failures_have_sanitized_useful_errors(self):
        from unittest.mock import patch
        from testnet_ci import api
        result = type('Result', (), {'returncode':1,'stdout':json.dumps({'message':'denied offline-token'})})()
        with patch.dict('os.environ', {'GH_TOKEN':'offline-token'}), patch('testnet_ci.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, r'denied \[redacted\]') as error:
                api('repos/a/b/environments/live-package-upgrade')
            self.assertNotIn('offline-token', str(error.exception))

    def test_real_ci_metadata_probe_has_no_wallet_or_environment(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/ci.yml').read_text()
        probe = workflow.split('\n  testnet-preflight:\n', 1)[1]
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', probe)
        self.assertIn('actions: read', probe)
        self.assertIn('--probe-environment', probe)
        self.assertNotIn('secrets.', probe)
        self.assertNotIn('environment:', probe)
        self.assertNotIn('SUIFLEET_TESTNET_KEY', probe)
