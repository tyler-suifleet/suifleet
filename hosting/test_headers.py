import importlib.util
from email.message import Message
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('check_headers', Path(__file__).with_name('check_headers.py'))
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class HeadersTest(unittest.TestCase):
    def headers(self, **values):
        result = Message()
        for key, value in values.items():
            result[key.replace('_', '-')] = value
        return result

    def test_fetch_uses_explicit_user_agent_and_checks_success_headers(self):
        response = MagicMock()
        response.status = 200
        response.url = 'https://example.com/'
        response.headers = self.headers(Content_Type='text/html')
        response.__enter__.return_value = response
        with patch.object(check, 'urlopen', return_value=response) as fetch:
            errors = check.check(dict.fromkeys(['origin', 'app_url', 'docs_url'], response.url))
        self.assertEqual(fetch.call_args.args[0].get_header('User-agent'), 'suifleet-header-check/1')
        self.assertTrue(any('frame-ancestors' in e for e in errors))
        self.assertFalse(any('fetch/access failure' in e for e in errors))

    def test_http_403_is_access_failure_without_header_assertions(self):
        with patch.object(check, 'urlopen', side_effect=HTTPError('https://example.com', 403, 'Forbidden', {}, None)):
            errors = check.check(dict.fromkeys(['origin', 'app_url', 'docs_url'], 'https://example.com'))
        self.assertEqual(len(errors), 4)
        self.assertTrue(all('fetch/access failure: HTTP 403' in e for e in errors))
        self.assertFalse(any('CSP' in e for e in errors))

    def test_provider_without_headers_fails_closed(self):
        errors = check.failures(self.headers(Content_Type='text/html'))
        self.assertEqual(len(errors), 6)
        self.assertTrue(any('frame-ancestors' in e for e in errors))

    def test_real_headers_and_download_metadata(self):
        headers = self.headers(
            Content_Security_Policy="default-src 'none'; base-uri 'none'; frame-ancestors 'none'; script-src 'self'",
            Strict_Transport_Security='max-age=31536000; includeSubDomains',
            X_Content_Type_Options='nosniff',
            Referrer_Policy='strict-origin-when-cross-origin')
        self.assertEqual(check.failures(headers), [])
        headers.replace_header('Content-Security-Policy', "default-src 'none'; base-uri 'none'; script-src * 'unsafe-eval'")
        self.assertEqual(len(check.failures(headers)), 2)
        self.assertEqual(len(check.failures(self.headers(Content_Type='application/octet-stream'), True)), 3)
        self.assertEqual(check.failures(self.headers(Content_Type='text/plain; charset=utf-8', Content_Disposition='attachment; filename="suifleet-dns.zone"', X_Robots_Tag='noindex'), True), [])


if __name__ == '__main__':
    unittest.main()
