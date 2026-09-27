"""Transport failures must remain distinguishable from unmatched notification lists."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from backend.scraper.engine import _fetch_html
from backend.services.source_collection import fetch_source_page,SourceAccessError


class FetchFailureTests(unittest.TestCase):
    def test_configured_source_does_not_hide_dns_failure_as_empty_html(self):
        with patch.dict(os.environ,{'WATCHER_BROWSER':'0'}),patch('backend.scraper.engine.requests.get',
                side_effect=ValueError('No public IPv4 address could be verified')):
            with self.assertRaisesRegex(RuntimeError,'公网地址'):
                _fetch_html('https://www.example.edu.cn/',raise_fetch_errors=True)
            self.assertEqual(_fetch_html('https://www.example.edu.cn/'),'')

    def test_access_challenge_has_no_immediate_retry(self):
        response=requests.Response();response.status_code=202
        response._content=b'<script>$_ts={};</script>';response._content_consumed=True
        with patch.dict(os.environ,{'WATCHER_BROWSER':'0'}),patch('backend.scraper.engine.requests.get',return_value=response):
            with self.assertRaisesRegex(SourceAccessError,'访问校验') as caught:
                _fetch_html('https://www.example.edu.cn/',raise_fetch_errors=True)
            self.assertFalse(caught.exception.retryable)

    def test_unit_response_codes_are_not_flattened_to_generic_failure(self):
        for status,label in [(403,'拒绝'),(404,'不存在'),(429,'过于频繁'),(503,'暂时不可用')]:
            with patch('backend.scraper.discovery.inventory_crawler.fetch_page',return_value={'status':status,'html':''}):
                with self.assertRaisesRegex(RuntimeError,label) as caught:fetch_source_page('https://www.example.edu.cn/')
                self.assertEqual(caught.exception.retryable,status==503)

    def test_network_timeout_retains_a_retryable_cause(self):
        with patch('backend.scraper.discovery.inventory_crawler.fetch_page',side_effect=requests.Timeout('read timeout')):
            with self.assertRaisesRegex(RuntimeError,'超时') as caught:fetch_source_page('https://www.example.edu.cn/')
            self.assertTrue(caught.exception.retryable)


if __name__=='__main__':unittest.main()
