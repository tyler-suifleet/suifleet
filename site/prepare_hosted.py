#!/usr/bin/env python3
"""Prepare conventional-host copies; never change the Walrus distribution."""
import argparse
import base64
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
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


ROOT = Path(__file__).resolve().parents[1]


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('hosted console requires credential-free HTTPS endpoints')
    # Keep explicit ports (the manifest uses :443).
    return 'https://' + parsed.netloc


def console_endpoints(manifest, profile=ROOT / 'profiles/seal-testnet'):
    """Read reviewed profile and application constants, without broad wildcard grants."""
    expected = json.loads((profile / 'deployment.json').read_text())
    if manifest != expected:
        raise ValueError('console manifest must match the reviewed hosting profile')
    connect = {origin(url) for url in [manifest['rpc_url'], *manifest.get('rpc_urls', [])]}
    walrus = {origin(manifest['walrus'][key]) for key in ['publisher_url', 'aggregator_url']}
    # These files own the built-in fallback and quote endpoints. Read every URL
    # literal so adding another fallback cannot silently leave the CSP behind.
    for name in ['walrus.ts', 'cost-quote.ts']:
        urls = re.findall(r'"(https://[^"\s]+)"', (ROOT / 'dapp/src/lib' / name).read_text())
        if not urls:
            raise ValueError('missing application endpoint constants: ' + name)
        connect.update(origin(url) for url in urls)
    seal = json.loads((profile / 'seal.json').read_text())
    for server in seal['keyServers']:
        # URLs are read-only on-chain observations pinned beside the object IDs.
        connect.add(origin(seal['keyServerUrls'][server['objectId']]))
    connect.update(walrus)
    # Blob images are read through the configured aggregator, never arbitrary hosts.
    return sorted(connect), sorted(walrus)


def prepare(source, out, kind, revision):
    if out.exists() or out.resolve().is_relative_to(source.resolve()):
        raise ValueError('output must be new and outside input')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('symlinks forbidden')
    if kind == 'console':
        manifest = json.loads((source / 'deployment-manifest.json').read_text())
        validate(manifest)
        connect, images = console_endpoints(manifest)
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
        # No external scripts; network grants follow the reviewed profile/constants.
        policy = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval' " + ' '.join(sorted(hashes)) + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: " + ' '.join(images) + "; font-src 'self'; connect-src 'self' " + ' '.join(connect) + "; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        # Meta CSP is enforced even on hosts that ignore conventional _headers.
        # frame-ancestors needs a real response header; never imply otherwise.
        meta_policy = policy.replace("; frame-ancestors 'none'", "")
        for page in sorted(out.rglob('*.html')):
            document = page.read_text()
            meta = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(meta_policy, quote=True) + '"><meta name="referrer" content="strict-origin-when-cross-origin">'
            match = re.search(r'<head(?:\s[^>]*)?>', document, re.IGNORECASE)
            if not match:
                raise ValueError('console HTML must have a head: ' + str(page))
            page.write_text(document[:match.end()] + meta + document[match.end():])
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
