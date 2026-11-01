"""Regression tests for authenticated proof-tool archive downloads."""
import hashlib
import io
from email.message import Message
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import ContentTooShortError

spec = importlib.util.spec_from_file_location('build_move_prover', Path(__file__).resolve().parents[1] / 'build_move_prover.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ArchiveDownloadTests(unittest.TestCase):
    def download(self, path):
        mod.verified_download(path, 'https://example.test/sdk?existing=1', hashlib.sha512(b'complete').hexdigest(), 'sha512')

    def test_truncated_transfer_is_retried_and_only_verified_bytes_published(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sdk'
            calls = []
            def retrieve(url, temporary):
                self.assertFalse(path.exists())
                calls.append(url)
                Path(temporary).write_bytes(b'partial' if len(calls) == 1 else b'complete')
                if len(calls) == 1:
                    raise ContentTooShortError('retrieval incomplete', None)
            with patch.object(mod, 'retrieve_archive', side_effect=retrieve):
                self.download(path)
            self.assertEqual(path.read_bytes(), b'complete')
            self.assertIn('existing=1&suifleet_download=', calls[1])
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_stale_partial_cache_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sdk'
            path.write_bytes(b'partial')
            with patch.object(mod, 'retrieve_archive', side_effect=lambda url, target: Path(target).write_bytes(b'complete')) as fetch:
                self.download(path)
                self.download(path)
            self.assertEqual(fetch.call_count, 1)

    def test_bad_digest_is_bounded_and_never_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            with patch.object(mod, 'retrieve_archive', side_effect=lambda url, target: Path(target).write_bytes(b'corrupt')) as fetch:
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    mod.archive(tools, 'sdk', 'https://example.test/sdk', 'wrong', 'sha512', tools / 'extracted')
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(list(tools.iterdir()), [])

    def test_network_failure_is_bounded_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mod, 'retrieve_archive', side_effect=ContentTooShortError('incomplete', None)) as fetch:
                with self.assertRaises(ContentTooShortError):
                    self.download(Path(directory) / 'sdk')
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_response_length_is_checked(self):
        response = io.BytesIO(b"partial")
        response.headers = Message()
        response.headers["Content-Length"] = "100"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mod.urllib.request, "urlopen", return_value=response) as fetch:
                with self.assertRaises(ContentTooShortError):
                    mod.retrieve_archive("https://example.test/sdk", Path(directory) / "sdk")
            fetch.assert_called_once_with("https://example.test/sdk", timeout=60)
