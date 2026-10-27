import importlib.util
from pathlib import Path
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', SITE / 'release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in ['index.html', 'robots.txt', 'sitemap.xml']:
            (self.source / name).write_text('public content')
        (self.source / '_headers').write_bytes((SITE / 'src/_headers').read_bytes())
        self.out = self.root / 'release'

    def seal(self, hostname='suifleet.io'):
        return release.seal(self.source, self.out, 'a' * 40, hostname)

    def test_reproducible_and_preserves_exact_files(self):
        checksum = self.seal()
        self.assertEqual(release.verify(self.out, checksum)['files'], release.inventory(self.source))
        self.assertEqual(checksum, release.seal(self.source, self.root / 'again', 'a' * 40, 'suifleet.io'))
        with self.assertRaises(ValueError):
            release.verify(self.out, '0' * 64)

    def test_modified_missing_extra_files_rejected(self):
        checksum = self.seal()
        page = self.out / 'public/index.html'
        for action in ['modified', 'missing', 'extra']:
            with self.subTest(action=action):
                page.write_text('public content')
                if action == 'modified':
                    page.write_text('tampered')
                elif action == 'missing':
                    page.unlink()
                else:
                    (self.out / 'public/extra').write_text('extra')
                with self.assertRaises(ValueError):
                    release.verify(self.out, checksum)

    def test_no_workers_private_files_or_symlinks(self):
        for name in ['device.key', '_worker.js', '.env', 'ws-resources.json', 'source.map']:
            with self.subTest(name=name):
                path = self.source / name
                path.write_text('forbidden')
                with self.assertRaises(ValueError):
                    self.seal()
                path.unlink()
        (self.source / 'linked').symlink_to(self.source / 'index.html')
        with self.assertRaises(ValueError):
            self.seal()

    def test_console_manifest_and_header_gate(self):
        with self.assertRaises(ValueError):
            self.seal('app.suifleet.io')
        (self.source / 'deployment-manifest.json').write_text('{}')
        self.seal('app.suifleet.io')
        (self.source / '_headers').write_text('/*')
        with self.assertRaises(ValueError):
            release.inventory(self.source)

    def test_refuses_overwrite_and_nested_output(self):
        self.seal()
        with self.assertRaises(ValueError):
            self.seal()
        with self.assertRaises(ValueError):
            release.seal(self.source, self.source / 'nested', 'a' * 40, 'suifleet.io')


if __name__ == '__main__':
    unittest.main()
