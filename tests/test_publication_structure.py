import json
import tempfile
import unittest
from pathlib import Path

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://www.example.edu.cn/'


def listing(start=10000):
    return '<ul>' + ''.join(f'<li><a href="/info/1/{start+i}.htm">关于新学期学术交流安排的通知{i}</a>'
                           '<span>2026-09-16</span></li>' for i in range(3)) + '</ul>'


class PublicationStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inventory = Inventory(Path(self.temp.name) / 'inventory.sqlite3')
        self.key = self.inventory.ensure_site('某大学', ROOT)

    def inspect(self, html):
        report = self.inventory.report(self.key)
        page = next(p for p in report['pages'] if p['url'] == ROOT)
        inspect_page(self.inventory, report['site'], page,
                     fetcher=lambda _: {'html': html, 'url': ROOT, 'status': 200})
        return self.inventory.structure(self.key)

    def test_proven_column_without_keyword_appears_under_page_identity(self):
        rows = self.inspect('<section><h2><a href="/recommended/">文章推荐</a></h2>' + listing() + '</section>')
        owner = next(n for n in rows if n['relation'] == 'page_identity')
        column = next(n for n in rows if n['name'] == '文章推荐')
        self.assertEqual((column['kind'], column['url'], column['parent_key'], column['relation']),
                         ('channel', ROOT + 'recommended/', owner['node_key'], 'publication_column'))
        self.assertTrue(column['locator'])

    def test_existing_navigation_parents_and_separate_page_placement_survive(self):
        html = '<nav><ul><li><a>科研服务</a><ul><li><a href="/notices/">通知公告</a></li></ul></li></ul></nav>'
        html += '<section><h2><a href="/notices/">通知公告</a></h2>' + listing() + '</section>'
        original = extract_structure(html, ROOT, ROOT, 'root', '某大学')['nodes']
        rows = self.inspect(html)
        for node in original:
            self.assertTrue(any((r['node_key'], r['parent_key'], r['locator']) ==
                                (node['key'], node['parent'], node['locator']) for r in rows))
        appearances = [n for n in rows if n['name'] == '通知公告']
        self.assertEqual(len({n['parent_key'] for n in appearances}), 2)
        self.assertEqual(len(appearances), 2)

    def test_two_same_named_widgets_without_independent_urls_remain_distinct(self):
        rows = self.inspect('<section><h2>文章推荐</h2>' + listing() + '</section>' +
                            '<section><h2>文章推荐</h2>' + listing(20000) + '</section>')
        columns = [n for n in rows if n['relation'] == 'publication_column']
        self.assertEqual(len(columns), 2)
        self.assertEqual(len({n['node_key'] for n in columns}), 2)
        self.assertTrue(all(not n['url'] for n in columns))

    def test_unresolved_title_does_not_create_a_guessed_column(self):
        rows = self.inspect('<section><div class="title"><a href="/a/">通知公告</a>'
                            '<a href="/b/">学术动态</a></div>' + listing() + '</section>')
        self.assertFalse(any(n['relation'] == 'publication_column' for n in rows))
        page = next(p for p in self.inventory.report(self.key)['pages'] if p['url'] == ROOT)
        self.assertTrue(any(n.startswith('publication_heading_requires_review:') for n in json.loads(page['notes_json'])))


if __name__ == '__main__':
    unittest.main()
