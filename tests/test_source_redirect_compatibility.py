"""Repair only an observed malformed same-host upgrade bound to the sent URL."""
from contextlib import ExitStack
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from backend.scraper.acquisition import FetchRequest
from backend.scraper.acquisition.http_transport import fetch_http


class SourceRedirectCompatibilityTests(unittest.TestCase):
    original = 'http://campus.example.edu.cn/notices/index.htm?type=students'
    pinned = 'http://8.8.8.8/notices/index.htm?type=students'

    def acquire(self, location, *, purpose='directory'):
        sent = []
        class Response:
            def __init__(self, first):
                self.status = 301 if first else 200
                self.headers = {'Location': location} if first else {'Content-Type': 'text/html'}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield b'<html><title>Official notices</title></html>'
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url, **kwargs):
                sent.append((url, kwargs))
                return Response(len(sent) == 1)
        def addresses(url):
            if urlsplit(url).hostname != 'campus.example.edu.cn':
                raise ValueError('No public IPv4 address could be verified')
            return ['8.8.8.8']
        module = 'backend.scraper.acquisition.http_transport.'
        with ExitStack() as stack:
            for name, value in (('_shared_permit', None), ('_release_permit', None), ('select_proxy', None)):
                stack.enter_context(patch(module + name, return_value=value))
            stack.enter_context(patch(module + 'public_addresses', side_effect=addresses))
            stack.enter_context(patch(module + 'aiohttp.ClientSession', Session))
            result = fetch_http(FetchRequest(self.original, purpose=purpose))
        return result, sent

    def test_source_upgrade_reuses_original_host_and_exact_sent_path(self):
        for purpose in ('directory', 'list'):
            with self.subTest(purpose=purpose):
                result, sent = self.acquire('https://campus.example.edu.cn' + self.pinned, purpose=purpose)
                self.assertEqual((result.status, result.final_url),
                    (200, self.original.replace('http:', 'https:', 1)))
                self.assertEqual(len(sent), 2)
                self.assertEqual(sent[1][0], self.pinned.replace('http:', 'https:', 1))
                self.assertEqual(sent[1][1]['headers']['Host'], 'campus.example.edu.cn')
                self.assertEqual(sent[1][1]['server_hostname'], 'campus.example.edu.cn')
                self.assertTrue(any(note.startswith('repaired_same_host_https_redirect:') for note in result.evidence))

    def test_unrelated_address_or_path_is_not_reconstructed(self):
        for suffix in (self.pinned.replace('8.8.8.8', '9.9.9.9'), self.pinned.replace('8.8.8.8', '127.0.0.1'),
                       self.pinned.replace('/notices/', '/unobserved/')):
            with self.subTest(suffix=suffix):
                result, sent = self.acquire('https://campus.example.edu.cn' + suffix)
                self.assertEqual(len(sent), 1)
                self.assertFalse(result.ok)
                self.assertFalse(any(note.startswith('repaired_same_host_https_redirect:') for note in result.evidence))

    def test_article_does_not_gain_the_new_source_only_repair(self):
        result, sent = self.acquire('https://campus.example.edu.cn' + self.pinned, purpose='article')
        self.assertEqual(len(sent), 1)
        self.assertFalse(result.ok)

    def test_existing_literal_same_host_repair_is_preserved_for_articles(self):
        result, sent = self.acquire('https://campus.example.edu.cn' + self.original, purpose='article')
        self.assertEqual(result.status, 200)
        self.assertEqual(len(sent), 2)


if __name__ == '__main__':
    unittest.main()
