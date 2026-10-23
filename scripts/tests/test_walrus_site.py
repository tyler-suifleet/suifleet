import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_walrus_site as site

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
