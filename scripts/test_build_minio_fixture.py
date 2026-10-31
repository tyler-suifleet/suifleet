"""Exercise bounded public-dependency retry policy without network or Docker."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('build_minio_fixture.sh')


class FetchRetryTests(unittest.TestCase):
    def run_case(self, diagnostic, failures):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'go').write_text('''#!/bin/bash
n=$(cat "$STATE" 2>/dev/null || echo 0)
n=$((n+1)); echo "$n" > "$STATE"
if (( n <= FAILURES )); then echo "$DIAGNOSTIC" >&2; exit 7; fi
''')
            (root / 'sleep').write_text('#!/bin/bash\nexit 0\n')
            for name in ('go', 'sleep'):
                (root / name).chmod(0o755)
            env = dict(os.environ, PATH=tmp + ':' + os.environ['PATH'],
                       STATE=str(root / 'count'), FAILURES=str(failures), DIAGNOSTIC=diagnostic)
            result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)
            return result, int((root / 'count').read_text())

    def test_exact_ci_eof_recovers(self):
        result, calls = self.run_case('helpers.go:13:2: read "https://proxy.golang.org/github.com/apache/thrift/@v/v0.21.0.zip": unexpected EOF', 1)
        self.assertEqual((result.returncode, calls), (0, 2))

    def test_success_and_other_transient_fetches(self):
        result, calls = self.run_case('', 0)
        self.assertEqual((result.returncode, calls), (0, 1))
        for failure in ('i/o timeout', 'TLS handshake timeout',
                        'connection reset by peer', '502 Bad Gateway', '503 Service Unavailable'):
            with self.subTest(failure=failure):
                result, calls = self.run_case('https://proxy.golang.org/pkg: ' + failure, 2)
                self.assertEqual((result.returncode, calls), (0, 3))

    def test_exhausted_fetch_stays_failed(self):
        result, calls = self.run_case('read "https://proxy.golang.org/pkg": unexpected EOF', 10)
        self.assertEqual((result.returncode, calls), (7, 3))

    def test_compiler_and_checksum_fail_fast(self):
        for diagnostic in ('# package\nfile.go:1: undefined: name',
                           'checksum mismatch\nhttps://proxy.golang.org/pkg: unexpected EOF',
                           '# package\nhttps://proxy.golang.org/pkg: unexpected EOF',
                           'unknown revision abc'):
            with self.subTest(diagnostic=diagnostic):
                result, calls = self.run_case(diagnostic, 10)
                self.assertEqual((result.returncode, calls), (7, 1))


if __name__ == '__main__':
    unittest.main()
