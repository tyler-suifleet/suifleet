import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'export_yocto_layer.py'
spec = importlib.util.spec_from_file_location('layer_export', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LayerExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.layer = self.repo / module.PREFIX
        self.layer.mkdir(parents=True)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Export fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.layer / 'README.md').write_text('reviewed bytes\n')
        (self.layer / 'export-files.json').write_text(json.dumps(['README.md', 'export-files.json']))
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args])

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def test_reproducible_and_excludes_working_state(self):
        (self.layer / 'device.key').write_text('private fixture, must be excluded')
        (self.layer / 'README.md').write_text('unreviewed modification')
        first, second = self.repo/'one.gz', self.repo/'two.gz'
        a = module.export(self.repo, 'HEAD', first)
        b = module.export(self.repo, 'HEAD', second)
        self.assertEqual(a, b)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            self.assertEqual(archive.extractfile('meta-suifleet/README.md').read(), b'reviewed bytes\n')
            self.assertFalse(any('device.key' in n for n in archive.getnames()))
            self.assertTrue(all(m.uid == m.gid == m.mtime == 0 for m in archive))
            provenance = json.load(archive.extractfile('meta-suifleet/export-provenance.json'))
            self.assertEqual(provenance['source_commit'], a['source_commit'])
        with self.assertRaises(FileExistsError):
            module.export(self.repo, 'HEAD', first)

    def test_rejects_symlink_and_unsafe_paths(self):
        for names in [['../secret'], ['/secret'], ['README.md', 'README.md'], ['link']]:
            (self.layer/'export-files.json').write_text(json.dumps(names))
            link = self.layer/'link'
            if not link.is_symlink():
                link.symlink_to('/etc/passwd')
            self.commit()
            with self.assertRaises(ValueError):
                module.export(self.repo, 'HEAD', self.repo/'bad.gz')

    def test_catalog_covers_software_layer_only(self):
        layer = SCRIPT.parents[1] / module.PREFIX
        names = json.loads((layer/'export-files.json').read_text())
        self.assertEqual(set(names), {str(p.relative_to(layer)) for p in layer.rglob('*') if p.is_file()})
        self.assertFalse(any(n.endswith('.bbappend') or n.startswith('recipes-core/') for n in names))


if __name__ == '__main__':
    unittest.main()
