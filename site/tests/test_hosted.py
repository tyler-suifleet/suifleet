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

    def test_console_csp_covers_profile_and_application_endpoints(self):
        manifest = json.loads((SITE.parent / 'profiles/seal-testnet/deployment.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / 'source', Path(tmp) / 'hosted'
            src.mkdir()
            (src / 'deployment-manifest.json').write_text(json.dumps(manifest))
            (src / 'index.html').write_text('<html><head><script>self.test=1</script></head><body></body></html>')
            hosted.prepare(src, out, 'console', 'a' * 40)
            policy = (out / '_headers').read_text().split('Content-Security-Policy: ')[1].split('\n')[0]
            from html.parser import HTMLParser
            class Metas(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    if tag == 'meta':
                        self.tags.append(dict(attrs))
            parsed = Metas()
            parsed.tags = []
            document = (out / 'index.html').read_text()
            parsed.feed(document)
            meta = next(t['content'] for t in parsed.tags if t.get('http-equiv') == 'Content-Security-Policy')
            self.assertEqual(meta, policy.replace("; frame-ancestors 'none'", ""))
            self.assertLess(document.index('<meta'), document.index('<script'))
            self.assertLess(document.index('<meta charset="utf-8">'), 1024)
            self.assertIn({'name': 'referrer', 'content': 'strict-origin-when-cross-origin'}, parsed.tags)
            parser = hosted.Scripts()
            parser.feed(document)
            self.assertTrue(all(h in meta for h in parser.hashes))
            directives = {parts[0]: set(parts[1:]) for clause in policy.split(';') if (parts := clause.split())}
            required = {
                'https://fullnode.testnet.sui.io:443',
                'https://publisher.walrus-testnet.walrus.space',
                'https://aggregator.walrus-testnet.walrus.space',
                'https://publisher.walrus-testnet.h2o-nodes.com',
                'https://walrus-publisher-testnet.n1stake.com',
                'https://api.coingecko.com',
                'https://seal-key-server-testnet-1.mystenlabs.com',
                'https://seal-key-server-testnet-2.mystenlabs.com',
            }
            self.assertTrue(required <= directives['connect-src'])
            self.assertIn(manifest['walrus']['aggregator_url'], directives['img-src'])
            self.assertNotIn('https:', directives['connect-src'])
            self.assertFalse(any('*' in source for source in directives['connect-src']))
            self.assertNotIn('https://api.coingecko.com', directives['script-src'])

    def test_endpoint_derivation_tracks_constants_and_rejects_profile_drift(self):
        from unittest.mock import patch
        manifest = json.loads((SITE.parent / 'profiles/seal-testnet/deployment.json').read_text())
        read_text = Path.read_text
        def updated(path, *args, **kwargs):
            text = read_text(path, *args, **kwargs)
            if path.name == 'walrus.ts':
                text += '\nconst newFallback = "https://new-publisher.example/upload";'
            return text
        with patch.object(Path, 'read_text', updated):
            connect, _ = hosted.console_endpoints(manifest)
            self.assertIn('https://new-publisher.example', connect)
        manifest['rpc_url'] = 'https://different.example'
        with self.assertRaisesRegex(ValueError, 'reviewed hosting profile'):
            hosted.console_endpoints(manifest)
        for url in ['http://unsafe.example', 'https://user:secret@example.com']:
            with self.assertRaises(ValueError):
                hosted.origin(url)

    def test_missing_seal_url_fails_closed(self):
        manifest = json.loads((SITE.parent / 'profiles/seal-testnet/deployment.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / 'deployment.json').write_text(json.dumps(manifest))
            (profile / 'seal.json').write_text(json.dumps({'keyServers': [{'objectId': '0x1'}], 'keyServerUrls': {}}))
            with self.assertRaises(KeyError):
                hosted.console_endpoints(manifest, profile)
