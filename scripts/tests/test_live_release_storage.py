"""Offline signer-policy tests; no wallet, CLI signing or network calls."""
import copy
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_release_storage import verify_step
from sui_toolchain import NotSubmitted
from deployment_manifest import object_id

class ReleaseSigning(unittest.TestCase):
    def setUp(self):
        self.sender = object_id('0x1')
        self.blob = object_id('0xf')
        self.system = '0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af'
        self.tx = dict(sender=self.sender, gas_owner=self.sender, gas_budget='30000000',
            inputs=[dict(objectId=self.system), dict(objectId=self.blob)], commands=[
                dict(kind='MoveCall', package=object_id('0xb'), module='system', function='extend_blob', arguments=[dict(kind='Input', index=0), dict(kind='Input', index=1)]),
                dict(kind='MoveCall', package=object_id('0x2'), module='coin', function='destroy_zero', arguments=[]),
            ])
    def check_tx(self, tx):
        verify_step(tx, 'in-app wallet storage extension', self.sender, 30000000, '0xa', '0xb', self.blob)
    def test_expected_extension(self):
        self.check_tx(self.tx)
    def test_refuses_sender_gas_target_and_transfer_changes(self):
        changes = [lambda t:t.update(sender=object_id('0x2')), lambda t:t.update(gas_owner=object_id('0x2')),
            lambda t:t.update(gas_budget='31000000'), lambda t:t['inputs'][1].update(objectId=object_id('0xe')),
            lambda t:t['inputs'][0].update(objectId=object_id('0xe')),
            lambda t:t['commands'][0].update(package=object_id('0xc')),
            lambda t:t['commands'].append(dict(kind='TransferObjects')),
            lambda t:t['commands'][1].update(function='transfer')]
        for change in changes:
            tx=copy.deepcopy(self.tx);change(tx)
            with self.assertRaises(NotSubmitted):self.check_tx(tx)
    def test_publication_is_one_pinned_call(self):
        tx=copy.deepcopy(self.tx)
        tx['commands']=[dict(kind='MoveCall',package=object_id('0xa'),module='component_catalog',function='publish_version')]
        verify_step(tx,'publish chosen-period version',self.sender,30000000,'0xa')
        tx['commands'].append(dict(kind='SplitCoins'))
        with self.assertRaises(NotSubmitted):verify_step(tx,'publish chosen-period version',self.sender,30000000,'0xa')

if __name__ == '__main__':unittest.main()
