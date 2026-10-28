#!/usr/bin/env python3
"""Bind all three origin release digests to a single approval digest."""
import argparse
import json
from pathlib import Path
from release import digest, verify


def check(root, expected):
    raw = (root / 'collection.json').read_bytes()
    if digest(raw) != expected:
        raise ValueError('collection approval mismatch')
    records = json.loads(raw)
    if set(records) != {'marketing', 'docs', 'console'}:
        raise ValueError('all three origins required')
    revisions = set()
    for name, hostname in [('marketing', 'suifleet.io'), ('docs', 'docs.suifleet.io'), ('console', 'app.suifleet.io')]:
        record = verify(root / name, records[name])
        revisions.add(record['revision'])
        if record['hostname'] != hostname:
            raise ValueError('origin mismatch')
    if len(revisions) != 1:
        raise ValueError('origin source revisions differ')
    return records


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('--sha256')
    a = p.parse_args()
    if a.sha256:
        check(a.root, a.sha256)
    else:
        records = {name: digest((a.root / name / 'release.json').read_bytes()) for name in ['marketing', 'docs', 'console']}
        raw = (json.dumps(records, sort_keys=True) + '\n').encode()
        (a.root / 'collection.json').write_bytes(raw)
        check(a.root, digest(raw))
        print(digest(raw))
