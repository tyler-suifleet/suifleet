"""Offline SF-091 clean-operator restore and frontend rollback checks against
the committed public SF-082 Testnet receipts; no network or keys."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleet_recovery
from build_walrus_site import canonical

V = ROOT / 'docs/validation'
PUBLIC = dict(manifest=V / 'sf082-testnet-deployment.json', build_receipt=V / 'sf082-site-build-a.json',
              publication_receipt=V / 'sf082-site-publication.json', fleet_data=V / 'sf082-testnet-fleet.json',
              seal_settings=V / 'sf082-testnet-seal.json', fleet_storage=V / 'sf082-testnet-storage.json')


def arguments(**paths):
    values = PUBLIC | paths
    result = []
    for name, value in values.items():
        if value is not None:
            result += ['--' + name.replace('_', '-'), str(value)]
    return result


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fleet_recovery.main(argv)
    return code, out.getvalue(), err.getvalue()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def copy(self, source, edit):
        value = json.loads(Path(source).read_text())
        edit(value)
        path = self.tmp / Path(source).name
        path.write_text(json.dumps(value))
        return path

    def test_public_testnet_records_restore_with_environment(self):
        env = self.tmp / 'restored.env'
        code, out, err = run(['restore', *arguments(), '--env-out', str(env)])
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual(summary['chain_identifier'], '4c78adac')
        self.assertEqual(summary['site_id'], json.loads(PUBLIC['publication_receipt'].read_text())['site_id'])
        self.assertEqual(summary['current_release']['resources'], 122)
        self.assertEqual(len(summary['earlier_releases']), 5)
        lines = dict(line.split('=', 1) for line in env.read_text().splitlines())
        self.assertEqual(json.loads(json.loads(lines['NEXT_PUBLIC_FLEET_DATA']))['fleetId'], summary['fleet_id'])
        self.assertEqual(set(lines), {'NEXT_PUBLIC_DEPLOYMENT_MANIFEST', 'NEXT_PUBLIC_FLEET_DATA', 'NEXT_PUBLIC_SEAL_KEY_SERVERS',
                                      'NEXT_PUBLIC_SEAL_THRESHOLD', 'NEXT_PUBLIC_FLEET_STORAGE_PACKAGE',
                                      'NEXT_PUBLIC_FLEET_STORAGE_ORIGIN', 'NEXT_PUBLIC_FLEET_RETENTION_ORIGIN'})
        code, _, err = run(['restore', *arguments(), '--env-out', str(env)])
        self.assertEqual(code, 1)
        self.assertIn('exists', err)

    def test_changed_or_missing_bindings_are_refused(self):
        cases = {
            'foreign fleet': dict(fleet_data=self.copy(PUBLIC['fleet_data'], lambda v: v.update(fleetId='0x99'))),
            'weaker threshold': dict(seal_settings=self.copy(PUBLIC['seal_settings'], lambda v: v.update(threshold=1))),
            'other storage package': dict(fleet_storage=self.copy(PUBLIC['fleet_storage'], lambda v: v.update(packageId='0x99'))),
            'other manifest': dict(manifest=self.copy(PUBLIC['manifest'], lambda v: v.update(package_version=str(int(v['package_version']) + 1)))),
            'missing fleet binding': dict(fleet_data=None),
            'unpublished build': dict(publication_receipt=self.copy(PUBLIC['publication_receipt'], lambda v: v.update(build_receipt_sha256='0' * 64))),
            'unverified phase': dict(publication_receipt=self.copy(PUBLIC['publication_receipt'], lambda v: v['phases'][-1].update(fullWalrusReadback=False))),
            'foreign site phase': dict(publication_receipt=self.copy(PUBLIC['publication_receipt'], lambda v: v['phases'][0].update(siteId='0x99'))),
        }
        for name, paths in cases.items():
            with self.subTest(name):
                code, _, err = run(['restore', *arguments(**paths)])
                self.assertEqual(code, 1, name)
                self.assertTrue(err.startswith('refused:'), err)

    def test_private_inputs_are_refused(self):
        leaked = self.copy(PUBLIC['seal_settings'], lambda v: v.update(note='suiprivkey1qexample'))
        code, _, err = run(['restore', *arguments(seal_settings=leaked)])
        self.assertEqual(code, 1)
        self.assertIn('looks private', err)

    def test_retained_export_is_verified_byte_for_byte(self):
        manifest = json.loads(PUBLIC['manifest'].read_text())
        export = self.tmp / 'export'
        export.mkdir()
        (export / 'index.html').write_bytes(b'<html>restored</html>')
        (export / 'deployment-manifest.json').write_bytes(canonical(manifest))
        receipt = json.loads(PUBLIC['build_receipt'].read_text())
        receipt['files'] = fleet_recovery.inventory(export)
        receipt.pop('fleet_data_sha256'); receipt.pop('seal_settings_sha256'); receipt.pop('fleet_storage_sha256')
        receipt_path = self.tmp / 'build.json'
        receipt_path.write_bytes(canonical(receipt))
        publication = json.loads(PUBLIC['publication_receipt'].read_text())
        publication['build_receipt_sha256'] = fleet_recovery.digest(receipt_path.read_bytes())
        publication['phases'][-1]['files'] = receipt['files']
        publication_path = self.tmp / 'publication.json'
        publication_path.write_text(json.dumps(publication))
        paths = dict(build_receipt=receipt_path, publication_receipt=publication_path, fleet_data=None, seal_settings=None, fleet_storage=None)
        code, out, err = run(['restore', *arguments(**paths), '--export', str(export)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['verified_export_resources'], 2)
        (export / 'index.html').write_bytes(b'<html>bad release</html>')
        code, _, err = run(['restore', *arguments(**paths), '--export', str(export)])
        self.assertEqual(code, 1)
        self.assertIn('differs from the build receipt', err)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        self.publication = json.loads(PUBLIC['publication_receipt'].read_text())
        self.current = json.loads(PUBLIC['build_receipt'].read_text())

    def tearDown(self):
        self.directory.cleanup()

    def retained(self, files, **changes):
        # One file per receipt: cases are built before any of them runs.
        path = self.tmp / f'retained-{len(list(self.tmp.iterdir()))}.json'
        path.write_text(json.dumps(self.current | {'files': files} | changes))
        return path

    def rollback(self, retained, current=PUBLIC['build_receipt']):
        return run(['rollback', '--build-receipt', str(current), '--retained-build-receipt', str(retained),
                    '--publication-receipt', str(PUBLIC['publication_receipt'])])

    def test_rollback_only_to_a_verified_earlier_release_of_the_same_deployment(self):
        code, out, err = self.rollback(self.retained(self.publication['phases'][1]['files']))
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan['rollback_to']['transaction'], self.publication['phases'][1]['digest'])
        self.assertIn('--operation rollback', plan['next'])
        for name, retained in {
            'never published': self.retained({'/index.html': {'sha256': '0' * 64, 'bytes': 1}}),
            'current release': self.retained(self.current['files']),
            'other deployment': self.retained(self.publication['phases'][1]['files'], deployment_manifest_sha256='0' * 64),
            'other storage bindings': self.retained(self.publication['phases'][1]['files'], fleet_storage_sha256='0' * 64),
            'storage binding removed': self.retained(self.publication['phases'][1]['files'], fleet_storage_sha256=None),
            'other call package': self.retained(self.publication['phases'][1]['files'], call_package_id='0x99'),
        }.items():
            with self.subTest(name):
                code, _, err = self.rollback(retained)
                self.assertEqual(code, 1, name)
                self.assertTrue(err.startswith('refused:'))

    def test_current_receipt_must_be_the_published_latest_release(self):
        retained = self.retained(self.publication['phases'][1]['files'])
        unpublished = self.tmp / 'unpublished-current.json'
        unpublished.write_text(json.dumps(self.current | {'files': {'/index.html': {'sha256': '1' * 64, 'bytes': 1}}}))
        earlier = self.tmp / 'earlier-current.json'
        earlier.write_text(json.dumps(self.current | {'files': self.publication['phases'][2]['files']}))
        for name, current in {'unpublished current file map': unpublished, 'earlier phase presented as current': earlier}.items():
            with self.subTest(name):
                code, _, err = self.rollback(retained, current)
                self.assertEqual(code, 1, name)
                self.assertTrue(err.startswith('refused:'), err)


if __name__ == '__main__':
    unittest.main()
