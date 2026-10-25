"""Offline safety regressions: never make remote mutations."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent
from testnet_ci import trusted_pr, plan


class TrustTests(unittest.TestCase):
    def test_receipt_timeout_diagnostics_exclude_private_journal_and_log_values(self):
        import tempfile
        from live_package_upgrade import daemon_progress
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); journal = root/'journal'; log = root/'log'
            journal.write_text(json.dumps(dict(initialized=True, device_key='PRIVATE-CANARY',
                cursor={'after':'PRIVATE-CANARY'}, deployments={'0x1':dict(status=1,
                    reported=False, error='PRIVATE-CANARY', secret='PRIVATE-CANARY')})))
            log.write_text('Sync failed PRIVATE-CANARY RPC request timeout\n')
            diagnostic = daemon_progress(journal, log)
            self.assertTrue(diagnostic['initialized'])
            self.assertEqual(list(diagnostic['outcomes'].values()),
                             [dict(status=1, reported=False, has_error=True)])
            self.assertEqual(diagnostic['log_counts']['RPC request timeout'], 1)
            self.assertNotIn('PRIVATE-CANARY', json.dumps(diagnostic))
            journal.write_text('malformed'); log.unlink()
            self.assertEqual(daemon_progress(journal, log)['outcomes'], {})

    def test_large_upgrade_uses_remaining_allowance_without_replenishment(self):
        evidence = []
        budget = Budget(800_000_000, evidence)
        budget.grant(351_442_556)  # Setup in failed run 40140740252.
        def upgrade(reserved):
            self.assertEqual(reserved, 448_557_444)
            return dict(digest='upgrade', effects=dict(status=dict(status='success'),
                gasUsed=dict(computationCost='10000000', storageCost='400000000', storageRebate='0')))
        budget.execute(Mock(), 'upgrade to head', upgrade, gas=budget.remaining)
        self.assertEqual(budget.used, 761_442_556)
        self.assertEqual(budget.remaining, 38_557_444)
        # A later unknown operation keeps its entire remaining reservation.
        with self.assertRaises(TimeoutError):
            budget.execute(Mock(), 'unknown', Mock(side_effect=TimeoutError()), gas=budget.remaining)
        self.assertEqual(budget.used, 800_000_000)
        with self.assertRaises(Spent):
            budget.execute(Mock(), 'retry', upgrade, gas=400_000_000)

    def test_lower_ptb_budget_does_not_raise_existing_transaction_cap(self):
        from live_package_upgrade import transaction_gas_budget
        self.assertEqual(transaction_gas_budget(40_000_000), 40_000_000)
        self.assertEqual(transaction_gas_budget(200_000_000), 200_000_000)
        for value in [0, 1_999_999, 200_000_001, True, "40000000"]:
            with self.assertRaises(ValueError):
                transaction_gas_budget(value)

    def test_checkout_must_match_immutable_authenticated_commit(self):
        from testnet_ci import verify_checkout
        run = Mock(return_value=type('Result', (), {'stdout': 'a'*40 + '\n'})())
        verify_checkout('a'*40, run)
        run.assert_called_once_with(['git', 'rev-parse', '--verify', 'HEAD'],
                                    text=True, capture_output=True, check=True)
        with self.assertRaises(RuntimeError):
            verify_checkout('b'*40, run)
        for ref in ['main', 'refs/pull/1/head', '', 'a'*39, 'a'*40 + '\n']:
            with self.assertRaises(RuntimeError):
                verify_checkout(ref, run)

    def test_workflow_checks_immutable_event_commit_before_revision_execution(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        self.assertIn('ref: ${{ github.workflow_sha }}', workflow)
        checkout = workflow.split('      - name: Checkout authenticated test revision', 1)[1]
        self.assertIn('github.event.workflow_run.head_sha || github.sha', checkout)
        self.assertNotIn('ref: ${{ steps.plan.outputs.head }}', checkout)
        verify = checkout.index('--verify-checkout')
        self.assertLess(verify, checkout.index('docker/setup-buildx-action'))
        self.assertLess(verify, checkout.index('SUIFLEET_TESTNET_KEY'))

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
            built={'digest':[1]*32}
            before = live.contracts_digest(root,compiled=built)
            (root/'build').mkdir()
            (root/'build'/'junk').write_text('changed build output')
            self.assertEqual(before, live.contracts_digest(root,compiled=built))
            (root/'fleet_data').mkdir();(root/'fleet_data/source.move').write_text('separate package changed')
            self.assertEqual(before,live.contracts_digest(root,compiled=built))
            self.assertNotEqual(before,live.contracts_digest(root,compiled={'digest':[2]*32}))

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
            self.assertEqual(execute(root, '/state', 'a'*40, b'offline-test-keystore', child, lambda base: False), 0)

    def suite_run(self, root, state, codes, relevant=True):
        from ci_testnet_wallet import execute
        calls = []
        def child(command, check):
            calls.append(command)
            if 'live_wallet_gate.py' in command[1]:  # A failed sweep leaves its key.
                (Path(root)/'live-browser-key.hex').write_text('cd'*32 + '\n')
            return type('Result', (), {'returncode': codes.get(Path(command[1]).name, 0)})()
        return execute(root, state, 'a'*40, b'offline-test-keystore', child, lambda base: relevant), calls

    def test_sf010_suites_follow_package_upgrade_with_their_own_ceilings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            state = str(Path(root)/'state.json')
            code, calls = self.suite_run(root, state, {})
            self.assertEqual(code, 0)
            self.assertEqual([Path(c[1]).name for c in calls],
                             ['live_package_upgrade.py', 'live_upgrade_security.py', 'live_wallet_gate.py'])
            for command, ceiling, evidence in ((calls[1], '850000000', 'evidence/upgrade-security.json'),
                                               (calls[2], '150000000', 'evidence/wallet.json')):
                self.assertEqual(command[command.index('--max-spend-mist') + 1], ceiling)
                self.assertEqual(command[command.index('--state') + 1], state)
                self.assertEqual(command[command.index('--evidence') + 1], evidence)
                self.assertNotIn('--faucet-tool', command)  # One faucet request per run, package gate only.

    def test_fresh_sf129_package_costs_leave_room_for_deployments(self):
        # Actual settled costs/funding from run 40184514226: base publish,
        # register, group, head upgrade, activation, device top-up (gas +
        # funding) and the three-deployment batch. Under the former 0.8-SUI
        # ceiling only 5,827,318 MIST remained for the base-package deployment,
        # which failed with InsufficientGas.
        from ci_testnet_wallet import PACKAGE_UPGRADE_CEILING
        observed = [349_109_200, 8_425_378, 1_243_578, 356_186_163, 8_737_163,
                    1_988_000 + 30_000_000, 38_483_200]
        self.assertEqual(800_000_000 - sum(observed), 5_827_318)
        ledger = Budget(PACKAGE_UPGRADE_CEILING, [])
        for cost in observed:
            ledger.grant(cost)
        # Room for the base deployment, cleanup and report-gas variance; the
        # ceiling stays strict and non-replenishing.
        self.assertGreaterEqual(ledger.remaining, 150_000_000)
        with self.assertRaises(Spent):
            ledger.grant(ledger.remaining + 1)

    def test_fresh_post_sf136_pair_fits_the_package_budget_and_ceilings(self):
        # Run 40238821132: publishing base b819cb2 (SF-136, 62,599 bytecode
        # bytes) with the former fixed 0.4-SUI budget failed InsufficientGas.
        # Run 40184514226 measured 349,109,200 MIST for 39,646 bytes, about
        # 8,806 MIST/byte; the SF-129 head is 63,783 bytes and the
        # upgrade-security base 6,608. Setup/deployment costs as measured.
        from ci_testnet_wallet import PACKAGE_UPGRADE_CEILING, SUITES
        from live_package_upgrade import PACKAGE_GAS
        rate = 349_109_200 // 39_646
        base, head = 62_599 * rate, 63_783 * rate
        self.assertLess(400_000_000, base)
        self.assertGreaterEqual(PACKAGE_GAS - base, 100_000_000)
        ledger = Budget(PACKAGE_UPGRADE_CEILING, [])
        for cost in (base, 8_425_378, 1_243_578, head, 8_737_163, 1_988_000 + 30_000_000, 38_483_200):
            ledger.grant(cost)
        self.assertGreaterEqual(ledger.remaining, 150_000_000)
        # Upgrade-security: small base, the same head upgrade, and its cached
        # run's measured 104,743,694 MIST of objects/reports (run 40231623943).
        security = Budget(dict((name, ceiling) for name, _, ceiling in SUITES)['upgrade-security'], [])
        for cost in (6_608 * rate, head, 104_743_694):
            security.grant(cost)
        self.assertGreaterEqual(security.remaining, 100_000_000)

    def test_sf010_suites_skip_when_irrelevant_or_after_package_failure(self):
        import tempfile
        for codes, relevant, expected in (({}, False, (0, 1)), ({'live_package_upgrade.py': 3}, True, (3, 1)),
                                          ({'live_upgrade_security.py': 4}, True, (4, 2))):
            with tempfile.TemporaryDirectory() as root:
                code, calls = self.suite_run(root, str(Path(root)/'state.json'), codes, relevant)
                self.assertEqual((code, len(calls)), expected)

    def test_sf010_unswept_keys_persist_in_the_encrypted_state(self):
        import os
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state.json'
            state.write_text(json.dumps({'chain': '4c78adac', 'fixtures': {'k': 1},
                                         'pending_keys': {'live-security-device.key': 'ab'*32, '../escape': 'ef'}}))
            root = Path(tmp)/'run'
            root.mkdir()
            restored = {}
            from ci_testnet_wallet import execute
            def child(command, check):
                if 'live_upgrade_security.py' in command[1]:
                    key = root/'live-security-device.key'
                    restored.update(seed=key.read_text().strip(), mode=stat.S_IMODE(key.stat().st_mode))
                    key.unlink()  # Recovered by this run.
                if 'live_wallet_gate.py' in command[1]:
                    (root/'live-browser-key.hex').write_text('cd'*32 + '\n')
                    return type('Result', (), {'returncode': 1})()
                return type('Result', (), {'returncode': 0})()
            self.assertEqual(execute(str(root), str(state), 'a'*40, b'k', child, lambda base: True), 1)
            self.assertEqual(restored, {'seed': 'ab'*32, 'mode': 0o600})
            self.assertFalse((Path(tmp)/'escape').exists())
            saved = json.loads(state.read_text())
            self.assertEqual(saved['pending_keys'], {'live-browser-key.hex': 'cd'*32})
            self.assertEqual(saved['fixtures'], {'k': 1})
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)

    def test_sf010_suite_relevance(self):
        import os
        from unittest import mock
        from ci_testnet_wallet import suites_relevant
        def git(names):
            return lambda *a, **k: type('Result', (), {'stdout': names})()
        with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_run'}):
            self.assertTrue(suites_relevant('b', git('docs/x.md\ncontracts/sources/a.move\n')))
            self.assertTrue(suites_relevant('b', git('scripts/live_common.py\n')))
            self.assertTrue(suites_relevant('b', git('scripts/sui_toolchain.py\n')))  # Their transaction CLI.
            self.assertFalse(suites_relevant('b', git('docs/x.md\nscripts/tracking.py\n')))
            self.assertFalse(suites_relevant('b', git('')))
        with mock.patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}):
            self.assertTrue(suites_relevant('b', git('')))

    def test_wallet_live_gate_uses_the_serialized_ci_wallet_controller(self):
        workflow = (Path(__file__).resolve().parents[2]/'.github/workflows/live-gates.yml').read_text()
        self.assertIn("inputs.gate != 'hardware' && 'suifleet-testnet-ci-wallet'", workflow)
        self.assertIn('[ "$GATE" = wallet ]', workflow)
        self.assertIn("if: inputs.gate == 'hardware'", workflow)
        testnet = (Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml').read_text()
        self.assertIn('-e GITHUB_EVENT_NAME', testnet)
        self.assertIn('path: evidence/*.json', testnet)

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
                public_api(path, Mock(side_effect=error), sleep=lambda seconds: None, log=lambda line: None)
        for branches in ({'total_count':2,'branch_policies':self.branches()['branch_policies']},
                         {'total_count':1,'branch_policies':[{'id':7,'name':'*','type':'branch'}]},
                         {'total_count':1,'branch_policies':[{'id':7,'name':'main','type':'tag'}]}):
            with self.subTest(branches=branches), self.assertRaises(RuntimeError):
                verify_environment('a/b', Mock(return_value=self.environment()), Mock(return_value=branches))

    def http_error(self, code, headers=None, body=b''):
        import io
        from email.message import Message
        from urllib.error import HTTPError
        message = Message()
        for name, value in (headers or {}).items():
            message[name] = value
        return HTTPError('https://api.github.com/', code, 'error', message, io.BytesIO(body))

    def retrying_read(self, outcomes, **options):
        """Drive public_api with a scripted opener and a fake clock; no real waits."""
        import io
        from testnet_ci import public_api
        now = [0.0]
        sleeps, logs, calls = [], [], []
        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertFalse(request.has_header('Authorization'))
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return io.BytesIO(json.dumps(outcome).encode())
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        options.setdefault('wall', lambda: 1_000_000 + now[0])
        result = public_api('repos/a/b/environments/live-package-upgrade/deployment-branch-policies',
                            opener, sleep=sleep, clock=lambda: now[0], log=logs.append, **options)
        return result, sleeps, logs, calls

    def test_confirmed_rate_limits_and_transient_failures_are_retried_freshly(self):
        from urllib.error import URLError
        from http.client import RemoteDisconnected
        cases = {
            'anonymous quota 403 body': self.http_error(403, {'X-RateLimit-Limit': '60'}, b'{"message":"API rate limit exceeded for 1.2.3.4."}'),
            'secondary 403 retry-after': self.http_error(403, {'Retry-After': '7'}),
            '429': self.http_error(429, {'Retry-After': '3'}),
            '503': self.http_error(503),
            'transport': URLError('connection reset'),
            'disconnect': RemoteDisconnected('closed'),
        }
        for name, error in cases.items():
            with self.subTest(name):
                result, sleeps, logs, calls = self.retrying_read([error, self.branches()])
                self.assertEqual(result, self.branches())
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(sleeps), 1)
                self.assertLessEqual(sleeps[0], 30)
                self.assertIn('retrying', logs[0])
        _, sleeps, _, _ = self.retrying_read([cases['secondary 403 retry-after'], self.branches()])
        self.assertEqual(sleeps, [7.0])
        quota = self.http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(1_000_000 + 40)})
        _, sleeps, logs, _ = self.retrying_read([quota, self.branches()])
        self.assertEqual(sleeps, [41.0])
        self.assertIn('"x-ratelimit-remaining": "0"', logs[0])

    def test_runner_quota_reset_from_failed_ci_fits_default_budget(self):
        # Run 40126963456 requested a 605-second wait, beyond the old 150s cap.
        quota = self.http_error(403, {'X-RateLimit-Remaining': '0',
                                     'X-RateLimit-Reset': '1000604'})
        result, sleeps, _, calls = self.retrying_read([quota, self.branches()])
        self.assertEqual(result, self.branches())
        self.assertEqual(sleeps, [605.0])
        self.assertEqual(len(calls), 2)

    def test_exhausted_retry_budget_fails_closed_with_diagnostics(self):
        limited = lambda: self.http_error(429, {'Retry-After': '50', 'X-RateLimit-Resource': 'core'})
        with self.assertRaisesRegex(RuntimeError, r'unavailable after 4 attempt\(s\).*budget 150s.*HTTP 429.*core') as error:
            self.retrying_read([limited() for _ in range(10)], budget=150)
        self.assertNotIn('Authorization', str(error.exception))
        # A quota that resets beyond the budget fails at once instead of sleeping past the job timeout.
        far = self.http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(1_000_000 + 3000)})
        outcomes = [far, self.branches()]
        with self.assertRaisesRegex(RuntimeError, r'after 1 attempt\(s\).*next wait 3001s'):
            self.retrying_read(outcomes)
        self.assertEqual(outcomes, [self.branches()])
        # The attempt cap applies even when every individual wait is short.
        outcomes = [self.http_error(502) for _ in range(10)]
        with self.assertRaisesRegex(RuntimeError, r'after 3 attempt\(s\)'):
            self.retrying_read(outcomes, attempts=3)
        self.assertEqual(len(outcomes), 7)

    def test_ordinary_denials_are_not_retried(self):
        for error in (self.http_error(403, {'X-RateLimit-Remaining': '42'}, b'{"message":"Resource not accessible by integration"}'),
                      self.http_error(404, {}, b'{"message":"Not Found"}'),
                      self.http_error(401)):
            outcomes = [error, self.branches()]
            with self.subTest(code=error.code), self.assertRaisesRegex(RuntimeError, rf'HTTP {error.code}'):
                self.retrying_read(outcomes)
            self.assertEqual(outcomes, [self.branches()])

    def test_untrusted_header_values_are_not_echoed(self):
        error = self.http_error(429, {'Retry-After': '1', 'X-RateLimit-Resource': 'core token=ghs_secret\nInjected'})
        with self.assertRaises(RuntimeError) as raised:
            self.retrying_read([error] * 10, attempts=2)
        self.assertNotIn('ghs_secret', str(raised.exception))
        self.assertNotIn('x-ratelimit-resource', str(raised.exception))

    def test_retried_reads_still_reject_misconfigured_environments(self):
        from testnet_ci import verify_environment
        retry = lambda: self.http_error(429, {'Retry-After': '1'})
        for branches in ({'total_count':2,'branch_policies':self.branches()['branch_policies']},
                         {'total_count':1,'branch_policies':[{'id':7,'name':'*','type':'branch'}]}):
            with self.subTest(branches=branches):
                read = lambda path, b=branches: self.retrying_read([retry(), b])[0]
                with self.assertRaisesRegex(RuntimeError, 'main-only'):
                    verify_environment('a/b', Mock(return_value=self.environment()), read)
        for environment in (dict(self.environment(), can_admins_bypass=True),
                            dict(self.environment(), protection_rules=[{'type': 'required_reviewers'}])):
            with self.subTest(environment=environment):
                read = lambda path: self.retrying_read([retry(), self.branches()])[0]
                with self.assertRaisesRegex(RuntimeError, 'main-only'):
                    verify_environment('a/b', Mock(return_value=environment), read)
        read = lambda path: self.retrying_read([retry(), self.branches()])[0]
        self.assertEqual(verify_environment('a/b', Mock(return_value=self.environment()), read)['policy_id'], 7)

    def test_recheck_reuses_this_jobs_verified_policy_only_when_quota_is_exhausted(self):
        import tempfile
        from testnet_ci import PublicRateLimited, verify_environment
        exhausted = lambda: self.http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(1_000_000 + 2103)})
        with self.assertRaises(PublicRateLimited):
            self.retrying_read([exhausted()])
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'policy.json'
            # Planning step: a fresh public read is verified, then recorded.
            verify_environment('a/b', Mock(return_value=self.environment()), Mock(return_value=self.branches()), record=record)
            self.assertEqual(json.loads(record.read_text()), self.branches())
            limited = lambda path: self.retrying_read([exhausted()])[0]
            proof = verify_environment('a/b', Mock(return_value=self.environment()), limited, fallback=record)
            self.assertEqual(proof['policy_id'], 7)
            self.assertIn('planning-step record', proof['branch_policy_transport'])
            # The authenticated environment is still read afresh and must stay ready.
            for environment in (dict(self.environment(), can_admins_bypass=True),
                                dict(self.environment(), protection_rules=[{'type': 'required_reviewers'}])):
                with self.subTest(environment=environment), self.assertRaisesRegex(RuntimeError, 'main-only'):
                    verify_environment('a/b', Mock(return_value=environment), limited, fallback=record)
            # A misconfigured or malformed record never passes.
            record.write_text(json.dumps({'total_count': 1, 'branch_policies': [{'id': 7, 'name': '*', 'type': 'branch'}]}))
            with self.assertRaisesRegex(RuntimeError, 'main-only'):
                verify_environment('a/b', Mock(return_value=self.environment()), limited, fallback=record)
            for content in ('not json', '[]'):
                record.write_text(content)
                with self.subTest(content=content), self.assertRaises(PublicRateLimited):
                    verify_environment('a/b', Mock(return_value=self.environment()), limited, fallback=record)
            # A real denial, transport failure or missing record still fails closed.
            record.write_text(json.dumps(self.branches()))
            denied = lambda path: self.retrying_read([self.http_error(404, {}, b'{"message":"Not Found"}')])[0]
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404') as raised:
                verify_environment('a/b', Mock(return_value=self.environment()), denied, fallback=record)
            self.assertNotIsInstance(raised.exception, PublicRateLimited)
            from urllib.error import URLError
            offline = lambda path: self.retrying_read([URLError('offline')] * 10, attempts=2)[0]
            with self.assertRaises(RuntimeError) as raised:
                verify_environment('a/b', Mock(return_value=self.environment()), offline, fallback=record)
            self.assertNotIsInstance(raised.exception, PublicRateLimited)
            with self.assertRaises(PublicRateLimited):
                verify_environment('a/b', Mock(return_value=self.environment()), limited, fallback=Path(directory) / 'absent.json')
            with self.assertRaises(PublicRateLimited):
                verify_environment('a/b', Mock(return_value=self.environment()), limited)

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

class BytecodeFixtureTests(unittest.TestCase):
    def test_pair_compiler_framework_and_verified_digest_are_required_for_migration(self):
        import copy, live_package_upgrade as live
        tools={'build':{'version':'pinned compiler'},'network':{'sha256':'pinned network'}}
        framework={'base':'framework-a','head':'framework-a'}
        proof=dict(build_cli='pinned compiler',network_cli_sha256='pinned network',framework=framework,
                   verified_transactions={'base':{'digest':'aa'},'upgrade':{'digest':'bb'}})
        good={'provenance':proof,'public_marker':'existing'}
        self.assertIs(live.matching_fixture({'fixtures':{'old source hash':good}},'bytecode-v1:aa','bytecode-v1:bb',tools,framework),good)
        for edit in [lambda p:p.update(build_cli='other'),lambda p:p.update(network_cli_sha256='other'),lambda p:p.update(framework={}),lambda p:p['verified_transactions']['upgrade'].update(digest='cc')]:
            bad=copy.deepcopy(good);edit(bad['provenance'])
            self.assertIsNone(live.matching_fixture({'fixtures':{'old':bad}},'bytecode-v1:aa','bytecode-v1:bb',tools,framework))
        self.assertIsNone(live.matching_fixture({'fixtures':{'old':good}},'bytecode-v1:bb','bytecode-v1:aa',tools,framework))
    def test_unknown_setup_is_never_retried_and_refusals_do_not_hide_publication(self):
        import tempfile, live_package_upgrade as live
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'private-state';p={};state={'publications':{'pair':p}}
            ledger=SimpleNamespace(evidence=[])
            def interrupted(persist):
                persist({'digest':'retained','tx_bytes':'AAAA'})  # Durable before signing.
                raise RuntimeError('transport outcome unknown')
            with self.assertRaises(RuntimeError):live.checkpoint_setup(path,state,p,'publish base',interrupted,ledger)
            marker=json.loads(path.read_text())['publications']['pair']['pending_operation']
            self.assertEqual((marker['label'],marker['tx']),('publish base',{'digest':'retained','tx_bytes':'AAAA'}))
            refused=Mock()
            with self.assertRaises(RuntimeError):live.checkpoint_setup(path,state,p,'publish again',refused,ledger)
            refused.assert_not_called()
            p.clear()
            def not_sent(persist):raise live.toolchain.NotSubmitted('refused before sign')
            with self.assertRaises(live.toolchain.NotSubmitted):live.checkpoint_setup(path,state,p,'publish base',not_sent,ledger)
            self.assertNotIn('pending_operation',json.loads(path.read_text())['publications']['pair'])
            self.assertEqual(live.checkpoint_setup(path,state,p,'publish base',lambda persist:{'id':'known'},ledger),{'id':'known'})
            self.assertNotIn('pending_operation',p)
            self.assertIn('pending_operation',json.loads(path.read_text())['publications']['pair'],'success is checkpointed only with its IDs by the caller')
    def test_malformed_private_state_does_not_trigger_a_fresh_publication(self):
        import tempfile, live_package_upgrade as live
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'private-state';path.write_text('malformed')
            with self.assertRaisesRegex(RuntimeError,'reconcile'):live.load_state(path,'chain','owner')


class FreshFixtureFlowTests(unittest.TestCase):
    def test_new_and_resumed_publications_reach_verified_submitter_with_saved_proof(self):
        # Exercise run(), not just checkpoint helpers: the original NameError
        # happened between compilation and the first pre-sign verification.
        import io, tarfile, tempfile, subprocess
        from types import SimpleNamespace
        import live_package_upgrade as live
        address = "0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac"
        tools = {"build": {"path": "/compiler", "version": live.toolchain.BUILD["version"]},
                 "network": {"path": "/network", "version": live.toolchain.NETWORK["version"], "sha256": "pinned"}}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("contracts/Move.lock"); data = b"offline fixture"
            info.size = len(data); archive.addfile(info, io.BytesIO(data))
        real_run = subprocess.run
        def command(argv, **kwargs):
            if argv[0] == "tar":
                return real_run(argv, **kwargs)
            if argv[0] == "git":
                return SimpleNamespace(stdout=buffer.getvalue(), returncode=0)
            self.assertEqual(argv[0], "/network")
            if "envs" in argv:
                output = json.dumps([[{"alias": "testnet", "rpc": "https://offline.invalid"}], "testnet"])
            elif "active-address" in argv:
                output = address
            else:
                self.assertIn("gas", argv)
                output = json.dumps({"addressMistBalance": "1000000000", "gasCoins": []})
            return SimpleNamespace(stdout=output, stderr="", returncode=0)
        for resumed in (False, True):
            with self.subTest(resumed=resumed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); state_path = root/"state.json"
                digest = "bytecode-v1:" + bytes(range(32)).hex()
                key = live.fixture_key(digest, digest)
                proof = {"base": {"digest": "saved-proof"}} if resumed else {}
                state = {"chain": "4c78adac", "operator": address, "fixtures": {}, "publications": {}}
                if resumed:
                    state["publications"][key] = {"source_pair": [digest, digest],
                        "compiler": tools["build"]["version"], "built": proof}
                live.save_state(state_path, state)
                observed = []
                def submitter(*args, **kwargs):
                    observed.append(dict(kwargs["record"]))
                    def refuse(_budget):
                        raise live.toolchain.NotSubmitted("offline pre-sign boundary reached")
                    return refuse
                args = SimpleNamespace(wallet=str(root/"client.yaml"), network="testnet",
                    evidence=str(root/"evidence.json"), max_spend_mist=800000000,
                    build_sui="/compiler", network_sui="/network", faucet_tool=None,
                    base_ref="b"*40, walrus="simulated", state=str(state_path), fresh=False)
                walrus = SimpleNamespace(aggregator="http://offline.invalid", mode="simulated", close=Mock())
                with patch.object(live.subprocess, "run", side_effect=command), \
                     patch.object(live.subprocess, "check_output", return_value="b"*40), \
                     patch.object(live.toolchain, "check_tools", return_value=tools), \
                     patch.object(live.toolchain, "preflight", return_value={"protocol_version":139}), \
                     patch.object(live.toolchain, "compile_package", return_value={"digest":list(range(32))}), \
                     patch.object(live.toolchain, "framework_pins", return_value={}), \
                     patch.object(live.toolchain, "package_submitter", side_effect=submitter), \
                     patch.object(live.dm, "chain_facts", return_value={"chain_identifier":"4c78adac"}), \
                     patch.object(live.live_common, "Walrus", return_value=walrus), \
                     patch.dict("os.environ", {"SUIFLEET_TESTNET_LOCK_TOKEN":""}):
                    with self.assertRaisesRegex(live.toolchain.NotSubmitted, "offline pre-sign boundary"):
                        live.run(args)
                self.assertEqual(observed, [proof.get("base", {})])
                saved = json.loads(state_path.read_text())["publications"][key]
                self.assertEqual(saved["built"], {"base": proof.get("base", {})})
                self.assertNotIn("pending_operation", saved)
                evidence = json.loads((root/"evidence.json").read_text())
                self.assertFalse(evidence["success"])
                self.assertEqual(evidence["allowance_used_mist"], 0)
                walrus.close.assert_called_once()


class PublicationAuthorityTests(unittest.TestCase):
    def test_checkpoint_recovery_requires_latest_cap_package_version_and_owners(self):
        import live_package_upgrade as live
        operator = live.dm.object_id("0xa")
        publication = dict(base_package="0x1", registry="0x2", upgrade_cap="0x3")
        owners = {"0x3": {"AddressOwner": operator}, "0x2": {"Shared": {}}}
        def reader(command, object_id):
            self.assertEqual(command, "object")  # Recovery is read-only.
            return {"owner": owners[object_id]}
        for upgraded in (False, True):
            with self.subTest(upgraded=upgraded):
                current = dict(publication)
                if upgraded:
                    current["head_package"] = "0x4"
                package = current.get("head_package", current["base_package"])
                cap = dict(kind="object", type="0x2::package::UpgradeCap",
                           upgrade_cap={"package": package, "version": 2 if upgraded else 1},
                           owner={"address": operator})
                facts = {"objects": {live.dm.object_id("0x3"): cap}}
                with patch.object(live.dm, "chain_facts", return_value=facts):
                    live.verify_publication(current, "offline", operator, reader)
                    for field, bad in (("package", "0x9"), ("version", 3)):
                        previous = cap["upgrade_cap"][field]; cap["upgrade_cap"][field] = bad
                        with self.assertRaises(AssertionError):
                            live.verify_publication(current, "offline", operator, reader)
                        cap["upgrade_cap"][field] = previous
                    owners["0x3"] = {"AddressOwner": live.dm.object_id("0xb")}
                    with self.assertRaises(AssertionError):
                        live.verify_publication(current, "offline", operator, reader)
                    owners["0x3"] = {"AddressOwner": operator}
                    owners["0x2"] = {"AddressOwner": operator}
                    with self.assertRaises(AssertionError):
                        live.verify_publication(current, "offline", operator, reader)
                    owners["0x2"] = {"Shared": {}}
