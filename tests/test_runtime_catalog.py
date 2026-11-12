from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_page_metadata_only_reads_requested_pages_without_html_or_full_report(self):
        self.inspect('<title>例校</title><a href="/news/">通知公告</a>')
        self.catalog.publish(self.inv, self.key)
        root = 'https://example.edu.cn/'
        with self.catalog.connect(write=True) as connection:
            ident = connection.execute('SELECT id FROM catalog_sites WHERE site_key=?', (self.key,)).fetchone()[0]
            # An unrelated corrupt page would fail if a full scan inflated it.
            connection.execute('INSERT INTO catalog_pages(site_id,url,page_gzip,nodes_gzip,evidence_gzip) VALUES(?,?,?,?,?)',
                (ident, root + 'unrelated/', b'invalid metadata', b'invalid nodes', b'invalid HTML'))
            connection.execute('UPDATE catalog_pages SET nodes_gzip=?,evidence_gzip=? WHERE url=?',
                (b'invalid nodes', b'invalid HTML', root))
        with patch.object(self.catalog, 'report', side_effect=AssertionError('No report')), \
             patch.object(self.catalog, 'snapshot', side_effect=AssertionError('No HTML')):
            info = self.catalog.page_metadata(self.key, [root, root, root + 'missing/'])
        self.assertEqual(set(info), {root})
        self.assertEqual(info[root]['title'], '例校')
        self.assertNotIn('feed_json', info[root])

    def test_listing_alias_metadata_is_explicit_and_stays_on_requested_pages(self):
        self.inspect('<title>例校</title><a href="/news/">通知公告</a>')
        self.catalog.publish(self.inv,self.key)
        root='https://example.edu.cn/'
        with self.catalog.connect(write=True)as connection:
            ident=connection.execute('SELECT id FROM catalog_sites WHERE site_key=?',(self.key,)).fetchone()[0]
            connection.execute('INSERT INTO catalog_pages(site_id,url,page_gzip,nodes_gzip,evidence_gzip) VALUES(?,?,?,?,?)',
                (ident,root+'unrelated/',b'invalid metadata',b'invalid nodes',b'invalid HTML'))
            connection.execute('UPDATE catalog_pages SET nodes_gzip=?,evidence_gzip=? WHERE url=?',
                (b'invalid nodes',b'invalid HTML',root))
        with patch.object(self.catalog,'report',side_effect=AssertionError('No report')),\
             patch.object(self.catalog,'snapshot',side_effect=AssertionError('No HTML')):
            info=self.catalog.page_metadata(self.key,[root],include_listing_data=True)
        self.assertEqual(set(info),{root})
        self.assertTrue(info[root]['checked_at'])
        self.assertIn('feed_json',info[root])
        self.assertEqual(info[root]['label'],'例校')

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

    def test_old_cached_parse_cannot_replace_a_published_parse_of_the_same_observation(self):
        root = 'https://example.edu.cn/'
        self.inspect('<title>例校</title><a href="/news/">通知公告</a>')
        self.catalog.publish(self.inv, self.key)
        expected_page = self.catalog.get_page(self.key, root)
        expected_nodes = self.catalog.structure(self.key)
        with self.inv.connect() as c:
            c.execute('UPDATE pages SET parser_revision=? WHERE site_key=? AND url=?',
                      ('stale-cached-parser', self.key, root))
            c.execute('DELETE FROM edges WHERE site_key=? AND parent_url=?', (self.key, root))
            c.execute('DELETE FROM structure WHERE site_key=? AND reference_url=?', (self.key, root))
        self.catalog.publish(self.inv, self.key, merge=True)
        self.assertEqual(self.catalog.get_page(self.key, root), expected_page)
        self.assertEqual(self.catalog.structure(self.key), expected_nodes)
        self.assertEqual(self.catalog.snapshot(self.key, root), '<title>例校</title><a href="/news/">通知公告</a>')

    def test_current_reparse_can_replace_an_old_published_parse_of_the_same_observation(self):
        from backend.services.runtime_catalog import pack
        root = 'https://example.edu.cn/'
        self.inspect('<title>例校</title><a href="/news/">通知公告</a>')
        self.catalog.publish(self.inv, self.key)
        expected_page = self.catalog.get_page(self.key, root)
        old_page = dict(expected_page, parser_revision='stale-published-parser', navigation_edges=[])
        with self.catalog.connect(write=True) as c:
            c.execute('UPDATE catalog_pages SET page_gzip=? WHERE url=?', (pack(old_page), root))
        self.catalog.publish(self.inv, self.key, merge=True)
        self.assertEqual(self.catalog.get_page(self.key, root), expected_page)

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

    def navigation_fixture(self):
        root = 'https://example.edu.cn/'
        directory = root + 'departments/'
        organization = root + 'organization/'
        self.inv.finish(self.key, root, state='fetched', title='Example', html='<nav>saved links</nav>')
        material = self.inv.get_page(self.key, root)['content_hash']
        self.inv.record_structure(self.key, root, [
            dict(key='root', name='Example', kind='group', url=root, parent='', locator='document', relation='page_identity'),
            dict(key='directory', name='Academic departments', kind='group', url=directory,
                 parent='root', locator='nav > a.departments', relation='navigation_entry'),
        ], material)
        self.inv.record_edges(self.key, root, [
            dict(url=directory, label='Academic departments', kind='directory', path=[],
                 locator='nav > a.departments', decision='follow'),
            # Two real entrances to one target are distinct observations.
            dict(url=directory, label='Academic departments', kind='directory', path=[],
                 locator='main > a.departments', decision='follow'),
            dict(url=organization, label='Organization', kind='directory', path=['About'],
                 locator='nav > ul > li > ul > a.organization', decision='follow'),
        ], material)
        return root, directory, organization, material

    def test_navigation_publication_preserves_direct_links_and_material_without_inventing_ownership(self):
        root, directory, organization, material = self.navigation_fixture()
        self.catalog.publish(self.inv, self.key)
        snapshot = self.catalog.monitor_snapshot(self.key)
        links = snapshot['navigation_edges']
        self.assertEqual(len(links), 3)
        self.assertEqual({edge['parent_url'] for edge in links}, {root})
        self.assertEqual({edge['target_url'] for edge in links}, {directory, organization})
        self.assertEqual(len([edge for edge in links if edge['target_url'] == directory]), 2)
        self.assertTrue(all(edge['content_hash'] == material and edge['observed_at'] and edge['locator'] for edge in links))
        direct = next(node for node in snapshot['structure_nodes'] if node['relation'] == 'navigation_entry')
        self.assertEqual(direct['content_hash'], material)
        self.assertTrue(direct['observed_at'])
        paths = self.catalog.paths(self.key)
        self.assertNotIn(directory, paths, 'Navigation links alone cannot establish an owner or roster path')
        self.assertNotIn(organization, paths)
        self.assertTrue(all(not path['nodes'] for path in paths.get(root, [])))
        # The monitor must remain independent of mutable investigation data.
        self.inv.path.unlink()
        self.assertEqual(self.catalog.monitor_snapshot(self.key)['navigation_edges'], links)

    def test_navigation_material_mismatch_is_excluded_on_publish_and_read(self):
        root, directory, organization, _ = self.navigation_fixture()
        with self.inv.connect() as connection:
            connection.execute('UPDATE edges SET content_hash=? WHERE target_url=?', ('old-material', organization))
        self.catalog.publish(self.inv, self.key)
        self.assertEqual({edge['target_url'] for edge in self.catalog.monitor_snapshot(self.key)['navigation_edges']}, {directory})
        from backend.services.runtime_catalog import pack, unpack
        with self.catalog.connect(write=True) as connection:
            row = connection.execute('SELECT page_gzip FROM catalog_pages WHERE url=?', (root,)).fetchone()
            page = unpack(row[0])
            page['navigation_edges'].append(dict(page['navigation_edges'][0], parent_url='https://wrong.example/'))
            page['content_hash'] = 'new-page-material'
            connection.execute('UPDATE catalog_pages SET page_gzip=? WHERE url=?', (pack(page), root))
        self.assertEqual(self.catalog.monitor_snapshot(self.key)['navigation_edges'], [])
        self.assertEqual(self.catalog.monitor_snapshot(self.key)['structure_nodes'], [])

    def test_navigation_failed_merge_preserves_valid_previous_material_and_republication_is_stable(self):
        root, _, _, _ = self.navigation_fixture()
        from backend.services.runtime_catalog import _catalogue_digest
        self.catalog.publish(self.inv, self.key)
        before = self.catalog.monitor_snapshot(self.key)['navigation_edges']
        fingerprint = _catalogue_digest(self.catalog, self.key)
        self.catalog.publish(self.inv, self.key)
        self.assertEqual(_catalogue_digest(self.catalog, self.key), fingerprint)
        self.inv.finish(self.key, root, state='failed', error='network failure')
        self.catalog.publish(self.inv, self.key, merge=True)
        self.assertEqual(self.catalog.monitor_snapshot(self.key)['navigation_edges'], before)
        self.assertEqual(self.catalog.get_page(self.key, root)['latest_attempt']['state'], 'failed')
        # A successful replacement invalidates old edges even if scratch still
        # carries them; changed material must never inherit old navigation.
        self.inv.finish(self.key, root, state='fetched', html='<nav>different material</nav>')
        self.catalog.publish(self.inv, self.key, merge=True)
        self.assertEqual(self.catalog.monitor_snapshot(self.key)['navigation_edges'], [])


if __name__ == '__main__':
    unittest.main()
