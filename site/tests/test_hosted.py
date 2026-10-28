import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SITE))
import prepare_hosted as hosted
import release
import collection


class HostedTests(unittest.TestCase):
    def test_three_origin_preview_fixture_and_restore(self):
        # Offline fixture only: no provider publication/rollback evidence.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in ['a', 'b']:
                bundle = root / version
                for name, host in [('marketing', 'suifleet.io'), ('docs', 'docs.suifleet.io'), ('console', 'app.suifleet.io')]:
                    src = root / (version + name)
                    src.mkdir()
                    for file in ['index.html', 'robots.txt', 'sitemap.xml']:
                        (src / file).write_text(version)
                    (src / '_headers').write_bytes((SITE / 'src/_headers').read_bytes())
                    if name == 'console':
                        raw = (SITE.parent / 'profiles/seal-testnet/deployment.json').read_bytes()
                        (src / 'deployment-manifest.json').write_bytes(raw)
                        (src / 'console-release.json').write_text(json.dumps({'revision': version * 40, 'manifest_sha256': release.digest(raw)}))
                    release.seal(src, bundle / name, version * 40, host)
                record = {name: release.digest((bundle / name / 'release.json').read_bytes()) for name in ['marketing', 'docs', 'console']}
                (bundle / 'collection.json').write_text(json.dumps(record))
            live = root / 'fixture'
            for version in ['a', 'b', 'a']:
                if live.exists(): shutil.rmtree(live)
                shutil.copytree(root / version, live)
                collection.check(live, release.digest((root / version / 'collection.json').read_bytes()))
            (live / 'console/public/index.html').write_text('tamper')
            with self.assertRaises(ValueError):
                collection.check(live, release.digest((live / 'collection.json').read_bytes()))

    def test_script_hashes_and_remote_script_rejection(self):
        p = hosted.Scripts()
        p.feed('<script>self.test=1</script>')
        self.assertEqual(len(p.hashes), 1)
        with self.assertRaises(ValueError):
            p.feed('<script src="https://example.com/tracker.js"></script>')
