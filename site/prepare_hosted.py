#!/usr/bin/env python3
"""Prepare conventional-host copies; never change the Walrus distribution."""
import argparse
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import sys
sys.path.append(str(Path(__file__).resolve().parents[1] / 'scripts'))
from deployment_manifest import validate


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inline = False
        self.text = ''
        self.hashes = set()
    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            src = dict(attrs).get('src')
            if src and (not src.startswith('/') or src.startswith('//')):
                raise ValueError('remote scripts require separate consent review')
            self.inline = src is None
            self.text = ''
    def handle_data(self, data):
        if self.inline:
            self.text += data
    def handle_endtag(self, tag):
        if tag == 'script' and self.inline:
            self.hashes.add("'sha256-" + base64.b64encode(hashlib.sha256(self.text.encode()).digest()).decode() + "'")
            self.inline = False


def sums(root):
    (root / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(root).as_posix() + '\n' for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'))


def prepare(source, out, kind, revision):
    if out.exists() or out.resolve().is_relative_to(source.resolve()):
        raise ValueError('output must be new and outside input')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('symlinks forbidden')
    if kind == 'console':
        manifest = json.loads((source / 'deployment-manifest.json').read_text())
        validate(manifest)
    shutil.copytree(source, out)
    if kind == 'docs':
        (out / '.suifleet-docs-output').unlink()
        with (out / '_headers').open('a') as f:
            f.write('  Strict-Transport-Security: max-age=31536000\n')
    else:
        (out / 'ws-resources.json').unlink(missing_ok=True)
        hashes = set()
        routes = []
        for page in sorted(out.rglob('*.html')):
            parser = Scripts()
            parser.feed(page.read_text())
            hashes.update(parser.hashes)
            if page.name == 'index.html':
                routes.append('/' + page.relative_to(out).as_posix().removesuffix('index.html'))
        # Explicit testnet profile endpoints. No external scripts are allowed.
        policy = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval' " + ' '.join(sorted(hashes)) + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.walrus.space; font-src 'self'; connect-src 'self' https://fullnode.testnet.sui.io:443 https://*.walrus.space https://seal-testnet.api.mystenlabs.com https://seal-testnet.api.nodeinfra.com; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        (out / '_headers').write_text('/*\n  Content-Security-Policy: ' + policy + '\n  Strict-Transport-Security: max-age=31536000\n  Referrer-Policy: strict-origin-when-cross-origin\n  X-Content-Type-Options: nosniff\n')
        (out / 'robots.txt').write_text('User-agent: *\nDisallow: /\nSitemap: https://app.suifleet.io/sitemap.xml\n')
        (out / 'sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join('<url><loc>https://app.suifleet.io' + r + '</loc></url>' for r in routes) + '</urlset>\n')
        (out / 'console-release.json').write_text(json.dumps({'schema': 1, 'revision': revision, 'manifest_sha256': hashlib.sha256((out / 'deployment-manifest.json').read_bytes()).hexdigest(), 'recording_acceptance': 'unrun; SF-031/#32 and SF-087/#159'}, sort_keys=True) + '\n')
    sums(out)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('kind', choices=['console', 'docs'])
    p.add_argument('source', type=Path)
    p.add_argument('out', type=Path)
    p.add_argument('--revision', required=True)
    a = p.parse_args()
    prepare(a.source, a.out, a.kind, a.revision)
