"""Exercise the actual fixture wrapper without network or a Go compiler."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'build_minio_fixture.sh'
EOF_CODE = ('pkg/mod/github.com/minio/mc@v0.0.0-20250313080218-cf909e1063a9/'
            'pkg/limiter/limiter.go:26:2: github.com/juju/ratelimit@v1.0.2: verifying module: '
            'github.com/juju/ratelimit@v1.0.2: reading '
            'https://sum.golang.org/tile/8/1/174: unexpected EOF')


class FixtureBuildTests(unittest.TestCase):
    def run_fixture(self, diagnostic, failures):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'go').write_text('''#!/usr/bin/env python3
import os, pathlib, sys
p = pathlib.Path(os.environ['COUNT'])
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
assert sys.argv[1:] == ['install', 'github.com/minio/minio@9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a']
if n <= int(os.environ['FAILURES']):
    print(os.environ['DIAGNOSTIC'], file=sys.stderr)
    sys.exit(7)
''')
            (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
            for name in ('go', 'sleep'):
                (root / name).chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                       COUNT=str(root / 'count'), FAILURES=str(failures), DIAGNOSTIC=diagnostic)
            result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)
            return result, int((root / 'count').read_text())

    def test_truncated_checksum_response_recovers(self):
        result, attempts = self.run_fixture(EOF_CODE, 2)
        self.assertEqual((result.returncode, attempts), (0, 3))
        self.assertIn(EOF_CODE, result.stderr)

    def test_truncated_checksum_response_is_bounded(self):
        result, attempts = self.run_fixture(EOF_CODE, 10)
        self.assertEqual((result.returncode, attempts), (7, 3))

    def test_other_failures_are_immediate(self):
        for diagnostic in ('compile error', 'checksum mismatch', 'SECURITY ERROR\n' + EOF_CODE,
                           'unrelated unexpected EOF', EOF_CODE + '\nchecksum mismatch'):
            with self.subTest(diagnostic=diagnostic):
                result, attempts = self.run_fixture(diagnostic, 2)
                self.assertEqual((result.returncode, attempts), (7, 1))

    def test_success_runs_once(self):
        result, attempts = self.run_fixture('', 0)
        self.assertEqual((result.returncode, attempts), (0, 1))
