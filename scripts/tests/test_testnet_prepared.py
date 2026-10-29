import copy
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import testnet_pool as pool
import testnet_prepared as prepared
import testnet_fixture_cache as cache
import ci_testnet_wallet as wallet


class PreparedTests(unittest.TestCase):
    def setUp(self):
        self.labels=prepared.expected('a/b','a'*40,'b'*40,12,1,'c'*40)

    def test_image_digest_and_all_provenance_fields_are_required(self):
        prepared.verify(self.labels,self.labels)
        for field in self.labels:
            changed={**self.labels,field:'wrong'}
            with self.assertRaises(RuntimeError):prepared.verify(changed,self.labels)
        self.assertEqual(prepared.image_ref('A/B','sha256:'+'d'*64),'ghcr.io/a/b-testnet-prepared@sha256:'+'d'*64)
        for digest in ('latest','sha256:bad','sha256:'+'d'*64+'\n'):
            with self.assertRaises(ValueError):prepared.image_ref('a/b',digest)

    def test_package_allowlist_excludes_wallet_state_and_cargo_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root';root.mkdir()
            (root/'dapp/node_modules/lib').mkdir(parents=True)
            (root/'dapp/node_modules/lib/index.js').write_text('public dependency')
            for n in ('suifleet','examples/chain_facts'):
                p=root/'suifleet-edge/target/debug'/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'binary')
            (root/'device.key').write_text('PRIVATE CANARY')
            (root/'live-state.json').write_text('PRIVATE CANARY')
            destination=Path(d)/'output'
            prepared.package(root,destination,self.labels)
            self.assertEqual({p.name for p in destination.iterdir()},{'Dockerfile','node_modules','suifleet','chain_facts'})
            self.assertNotIn('PRIVATE CANARY',''.join(p.read_text() for p in destination.rglob('*') if p.is_file()))
            self.assertNotIn('RUN ',(destination/'Dockerfile').read_text())

    def test_registry_retention_preserves_recent_and_foreign_versions(self):
        now=datetime(2026,10,28,tzinfo=timezone.utc)
        def v(i,age,tags):return {'id':i,'created_at':(now-timedelta(hours=age)).isoformat(),'metadata':{'container':{'tags':tags}}}
        versions=[v(i,i,['run-'+str(i)+'-1']) for i in range(1,61)]
        versions += [v(101,100,['production']),v(102,100,[]),v(103,100,['run-1-1','production'])]
        delete=prepared.prune_versions(versions,now)
        self.assertEqual(delete,list(range(49,61)))
        self.assertNotIn(101,delete)
        self.assertEqual(prepared.prune_versions(versions[:10],now),[])

    def test_registry_cleanup_refuses_live_or_foreign_producers(self):
        now=datetime.now(timezone.utc)
        versions=[{'id':i,'created_at':(now-timedelta(days=5)).isoformat(),
                   'metadata':{'container':{'tags':[f'run-{i}-1']}}} for i in range(1,16)]
        def run_api(url):
            run=int(url.rsplit('/',1)[1])
            value={'status':'completed','head_branch':'main','path':'.github/workflows/testnet.yml','repository':{'full_name':'a/b'}}
            if run==12:value['status']='in_progress'
            if run==13:value['path']='other.yml'
            if run==14:value['repository']={'full_name':'other/repository'}
            if run==15:value['head_branch']='feature'
            return value
        with patch.object(prepared.subprocess,'check_output',return_value=json.dumps(versions)), patch('testnet_ci.api',side_effect=run_api), patch.object(prepared.subprocess,'run') as delete:
            result=prepared.prune('a/b')
            self.assertEqual(result['deleted_versions'],[11])
            self.assertEqual(delete.call_count,1)
            self.assertTrue(delete.call_args.args[0][-1].endswith('/11'))

    def test_preparation_has_only_workflow_run_cache_scope(self):
        import yaml
        workflows = Path(__file__).resolve().parents[2]/'.github/workflows'
        # BaseLoader preserves the YAML 1.2 `on` key as a string.
        controller = yaml.load((workflows/'testnet.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(controller['on']), {'workflow_run'})
        self.assertEqual(controller['on']['workflow_run']['workflows'], ['CI'])
        self.assertEqual(controller['on']['workflow_run']['types'], ['completed'])
        request = yaml.load((workflows/'testnet-request.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(request['on']), {'schedule', 'workflow_dispatch'})
        job = request['jobs']['request']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(job['permissions'], {'actions': 'write'})
        self.assertEqual(len(job['steps']), 1)
        self.assertEqual(job['steps'][0]['run'], 'gh workflow run ci.yml --ref main')
        self.assertNotIn('checkout', json.dumps(job))
        self.assertNotIn('secrets.', json.dumps(job))

    def test_candidate_clone_preserves_controller_and_has_complete_history(self):
        import subprocess
        from testnet_ci import checkout_candidate
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin = root/'origin'; controller = root/'controller'
            def git(*args, cwd=origin):
                return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()
            origin.mkdir()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (origin/'controller.py').write_text('trusted')
            git('add', '.'); git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('clone', '--no-hardlinks', str(origin), str(controller), cwd=root)
            git('checkout', '--detach', base, cwd=controller)
            # Main may advance after the controller clone. Keep this new base
            # off the candidate ancestry so fetching only head cannot supply it.
            (origin/'base-info').write_text('new base')
            git('add', 'base-info'); git('commit', '-qm', 'advanced base')
            advanced_base = git('rev-parse', 'HEAD')
            git('checkout', '--detach', base)
            (origin/'controller.py').write_text('candidate')
            git('commit', '-qam', 'head')
            head = git('rev-parse', 'HEAD')
            def run(args, **kwargs):
                return subprocess.run(args, cwd=controller, **kwargs)
            checkout_candidate(head, advanced_base, controller/'candidate', run)
            self.assertEqual((controller/'controller.py').read_text(), 'trusted')
            self.assertEqual(git('rev-parse', 'HEAD', cwd=controller), base)
            self.assertEqual((controller/'candidate/controller.py').read_text(), 'candidate')
            self.assertEqual(git('rev-parse', 'HEAD', cwd=controller/'candidate'), head)
            self.assertEqual(git('show', base+':controller.py', cwd=controller/'candidate'), 'trusted')
            # Exercise the same archive operation used to prepare base contracts.
            import io
            import tarfile
            archived = subprocess.check_output(
                ['git', 'archive', advanced_base, 'base-info'], cwd=controller/'candidate')
            with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
                self.assertEqual(archive.extractfile('base-info').read(), b'new base')
            self.assertTrue((controller/'candidate/.git').is_dir())
            for bad in ('main', head+'\n', ''):
                with self.assertRaises(RuntimeError):
                    checkout_candidate(bad, base, root/'absent', run)
            with self.assertRaises(RuntimeError):
                checkout_candidate(head, base, controller/'candidate', run)

    def test_revision_preparation_has_no_shared_actions_cache(self):
        import yaml
        workflow=Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml'
        prep=yaml.safe_load(workflow.read_text())['jobs']['prepare']
        # workflow_run cache access is independent of GITHUB_TOKEN permissions.
        # Keep revision execution away from both explicit and implicit cache actions.
        self.assertFalse(any('cache' in s.get('uses', '') or
                             'docker/setup-buildx-action@' in s.get('uses', '') or
                             'docker/build-push-action@' in s.get('uses', '')
                             for s in prep['steps']))
        self.assertNotIn('type=gha', json.dumps(prep))
        build=next(s for s in prep['steps'] if s.get('name') == 'Build isolated testnet environment')
        self.assertIn('docker build --tag suifleet-dev:testnet', build['run'])
        self.assertIn('--build-arg USER_UID="$USER_UID"', build['run'])
        self.assertIn('--build-arg USER_GID="$USER_GID"', build['run'])

    def test_workflow_prepares_outside_locks_then_checks_before_signing(self):
        import yaml
        workflow=Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml'
        jobs=yaml.safe_load(workflow.read_text())['jobs'];prep=jobs['prepare'];live=jobs['validate'];prune=jobs['prune-fixture-history']
        for job in (jobs['plan'], live):
            self.assertIn('github.event.workflow_run.head_repository.full_name == github.repository', job['if'])
        self.assertEqual(prep['needs'], 'plan')
        self.assertEqual(prep['permissions'], {'contents':'read', 'actions':'read', 'pull-requests':'read'})
        for name in ('plan', 'preparation-status'):
            trusted = jobs[name]
            self.assertEqual(trusted['permissions']['statuses'], 'write')
            for step in trusted['steps']:
                if 'checkout@' in step.get('uses', ''):
                    self.assertEqual(step['with']['ref'], '${{ github.workflow_sha }}')
        failure = jobs['preparation-status']
        self.assertEqual(failure['needs'], ['plan', 'prepare'])
        self.assertIn("needs.prepare.result != 'success'", failure['if'])
        self.assertIn('--status error', failure['steps'][-1]['run'])
        self.assertNotIn('environment',prep);self.assertNotIn('concurrency',prep)
        self.assertNotIn('secrets.',json.dumps(prep))
        self.assertEqual(live['environment'],'live-package-upgrade')
        self.assertEqual(live['concurrency'],prune['concurrency'])
        self.assertEqual(live['needs'], ['prepare', 'publish'])
        publisher=jobs['publish']
        self.assertEqual(publisher['permissions'], {'contents':'read', 'actions':'read', 'packages':'write', 'statuses':'write'})
        checkouts=[s for s in publisher['steps'] if 'checkout@' in s.get('uses','')]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]['with']['ref'], '${{ github.workflow_sha }}')
        self.assertNotIn('docker run', json.dumps(publisher))
        self.assertNotIn('docker build', json.dumps(publisher))
        self.assertIn('docker load', json.dumps(publisher))
        self.assertIn('needs.publish.outputs.digest', json.dumps(live))
        upload=next(s for s in prep['steps'] if 'upload-artifact@' in s.get('uses',''))
        download=next(s for s in publisher['steps'] if 'download-artifact@' in s.get('uses',''))
        self.assertEqual(upload['with']['name'], 'prepared-image-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(download['with']['name'], upload['with']['name'])
        self.assertNotIn('run-id', download['with'])
        self.assertNotIn('repository', download['with'])
        terminal=publisher['steps'][-1]
        self.assertEqual(terminal['if'], 'failure() || cancelled()')
        self.assertIn('--status error', terminal['run'])
        self.assertNotIn('strategy',live) # one acceptance, never one per pool member
        steps=live['steps'];names=[s.get('name','') for s in steps]
        self.assertLess(names.index('Pull and verify immutable prepared image without signing credentials'),names.index('Run bounded testnet acceptance'))
        self.assertLess(names.index('Recheck head and base immediately before spending'),names.index('Run bounded testnet acceptance'))
        self.assertNotIn('cargo build',json.dumps(live));self.assertNotIn('pnpm --dir',json.dumps(live))
        for job in (prep, live):
            checkouts = [step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@')]
            self.assertEqual(len(checkouts), 1)
            self.assertEqual(checkouts[0]['with']['ref'], '${{ github.workflow_sha }}')
            commands = [step.get('run', '') for step in job['steps']]
            self.assertTrue(any('--checkout-candidate candidate' in command for command in commands))
            for command in commands:
                if 'docker run ' in command:
                    self.assertIn('-v "$PWD/candidate:/workspace"', command)
                    self.assertNotIn('-v "$PWD:/workspace"', command)
        final = next(step for step in steps if step.get('name') == 'Publish final exact-head status')
        self.assertNotIn('working-directory', final)  # Also report a failed clone.
        self.assertEqual(final['env']['TEST_EVIDENCE_DIR'], 'candidate/evidence')

        self.assertNotIn('packages',prep['permissions']);self.assertNotIn('actions: write',workflow.read_text().split('  prune-fixture-history:')[0])


if __name__=='__main__':unittest.main()
