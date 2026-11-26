#!/usr/bin/env python3
"""Classify CI coverage, never review/deployment authority. Unknowns run full CI."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

# Keep the server-side workflow filters in sync with the regression test.
IGNORED_PATTERNS = (
    '!site/src/**/*.html', '!site/src/**/*.css',
    '!docs/*.md', '!docs/**/*.md', '!README.md',
)
FULL_EXCEPTIONS = (
    'docs/PR_COORDINATION.md', 'docs/ATOMIC_TICKETING.md',
    'docs/IMPLEMENTATION_ACCEPTANCE.md', 'docs/SMALL_CHANGES.md',
    'docs/TESTNET.md', 'docs/SECURITY_CI.md', 'docs/OPERATIONS.md',
)


def classify(paths):
    if not paths:
        return dict(full=True, marketing=True, docs=True)
    marketing = docs = False
    for name in paths:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or name in FULL_EXCEPTIONS:
            return dict(full=True, marketing=True, docs=True)
        if name.startswith('site/src/') and path.suffix in {'.html', '.css'}:
            marketing = True
        elif name == 'README.md' or (name.startswith('docs/') and path.suffix == '.md'):
            docs = True
        else:
            return dict(full=True, marketing=True, docs=True)
    return dict(full=False, marketing=marketing, docs=docs)


def event_scope(event, git=None):
    """Use actual git paths, including both sides of renames/deletes; no labels."""
    git = git or subprocess.check_output
    try:
        if 'pull_request' in event:
            base = event['pull_request']['base']['sha']
            head = event['pull_request']['head']['sha']
            args = ['git', 'diff', '--merge-base', '--no-renames', '--name-only', '-z', base, head]
        else:
            base, head = event['before'], event['after']
            args = ['git', 'diff', '--no-renames', '--name-only', '-z', base, head]
        if not all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{40}', v) and v != '0'*40 for v in (base, head)):
            raise ValueError('Unverified revision range')
        paths = git(args, stderr=subprocess.DEVNULL).decode('utf-8').split('\0')
        paths = [p for p in paths if p]
        if not paths or len(paths) > 300:
            raise ValueError('Unverifiable or oversized path range')
        return dict(classify(paths), verified=True)
    except (KeyError, TypeError, ValueError, UnicodeError, subprocess.SubprocessError, OSError):
        return dict(classify([]), verified=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--event', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    try:
        event = json.loads(args.event.read_text())
        scope = event_scope(event)
    except (OSError, ValueError, TypeError):
        scope = dict(classify([]), verified=False)
    with args.output.open('a') as stream:
        for key, value in scope.items():
            stream.write(f'{key}={str(value).lower()}\n')
    print(json.dumps(scope, sort_keys=True))
    if not scope["verified"]:
        raise SystemExit("Content scope unavailable: require a full CI run; never assume a cosmetic-only range.")


if __name__ == '__main__':
    main()
