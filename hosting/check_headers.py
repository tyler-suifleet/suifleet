#!/usr/bin/env python3
"""Read-only post-publication gate. _headers files and meta CSP cannot pass it."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


def failures(headers, download=False):
    errors = []
    if download:
        if not headers.get('Content-Type', '').lower().startswith('text/plain'):
            errors.append('DNS download Content-Type must be text/plain')
        if 'attachment' not in headers.get('Content-Disposition', '').lower():
            errors.append('DNS download must be an attachment')
        if 'noindex' not in headers.get('X-Robots-Tag', '').lower():
            errors.append('DNS download must have X-Robots-Tag: noindex')
        return errors
    policy = headers.get('Content-Security-Policy', '')
    directives = {p[0].lower(): p[1:] for clause in policy.split(';') if (p := clause.split())}
    for name, expected in [('default-src', ["'none'"]), ('frame-ancestors', ["'none'"]), ('base-uri', ["'none'"])]:
        if directives.get(name) != expected:
            errors.append('response CSP requires ' + name + ' ' + ' '.join(expected))
    if "'unsafe-eval'" in directives.get('script-src', []) or '*' in directives.get('script-src', []):
        errors.append('response CSP permits unrestricted script execution')
    hsts = headers.get('Strict-Transport-Security', '').lower()
    try:
        age = next(int(p.strip().split('=', 1)[1]) for p in hsts.split(';') if p.strip().startswith('max-age='))
        if age < 31536000:
            raise ValueError()
    except (StopIteration, ValueError):
        errors.append('HSTS max-age must be at least 31536000')
    if headers.get('X-Content-Type-Options', '').lower() != 'nosniff':
        errors.append('missing nosniff response header')
    if headers.get('Referrer-Policy', '').lower() != 'strict-origin-when-cross-origin':
        errors.append('missing strict-origin-when-cross-origin response header')
    return errors


def check(origins):
    errors = []
    targets = [(origins[k], False) for k in ['origin', 'app_url', 'docs_url']]
    targets.append((origins['origin'].rstrip('/') + '/setup/suifleet-dns.zone', True))
    for url, download in targets:
        try:
            with urlopen(Request(url, headers={'Cache-Control': 'no-cache'}), timeout=30) as response:
                if response.status != 200 or not response.url.startswith('https://'):
                    errors.append(url + ': requires HTTPS 200')
                errors.extend(url + ': ' + error for error in failures(response.headers, download))
        except Exception as error:
            errors.append(url + ': fetch failed: ' + str(error))
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('origins', type=Path)
    args = parser.parse_args()
    errors = check(json.loads(args.origins.read_text()))
    for error in errors:
        print('FAIL ' + error)
    if not errors:
        print('PASS hosted response security headers and DNS download metadata')
    raise SystemExit(bool(errors))
