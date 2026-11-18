"""Exercise Docker's actual release-download flags against a local fault server."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import unittest


class ReleaseDownloads(unittest.TestCase):
    def test_bounded_retries_and_integrity(self):
        dockerfile = (Path(__file__).resolve().parents[2] / 'Dockerfile').read_text()
        commands = [shlex.split(line.removeprefix('RUN ').rstrip(' \\'))
                    for line in dockerfile.splitlines()
                    if line.startswith('RUN curl -fL')]
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertEqual(command[command.index('--retry') + 1], '3')
            self.assertEqual(command[command.index('--connect-timeout') + 1], '30')
            self.assertEqual(command[command.index('--max-time') + 1], '600')
        counts = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts[self.path] = counts.get(self.path, 0) + 1
                status = (504 if self.path == '/always' or
                          (self.path == '/recover' and counts[self.path] == 1)
                          else 404 if self.path == '/missing' else 200)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'archive' if status == 200 else b'error')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'download'
                for command in commands:
                    command = command[:command.index('-o')] + ['-o', str(output)]
                    for path, success, attempts in [('/recover', True, 2),
                                                    ('/always', False, 4),
                                                    ('/missing', False, 1)]:
                        counts.clear()
                        result = subprocess.run(command + [f'http://127.0.0.1:{server.server_port}{path}'],
                                                capture_output=True, timeout=30)
                        self.assertEqual(result.returncode == 0, success, result.stderr)
                        self.assertEqual(counts[path], attempts)
                        if success:
                            self.assertEqual(output.read_bytes(), b'archive')
                            for digest, expected in [(hashlib.sha256(b'archive').hexdigest(), 0),
                                                     ('0' * 64, 1)]:
                                check = subprocess.run(['sha256sum', '-c', '-'],
                                                       input=f'{digest}  {output}\n', text=True,
                                                       capture_output=True)
                                self.assertEqual(check.returncode, expected)
                for archive, digest in [('sui', 'SUI_SHA256'), ('sui-network', 'SUI_NETWORK_SHA256')]:
                    self.assertIn(f'echo "${{{digest}}}  /tmp/{archive}.tgz" | sha256sum -c - \\\n    &&', dockerfile)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
