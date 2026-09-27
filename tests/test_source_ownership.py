"""Official backlinks identify candidates, not ownership of every external site."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.services.source_catalog import publication_candidates

ROOT = 'https://www.example.edu.cn/'
EXTERNAL = 'https://hospital.example.org/'


def news_list():
    return '<h2><a href="/notices/">通知公告</a></h2><ul class="news-list">' + ''.join(
        '<li><a href="/info/1/' + str(10000 + n) + '.htm">新学期工作安排通知' + str(n) +
        '</a><time>2026-09-18</time></li>' for n in range(3)) + '</ul>'


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inventory = Inventory(Path(self.temp.name) / 'inventory.db')
        self.key = self.inventory.ensure_site('某大学', ROOT)

    def crawl(self, external_html, budget=2):
        pages = {ROOT: '<a href="' + EXTERNAL + '">附属医院</a>', EXTERNAL: external_html}
        return crawl_site(self.inventory, self.key, max_pages=budget, workers=1,
                          fetcher=lambda url: {'url': url, 'status': 200, 'html': pages.get(url, news_list())})

    def test_news_mention_and_friendly_link_cannot_claim_external_site_as_school_unit(self):
        html = '<title>合作机构新闻</title><article>某大学代表团来访</article><footer><a href="' + ROOT + '">某大学</a></footer>' + news_list()
        report = self.crawl(html)
        external = next(p for p in report['pages'] if p['url'] == EXTERNAL)
        self.assertIn('external_ownership_requires_review', json.loads(external['notes_json']))
        self.assertEqual(len(report['pages']), 2)
        self.assertEqual(publication_candidates(report), [])
        self.assertIsNotNone(self.inventory.snapshot(self.key, EXTERNAL))

    def test_external_school_branding_preserves_official_unit_and_its_columns(self):
        report = self.crawl('<title>某大学附属医院</title><h1>某大学附属医院</h1>' + news_list())
        external = next(p for p in report['pages'] if p['url'] == EXTERNAL)
        self.assertTrue(any(n.startswith('external_identity_evidence:') for n in json.loads(external['notes_json'])))
        self.assertTrue(any(p['url'] == EXTERNAL + 'notices/' for p in report['pages']))
        self.assertGreaterEqual(len(publication_candidates(report)), 1)

    def test_domain_evidence_can_support_a_notice_page_without_repeating_school_brand(self):
        report = self.crawl('<title>某大学附属医院</title>' + news_list(), budget=3)
        notices = next(p for p in report['pages'] if p['url'] == EXTERNAL + 'notices/')
        self.assertEqual(notices['state'], 'fetched')
        self.assertNotIn('external_ownership_requires_review', json.loads(notices['notes_json']))

    def test_stale_or_unavailable_branding_cannot_authorize_other_pages(self):
        self.crawl('<title>某大学附属医院</title>' + news_list())
        self.inventory.finish(self.key, EXTERNAL, state='failed', health='unreachable', error='HTTP 503')
        report = crawl_site(self.inventory, self.key, max_pages=1, workers=1,
            fetcher=lambda url: {'url': url, 'status': 200, 'html': news_list()})
        notices = next(p for p in report['pages'] if p['url'] == EXTERNAL + 'notices/')
        self.assertIn('external_ownership_requires_review', json.loads(notices['notes_json']))
        self.assertEqual(publication_candidates(report), [])

    def test_news_headline_starting_with_school_and_unit_is_not_site_identity(self):
        report = self.crawl('<title>某大学附属医院代表团来访</title><article>交流活动新闻</article>' + news_list())
        self.assertEqual(len(report['pages']), 2)
        self.assertEqual(publication_candidates(report), [])

    def test_changed_snapshot_invalidates_cached_domain_identity(self):
        self.crawl('<title>某大学附属医院</title>' + news_list())
        self.inventory.finish(self.key, EXTERNAL, html='<title>合作网站</title>', state='fetched')
        report = crawl_site(self.inventory, self.key, max_pages=1, workers=1,
            fetcher=lambda url: {'url': url, 'status': 200, 'html': news_list()})
        self.assertEqual(publication_candidates(report), [])


if __name__ == '__main__':
    unittest.main()
