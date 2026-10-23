#!/usr/bin/env python3
"""Run independent native archive/restart drills; not one shared-chain fleet."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--shim', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='sf090-daemon-') as temp:
        def drill(index):
            output = Path(temp) / f'{index}.log'
            began = time.monotonic()
            with output.open('w') as log:
                result = subprocess.run(['python3', 'scripts/test_fleet_data_ota.py', '--binary', str(args.binary.resolve()), '--shim', str(args.shim.resolve())], stdout=log, stderr=log)
            # Logs are private temporary files. Never copy plaintext into evidence.
            if result.returncode:
                raise RuntimeError(f'Native drill {index} failed with exit {result.returncode}; no success receipt written')
            return {'drill': index, 'status': 'PASS', 'elapsed_seconds': time.monotonic() - began}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(drill, range(4)))
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    receipt = {'schema': 1, 'result': 'PASS', 'mode': 'four-independent-daemon-simulator-drills',
               'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'binary_sha256': digest(args.binary), 'shim_sha256': digest(args.shim),
               'elapsed_seconds': time.monotonic() - start, 'concurrency': 2, 'drills': results,
               'checks': ['real signatures and native Seal ciphertext', 'browser signature/archive verification',
                          'storage exhaustion retains pending purchase', 'restart during certification and pruned history',
                          'no duplicate registration from repeated polling', 'cadence, revocation, diagnostics and crash archives'],
               'limits': ['four isolated simulators reuse test identity; not four devices on a shared chain',
                          'test codec/storage/certification responses; no real provider availability',
                          'borrowed binary explicitly hashed; no claim a fresh Rust build ran',
                          'short restart drills, not multi-day soak or hardware recovery']}
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    print('PASS four independent native archive/restart drills')


if __name__ == '__main__':
    main()
