import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_walrus_site as site
import live_walrus_site as live_site

class SiteBindingTests(unittest.TestCase):
    def test_binding_refuses_foreign_chain_registry_and_unexpected_fields(self):
        manifest=json.loads((site.ROOT/'manifests/fixture.localnet.json').read_text())
        data=dict(schema=1,chainIdentifier=manifest['chain_identifier'],registryId=manifest['shared_objects']['registry']['id'],
                  packageId='0xa1',typeOrigin='0xa0',fleetId='0xf1',budgetId='0xb1',storageId='0xb2')
        checked=site.fleet_binding(json.dumps(data),manifest)
        self.assertEqual(checked['packageId'],site.object_id('0xa1'))
        for key,value in [('chainIdentifier','ffffffff'),('registryId','0xff'),('secret','never publish')]:
            with self.assertRaises(ValueError):site.fleet_binding(json.dumps({**data,key:value}),manifest)
    def test_storage_binding_keeps_all_public_origins_separate(self):
        value = dict(packageId='0x3',storageOrigin='0x1',retentionOrigin='0x2')
        checked = site.storage_binding(json.dumps(value), {'network':'testnet'})
        self.assertEqual(checked['storageOrigin'],site.object_id('0x1'))
        self.assertEqual(checked['retentionOrigin'],site.object_id('0x2'))
        for network, data in [('mainnet',value),('testnet',dict(value,secret='forbidden')),('testnet',dict(value,packageId='not an id'))]:
            with self.assertRaises(ValueError):site.storage_binding(json.dumps(data), {'network':network})
    def test_only_public_seal_binding_and_valid_threshold(self):
        good = dict(keyServers=[dict(objectId='0x1',weight=1),dict(objectId='0x2',weight=1)], threshold=2)
        self.assertEqual(site.seal_binding(json.dumps(good))['keyServers'][0]['objectId'],site.object_id('0x1'))
        for value in [dict(good,threshold=3),dict(good,threshold=True),dict(good,api_key='forbidden'),
                      dict(good,keyServers=[dict(objectId='0x1',weight=1,api_key='forbidden')]),
                      dict(good,keyServers=[dict(objectId='0x1',weight=1),dict(objectId='0x01',weight=1)])]:
            with self.assertRaises(ValueError):site.seal_binding(json.dumps(value))

    def test_inventory_rejects_private_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'index.html').write_text('public');(root/'device.key').write_text('fixture')
            with self.assertRaises(ValueError):site.inventory(root)
            (root/'device.key').unlink();(root/'alias').symlink_to(root/'index.html')
            with self.assertRaises(ValueError):site.inventory(root)


class SiteRefreshTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(files={'/index.html': {'sha256':'same','bytes':4}}, rpc='testnet', owner='publisher', journal='private-journal', key='private-key-path', operations=[dict(name='update/6/hash')])
        self.request = dict(self.saved, operations=[dict(name='refresh/6/hash')])
        self.state = dict(intents={'site/update/6/hash':dict(digest='known-digest',status='success')})
    def test_refresh_requires_matching_effects_and_distinct_intent(self):
        calls=[]
        def effects(digest):
            calls.append(digest)
            return dict(digest=digest,effects=dict(status=dict(status='success')))
        self.assertEqual(live_site.refresh_effects(self.saved,self.request,self.state,effects),['known-digest'])
        self.assertEqual(calls,['known-digest'])
        with self.assertRaises(ValueError):live_site.refresh_effects(self.saved,self.saved,self.state,effects)
    def test_uncertain_or_failed_intents_never_get_replaced(self):
        for status in ['unknown','failure']:
            state=copy.deepcopy(self.state);state['intents']['another']=dict(status=status)
            with self.assertRaises(ValueError):live_site.refresh_effects(self.saved,self.request,state,lambda _:self.fail('must not query after uncertain intent'))
        with self.assertRaises(ValueError):live_site.refresh_effects(self.saved,self.request,dict(intents={}),lambda _:self.fail('must not query missing intent'))
    def test_foreign_boundaries_and_unverified_effects_are_refused(self):
        for key in ['files','rpc','owner','journal','key']:
            request=dict(self.request);request[key]='foreign'
            with self.assertRaises(ValueError):live_site.refresh_effects(self.saved,request,self.state,lambda _:self.fail('must reject boundary before effects'))
        for result in [None,dict(digest='different',effects=dict(status=dict(status='success'))),dict(digest='known-digest',effects=dict(status=dict(status='failure')))]:
            with self.assertRaises(ValueError):live_site.refresh_effects(self.saved,self.request,self.state,lambda _:result)

    def test_operation_export_overrides_cannot_cross_refresh_boundary(self):
        for override in [dict(files={'/other.html':{}}),dict(directory='foreign'),dict(marker='different bytes')]:
            saved=copy.deepcopy(self.saved);saved['operations'][0].update(override)
            with self.assertRaises(ValueError):live_site.refresh_effects(saved,self.request,self.state,lambda _:self.fail('must reject operation override before effects'))
