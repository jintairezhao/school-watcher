from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.discovery_cache import DiscoveryCache
from backend.scraper.discovery.inventory_crawler import inspect_page


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.inv = Inventory(self.folder / 'investigation.db')
        self.key = self.inv.ensure_site('例校', 'https://example.edu.cn/')
        self.catalog = RuntimeCatalog(self.folder / 'catalog.db')

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self, html):
        report = self.inv.report(self.key)
        page = next(p for p in report['pages'] if p['kind'] == 'root')
        inspect_page(self.inv, report['site'], page, fetcher=lambda url: {
            'html': html, 'url': url, 'status': 200})

    def test_catalogue_survives_removal_of_investigation_database(self):
        html = (Path(__file__).parent / 'fixtures/cupk_teaching_modules.html').read_text(encoding='utf-8')
        self.inspect(html.replace('https://www.cupk.edu.cn', 'https://example.edu.cn'))
        self.catalog.publish(self.inv, self.key)
        expected = self.catalog.candidates(self.key)
        self.assertTrue(expected)
        self.inv.path.unlink()
        self.assertEqual(self.catalog.candidates(self.key), expected)
        self.assertIsNotNone(self.catalog.report(self.key))
        self.assertEqual(len(self.catalog.all_sites()), 1)

    def test_changed_reference_invalidates_prior_check_on_merge(self):
        self.inspect('<title>例校</title><h2>院系设置</h2><a href="https://cs.example.edu.cn/">计算机学院</a>')
        report = self.inv.report(self.key)
        page = report['pages'][0]
        result = {'id': 'scope', 'reference_url': page['url'], 'reference_hash': page['content_hash'],
                  'scope_passed': True, 'reference_current': True, 'category': 'academic'}
        with self.inv.connect() as c:
            c.execute('INSERT INTO reference_checks VALUES(?,?,?,?,?)',
                      (self.key, 'scope', '{}', json.dumps(result), '2026-09-21'))
        self.catalog.publish(self.inv, self.key)
        self.inspect('<title>例校</title><h2>院系设置调整</h2><a href="https://eng.example.edu.cn/">工程学院</a>')
        self.catalog.publish(self.inv, self.key, merge=True)
        check = self.catalog.report(self.key)['reference_checks'][0]
        self.assertFalse(check['scope_passed'])
        self.assertFalse(check['reference_current'])

    def test_scratch_frontier_capacity_is_explicit_and_keeps_pending_work(self):
        inv = DiscoveryCache(self.folder / 'scratch.db')
        key = inv.ensure_site('例校', 'https://example.edu.cn/')
        inv.max_pages_per_site = 3
        for i in range(2):
            inv.enqueue(key, f'https://example.edu.cn/college/{i}', '学院', 'unit', 1, ['例校'], 'school_domain')
        with self.assertRaisesRegex(RuntimeError, '容量上限'):
            inv.enqueue(key, 'https://example.edu.cn/misc', '杂项导航', 'navigation', 1, [], 'school_domain')
        self.assertEqual(len(inv.report(key)['pages']), 3)
        self.assertEqual(inv.report(key)['states']['pending'], 3)
        inv.record_edges(key, 'https://example.edu.cn/', [], 'test')
        with inv.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM edges').fetchone()[0], 0)
        inv.trim()

    def test_deep_teaching_navigation_and_its_evidence_are_retained(self):
        inv = DiscoveryCache(self.folder / 'scratch.db')
        key = inv.ensure_site('例校', 'https://example.edu.cn/')
        inv.enqueue(key, 'https://example.edu.cn/college/teaching/graduate/notice/', '研究生招生',
                    'navigation', 7, ['学院', '人才培养', '研究生'], 'school_domain')
        pages = inv.report(key)['pages']
        self.assertTrue(any(page['depth'] == 7 and page['kind'] == 'navigation' for page in pages))
        inv.record_edges(key, 'https://example.edu.cn/', [{'url': pages[-1]['url'], 'label': '研究生招生',
                         'kind': 'navigation', 'decision': 'follow'}], 'proof')
        self.assertEqual(inv.report(key)['edge_count'], 1)

    def test_failed_refresh_keeps_published_list_and_owner_evidence(self):
        html = (Path(__file__).parent / 'fixtures/cupk_teaching_modules.html').read_text(encoding='utf-8')
        self.inspect(html.replace('https://www.cupk.edu.cn', 'https://example.edu.cn'))
        self.catalog.publish(self.inv, self.key)
        candidates = self.catalog.candidates(self.key)
        self.assertTrue(candidates)
        self.inv.finish(self.key, 'https://example.edu.cn/', state='failed', error='network failure')
        self.catalog.publish(self.inv, self.key, merge=True)
        from backend.services.source_catalog import FIELDS
        def published_configs(items):
            return [{**{field: item.get(field) for field in ('name', *FIELDS)},
                     'owners': sorted({path['unit_name'] for path in item.get('source_structure', [])})}
                    for item in items]
        self.assertEqual(published_configs(self.catalog.candidates(self.key)), published_configs(candidates))
        page = self.catalog.get_page(self.key, 'https://example.edu.cn/')
        self.assertEqual(page['state'], 'fetched')
        self.assertEqual(page['latest_attempt']['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
