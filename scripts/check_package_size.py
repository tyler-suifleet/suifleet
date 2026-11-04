#!/usr/bin/env python3
"""Check release package size offline before a gas-spending publication/upgrade.

Use MovePackage::size's accounting from the pinned Sui compiler release:
module bytes/names, every datatype's origin, dependency linkage, and version.
This is the conservative 100 KiB limit used by our supported testnet profile;
the network's own check remains authoritative.
"""
import argparse
import base64
from pathlib import Path
import tempfile

from check_contract_interfaces import ROOT, summarize
from sui_toolchain import compile_package

MAX_PACKAGE_SIZE = 102400


def package_size(modules, summaries, dependencies):
    module_bytes = sum(len(base64.b64decode(module, validate=True)) for module in modules)
    module_names = sum(len(name.encode()) for name in summaries)
    origins = sum(len(module.encode()) + len(name.encode()) + 32
                  for module, summary in summaries.items()
                  for kind in ('structs', 'enums') for name in summary[kind])
    return 8 + module_bytes + module_names + origins + 72 * len(dependencies)


def check_size(size, limit=MAX_PACKAGE_SIZE):
    if size > limit:
        raise ValueError(f'Move package is {size} bytes; limit {limit} (exceeds by {size - limit})')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contracts', type=Path, default=ROOT / 'contracts')
    args = parser.parse_args()
    built = compile_package('sui', args.contracts)
    with tempfile.TemporaryDirectory(prefix='package-size-') as directory:
        summaries, _ = summarize(args.contracts, Path(directory))
    size = package_size(built['modules'], summaries, built['dependencies'])
    check_size(size)
    print(f'PASS: Move package {size}/{MAX_PACKAGE_SIZE} bytes; {MAX_PACKAGE_SIZE - size} bytes free')


if __name__ == '__main__':
    main()
