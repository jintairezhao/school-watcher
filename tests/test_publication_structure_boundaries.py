"""Article occurrences must not expand the institution/source crawl frontier."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure, publication_evidence, add_publication_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory

URL = 'https://example.edu.cn/'


def news(count=12, heading=True, images=False):
    rows = []
    for i in range(count):
        target = '/reports/opaque-item-' + str(i) + '.htm'
        picture = '<a href="' + target + '"><img alt="医学研究院" src="/photo.jpg"></a>' if images else ''
        rows.append('<li>' + picture + '<a href="' + target + '">关于成立先进医学研究院</a>'
                    '<span>2026-09-18</span></li>')
    return '<section>' + ('<h2>学校新闻</h2>' if heading else '') + '<ul class="news-list">' + ''.join(rows) + '</ul></section>'


def parse(html):
    parsed = extract_structure(html, URL, URL, 'root', '示例大学')
    evidence = publication_evidence(html, URL)
    add_publication_structure(parsed, evidence)
    return parsed, evidence


class PublicationStructureBoundaryTests(unittest.TestCase):
    def test_all_articles_not_only_preview_samples_are_removed_from_structure(self):
        html = news()
        before = extract_structure(html, URL, URL, 'root', '示例大学')
        self.assertEqual(sum(n['name'] == '关于成立先进医学研究院' for n in before['nodes']), 12)
        parsed, evidence = parse(html)
        self.assertEqual(evidence['item_count'], 12)
        self.assertEqual(len(evidence['samples']), 8)
        self.assertEqual(len(evidence['article_links']), 12)
        self.assertFalse(any(n['name'] == '关于成立先进医学研究院' for n in parsed['nodes']))
        self.assertEqual(sum(l['decision'] == 'article_reference' for l in parsed['links']), 12)

    def test_same_url_in_real_navigation_keeps_its_unit_relationship(self):
        html = '<nav><ul><li><a href="/reports/opaque-item-0.htm">医学研究院</a></li></ul></nav>' + news()
        parsed, _ = parse(html)
        unit = [n for n in parsed['nodes'] if n['name'] == '医学研究院']
        self.assertEqual(len(unit), 1)
        self.assertEqual(unit[0]['relation'], 'navigation_entry')
        self.assertEqual(unit[0]['url'], URL + 'reports/opaque-item-0.htm')
        self.assertTrue(any(l['label'] == '医学研究院' and l['decision'] == 'follow' for l in parsed['links']))

    def test_duplicate_presentations_preserve_all_article_occurrences(self):
        parsed, evidence = parse(news(count=3) + news(count=3))
        self.assertEqual(len(evidence['lists']), 1)
        self.assertEqual(evidence['item_count'], 3)
        self.assertEqual(len(evidence['article_links']), 6)
        self.assertFalse(any(n['name'] == '关于成立先进医学研究院' for n in parsed['nodes']))

    def test_image_and_unnamed_list_do_not_create_institutions(self):
        parsed, evidence = parse(news(count=3, heading=False, images=True))
        self.assertEqual(evidence['name'], '')
        self.assertEqual(len(evidence['article_links']), 6)
        self.assertFalse(any(n['name'] in ('医学研究院', '关于成立先进医学研究院') for n in parsed['nodes']))

    def test_crawler_keeps_article_evidence_without_enqueuing_new_units(self):
        with tempfile.TemporaryDirectory() as directory:
            inv = Inventory(Path(directory) / 'inventory.db')
            key = inv.ensure_site('示例大学', URL)
            report = inv.report(key)
            inspect_page(inv, report['site'], report['pages'][0], fetcher=lambda _: {
                'html': news(), 'url': URL, 'status': 200})
            self.assertEqual(len(inv.report(key)['pages']), 1)
            with inv.connect() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM edges WHERE decision='article_reference'").fetchone()[0], 12)
            # An article queued by an older parser is retained as reference evidence.
            inv.enqueue(key, URL + 'reports/opaque-item-0.htm', '旧误识别单位', 'unit', 1, [], 'school_domain')
            self.assertEqual(inv.reconcile_article_references(key), 1)
            self.assertEqual(next(p for p in inv.report(key)['pages'] if p['kind'] == 'unit')['state'], 'reference_only')


if __name__ == '__main__':
    unittest.main()
