#!/usr/bin/env python3
"""Reject packaged-client drift against every committed evaluation profile, offline."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError(f'expected a release version: {value!r}')
    return tuple(map(int, value.split('.')))


def compatible(packaged, bounds):
    low, high = version(bounds['min']), version(bounds['max_exclusive'])
    if not low <= version(packaged) < high:
        raise ValueError(f'packaged client {packaged} outside [{bounds["min"]}, {bounds["max_exclusive"]})')


def check(root=ROOT):
    recipe = (root/'suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb').read_text()
    packaged = re.search(r'^SUIFLEET_CRATE_VERSION = "([^"]+)"$', recipe, re.M)
    if not packaged:
        raise ValueError('recipe must pin SUIFLEET_CRATE_VERSION')
    profiles = sorted((root/'profiles').glob('*/deployment.json'))
    if not profiles:
        raise ValueError('no evaluation deployment profiles found')
    for path in profiles:
        try:
            compatible(packaged.group(1), json.loads(path.read_text())['supported_clients']['edge'])
        except (KeyError, ValueError) as error:
            raise ValueError(f'{path.relative_to(root)}: {error}') from error
    return packaged.group(1), [str(p.relative_to(root)) for p in profiles]


if __name__ == '__main__':
    packaged, profiles = check()
    print(f'Yocto client {packaged} supports: {", ".join(profiles)}')
