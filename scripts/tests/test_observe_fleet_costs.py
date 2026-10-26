"""Offline adapter contract tests; SDK-shaped fixtures, no network or wallets."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
D = '2' * 43
A = '0x' + 'a' * 64


@unittest.skipUnless(shutil.which('node'), 'Node required')
class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'node_modules/@mysten/sui'
        (self.base / 'dist/grpc').mkdir(parents=True)
        (self.base / 'dist/utils').mkdir(parents=True)
        (self.base / 'package.json').write_text('{"version":"2.36.0"}')
        (self.base / 'dist/utils/index.mjs').write_text(
            'export const fromBase58 = () => { throw Error("unexpected base58"); };'
            'export const toHex = () => { throw Error("unexpected base58"); };')
        self.run = dict(schema_version=1, deployment=dict(network='localnet',
                       chain_identifier='1234abcd', rpc_url='https://fixture.invalid'),
                       workload=dict(max_transactions=2), run_id='offline',
                       interval=dict(start='2026-10-01T00:00:00Z'),
                       transactions=[dict(digest=D, category='receipts')])
        self.tx = dict(digest=D, timestampMs=1790812800000, status=dict(success=True),
                       effects=dict(transactionDigest=D, gasUsed=dict(computationCost='1',
                                    storageCost='2', storageRebate='0')),
                       transaction=dict(sender=A, gasData=dict(owner=A)), checkpoint='1', balanceChanges=[])

    def execute(self, chain='1234abcd', error=False):
        (self.root / 'run.json').write_text(json.dumps(self.run))
        module = 'export class SuiGrpcClient { core = {'
        module += 'getChainIdentifier: async () => (' + json.dumps(dict(chainIdentifier=chain)) + '),'
        module += 'getTransaction: async () => {'
        module += 'throw Error("unavailable")' if error else 'return ' + json.dumps({'Transaction': self.tx})
        module += '} }; }'
        (self.base / 'dist/grpc/index.mjs').write_text(module)
        return subprocess.run(['node', str(ROOT / 'scripts/observe_fleet_costs.mjs'),
                               str(self.root), str(self.root / 'run.json')],
                              capture_output=True, text=True)

    def test_positive_and_failed_effects(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['records'][0]['status'], 'success')
        self.tx['status']['success'] = False
        self.assertEqual(json.loads(self.execute().stdout)['records'][0]['status'], 'failure')

    def test_unknown_read_is_not_free_success(self):
        result = self.execute(error=True)
        record = json.loads(result.stdout)['records'][0]
        self.assertEqual(record['status'], 'unknown')
        self.assertNotIn('gas', record)

    def test_wrong_chain_refuses_output(self):
        result = self.execute(chain='ffffffff')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_digest_mismatch_and_missing_effects_refuse(self):
        self.tx['digest'] = '3' * 43
        self.assertNotEqual(self.execute().returncode, 0)
        self.tx['digest'] = D
        self.tx.pop('effects')
        self.assertNotEqual(self.execute().returncode, 0)

    def test_bounds_duplicates_and_wrong_sdk_refuse(self):
        self.run['workload']['max_transactions'] = 0
        self.assertNotEqual(self.execute().returncode, 0)
        self.run['workload']['max_transactions'] = 2
        self.run['transactions'] *= 2
        self.assertNotEqual(self.execute().returncode, 0)
        self.run['transactions'] = self.run['transactions'][:1]
        (self.base / 'package.json').write_text('{"version":"2.37.0"}')
        self.assertNotEqual(self.execute().returncode, 0)


if __name__ == '__main__':
    unittest.main()
