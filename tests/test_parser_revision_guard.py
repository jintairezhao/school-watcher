"""A worker from before a deployment must not overwrite newer source evidence."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page, crawl_site
from backend.scraper.discovery.parser_revision import PARSER_REVISION, ParserRevisionChanged

URL = 'https://example.edu.cn/'
TARGET = 'backend.scraper.discovery.parser_revision.current_revision'


class ParserRevisionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inv = Inventory(Path(self.temp.name) / 'inventory.db')
        self.key = self.inv.ensure_site('测试大学', URL)

    def test_obsolete_worker_does_not_fetch_or_change_the_page(self):
        report = self.inv.report(self.key)
        with patch(TARGET, return_value='obsolete'), patch('backend.scraper.discovery.inventory_crawler.fetch_page') as fetch:
            with self.assertRaises(ParserRevisionChanged):
                inspect_page(self.inv, report['site'], report['pages'][0], fetcher=fetch)
            fetch.assert_not_called()
        self.assertEqual(self.inv.report(self.key)['pages'], report['pages'])

    def test_deployment_during_fetch_or_parse_preserves_snapshot_and_structure(self):
        report = self.inv.report(self.key)
        inspect_page(self.inv, report['site'], report['pages'][0], fetcher=lambda _: {
            'html': '<title>原始目录</title><a href="/units/">院系设置</a>', 'status': 200, 'url': URL})
        report = self.inv.report(self.key)
        original_structure = self.inv.structure(self.key)
        snapshot = self.inv.snapshot(self.key, URL)
        for changes in [[PARSER_REVISION, 'changed'], [PARSER_REVISION, PARSER_REVISION, 'changed']]:
            with self.subTest(changes=len(changes)), patch(TARGET, side_effect=changes):
                with self.assertRaises(ParserRevisionChanged):
                    inspect_page(self.inv, report['site'], report['pages'][0], fetcher=lambda _: {
                        'html': '<title>旧进程的不同结果</title><a href="/old/">旧学院</a>', 'status': 200, 'url': URL})
            self.assertEqual(self.inv.report(self.key)['pages'], report['pages'])
            self.assertEqual(self.inv.structure(self.key), original_structure)
            self.assertEqual(self.inv.snapshot(self.key, URL), snapshot)

    def test_inflight_claim_returns_to_pending_instead_of_a_website_failure(self):
        with patch(TARGET, return_value=PARSER_REVISION) as version:
            def fetch(_):
                version.return_value = 'changed'
                return {'html': '<p>正常官网</p>', 'status': 200, 'url': URL}
            with self.assertRaises(ParserRevisionChanged):
                crawl_site(self.inv, self.key, max_pages=1, workers=1, fetcher=fetch)
        page = self.inv.report(self.key)['pages'][0]
        self.assertEqual(page['state'], 'pending')
        self.assertFalse(page.get('error'))

    def test_temporary_missing_parser_file_is_not_a_network_failure(self):
        report = self.inv.report(self.key)
        with patch(TARGET, side_effect=[PARSER_REVISION, FileNotFoundError('deployment')]):
            with self.assertRaises(ParserRevisionChanged):
                inspect_page(self.inv, report['site'], report['pages'][0], fetcher=lambda _: {
                    'html': '', 'status': 503, 'url': URL})
        self.assertEqual(self.inv.report(self.key)['pages'], report['pages'])


if __name__ == '__main__':
    unittest.main()
