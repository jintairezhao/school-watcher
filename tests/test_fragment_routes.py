"""Distinct official app routes must not collapse into one article or institution."""
import json
import tempfile
import unittest
from pathlib import Path

from backend.services.source_inventory import Inventory, canonical_url
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships

ROOT = 'https://www.example.edu.cn/'


class FragmentRouteTests(unittest.TestCase):
    def test_article_identifiers_and_route_query_order_survive(self):
        first = ROOT + 'main.html#/newsDetail?id=one&columnId=notices'
        second = ROOT + 'main.html#/newsDetail?id=two&columnId=notices'
        self.assertEqual(canonical_url(first), first)
        self.assertNotEqual(canonical_url(first), canonical_url(second))
        self.assertEqual(canonical_url(ROOT + '#!/digest?ArticleID=1'), ROOT + '#!/digest?ArticleID=1')
        self.assertEqual(canonical_url(ROOT + '#section'), ROOT)

    def test_hash_only_directory_links_keep_parent_and_separate_unit_identity(self):
        html = ('<ul><li><a href="#/college?id=1">工程学院</a><ul>'
                '<li><a href="#/department?id=1">机械工程系</a></li></ul></li>'
                '<li><a href="#/college?id=2">工程学院</a></li>'
                '<li><a href="#">出版学院</a></li></ul>')
        found = extract_structure(html, ROOT, ROOT, 'directory', '院系设置')
        colleges = [n for n in found['nodes'] if n['name'] == '工程学院']
        self.assertEqual({n['url'] for n in colleges}, {ROOT + '#/college?id=1', ROOT + '#/college?id=2'})
        child = next(n for n in found['nodes'] if n['name'] == '机械工程系')
        self.assertIn(child['parent'], {n['key'] for n in colleges if n['url'].endswith('id=1')})
        self.assertEqual(next(l for l in found['links'] if l['label'] == '出版学院')['url'], '')

    def test_distinct_dated_notifications_and_undated_journal_articles_remain(self):
        for route, date in [('#/newsDetail?id=', '<span>2026-09-16</span>'),
                            ('#!/digest?ArticleID=', '')]:
            html = '<section><h2>通知公告</h2><ul>' + ''.join(
                f'<li><a href="{route}{i}">关于学术活动开展安排的通知{i}</a>{date}</li>' for i in range(4)) + '</ul></section>'
            feeds = publication_lists(html, ROOT)
            self.assertEqual(len(feeds), 1)
            self.assertEqual({s['url'] for s in feeds[0]['samples']}, {ROOT + route + str(i) for i in range(4)})
            links = extract_structure(html, ROOT, ROOT)['links']
            self.assertTrue(all(l['decision'] == 'article_reference' for l in links))

    def test_more_link_preserves_functional_route(self):
        html = '<section><h2><a href="#/notices?department=1">通知公告</a></h2><ul>' + ''.join(
            f'<li><a href="/info/1/{i}.htm">关于新学期奖学金申请工作的通知{i}</a></li>' for i in range(4)) + '</ul></section>'
        self.assertEqual(publication_lists(html, ROOT)[0]['column_url'], ROOT + '#/notices?department=1')

    def test_lightweight_column_discovery_keeps_separate_routes(self):
        from backend.scraper.discovery.lightweight import discover_columns
        columns = discover_columns(ROOT, '<nav><a href="#/notices?id=1">通知公告</a>'
                                   '<a href="#/notices?id=2">新闻动态</a><a href="#news">新闻</a></nav>')
        self.assertEqual({c['list_url'] for c in columns}, {ROOT + '#/notices?id=1', ROOT + '#/notices?id=2'})

    def test_http_app_shell_is_not_route_content_or_a_redirect_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Inventory(Path(tmp) / 'inventory.sqlite3')
            key = inventory.ensure_site('示例大学', ROOT)
            target = ROOT + '#/college?id=1'
            inventory.enqueue(key, target, '工程学院', 'unit', 1, [], 'school_domain')
            report = inventory.report(key)
            page = next(p for p in report['pages'] if p['url'] == target)
            shell = '<title>门户</title><a href="/other/">其他学院</a><script src="app.js"></script>'
            inspect_page(inventory, report['site'], page, fetcher=lambda _: {'html': shell, 'url': ROOT, 'status': 200})
            report = inventory.report(key)
            saved = next(p for p in report['pages'] if p['url'] == target)
            self.assertEqual(saved['health'], 'dynamic_content')
            self.assertIsNone(saved['feed_json'])
            self.assertIn('fragment_route_requires_browser', json.loads(saved['notes_json']))
            self.assertEqual(inventory.snapshot(key, target), shell)
            relationships = SourceRelationships(report, [])
            self.assertNotIn(ROOT, relationships.aliases[target])
            self.assertFalse(any(p['url'] == ROOT + 'other/' for p in report['pages']))


if __name__ == '__main__':
    unittest.main()
