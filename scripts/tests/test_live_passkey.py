"""Fail-closed fixture reuse and encrypted-state plumbing; no chain writes."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_passkey_factor as target
from ci_testnet_wallet import save_pending, restore_pending


class CachedPasskeyTests(unittest.TestCase):
    def test_signing_identity_and_uncertainty_survive_ci_roundtrip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'cache.json'
            state.write_text('{}')
            values = {'live-passkey-state.keys.json': '[{"privateKey":"fixture-only"}]',
                      'live-passkey-state.json': '{"pending":{"digest":"uncertain"}}'}
            for name, value in values.items():
                (root / name).write_text(value)
            save_pending(root, state)
            for name in values:
                (root / name).unlink()
            restore_pending(root, state)
            for name, value in values.items():
                self.assertEqual((root / name).read_text().strip(), value)
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)

    def test_wrong_lineage_and_pending_refuse_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            live = SimpleNamespace(wallet=root / 'client.yaml', chain='chain', operator='0x'+'1'*64,
                rpc_url='read-only', object=Mock(), transaction=Mock(), evidence={})
            fixture = dict(base_package='0x'+'2'*64, head_package='0x'+'3'*64,
                           upgrade_cap='cap', registry='registry')
            cache = dict(fixtures={'key': fixture})
            with patch('live_package_upgrade.load_state', return_value=cache), \
                 patch('live_wallet_gate.head_fixture', return_value=fixture), \
                 patch.object(target, 'contracts_digest', return_value='source'), \
                 patch.object(target.dm, 'chain_facts', return_value={}), \
                 patch.object(target.dm, 'origins', return_value={'passkey_factor::Enrollment': fixture['head_package']}):
                live.object.return_value = dict(owner={'AddressOwner': '0x'+'4'*64},
                    content={'package': fixture['head_package'], 'version': 2})
                with self.assertRaises(AssertionError):
                    target.cached_acceptance(live, root / 'package.json')
                live.transaction.assert_not_called()
                live.object.side_effect = [dict(owner={'AddressOwner': live.operator},
                    content={'package': fixture['head_package'], 'version': 2}), dict(owner={'Shared': {}})]
                private = root / 'live-passkey-state.json'
                private.write_text(json.dumps({'pending': {'digest': 'uncertain'}}))
                with patch.object(target.dm, 'chain_facts', return_value={'objects': {fixture['head_package']: {}}}):
                    with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                        target.cached_acceptance(live, root / 'package.json')
                live.transaction.assert_not_called()
                self.assertEqual(json.loads(private.read_text())['pending']['digest'], 'uncertain')
