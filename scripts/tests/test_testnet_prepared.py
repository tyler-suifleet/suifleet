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

    def test_workflow_prepares_outside_locks_then_checks_before_signing(self):
        import yaml
        workflow=Path(__file__).resolve().parents[2]/'.github/workflows/testnet.yml'
        jobs=yaml.safe_load(workflow.read_text())['jobs'];prep=jobs['prepare'];live=jobs['validate'];prune=jobs['prune-fixture-history']
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
        self.assertNotIn('packages',prep['permissions']);self.assertNotIn('actions: write',workflow.read_text().split('  prune-fixture-history:')[0])


if __name__=='__main__':unittest.main()
