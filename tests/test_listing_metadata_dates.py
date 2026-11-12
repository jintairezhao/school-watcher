"""Metadata-list dates must govern both ingestion and historical pagination."""
from datetime import datetime
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.scraper.date_elements import infer_publication_date_selector, publication_date_text


URL = 'https://example.edu.cn/news/'
GENERIC_DATE = 'time, span, em, i, .date, .time'


def page(number, dates, *, body_date='', event=False, stray_span=False):
    cards = []
    for index, stamp in enumerate(dates):
        ident = number * 10 + index
        metadata = '<li>' + ('活动时间：' if event else '') + stamp + '</li>' if stamp else ''
        cards.append(f'<div class="SecondList__list--list"><a class="SecondList__pic" href="/info/{ident}.htm"><img src="/icon.png"></a>'
            f'<div class="SecondList__details"><a class="SecondList__details--link" href="/info/{ident}.htm">'
            f'<h3 class="SecondList__details--title">校区学生奖学金评审工作通知{ident}</h3>'
            + ('<span>校区动态</span>' if stray_span else '')
            + f'<div class="SecondList__details--article">{body_date}</div></a>'
            f'<ul class="SecondList__msg">{metadata}<li>作者：测试作者</li></ul></div></div>')
    return '<section id="column"><h2 id="heading">校区新闻</h2><div class="SecondList__list">' + ''.join(cards) + \
        f'</div><a href="?page={number + 1}" rel="next">下一页</a></section>'


class MetadataDateInferenceTests(unittest.TestCase):
    def test_nested_metadata_list_supplies_an_unambiguous_reusable_selector(self):
        html = page(1, ['2024-01-01', '2024-02-01', '2024-03-01'])
        items = BeautifulSoup(html, 'lxml').select('.SecondList__list--list')
        selector = infer_publication_date_selector(items)
        self.assertTrue(selector)
        self.assertTrue(all(len(item.select(selector)) == 1 for item in items), selector)
        self.assertEqual([publication_date_text(item.select_one(selector)) for item in items],
            ['2024-01-01', '2024-02-01', '2024-03-01'])

    def test_event_dates_and_dates_quoted_in_article_prose_are_not_inferred(self):
        for html in (page(1, ['2023-12-01'] * 3, event=True),
                page(1, [''] * 3, body_date='<ul><li>2023-12-01</li></ul>')):
            self.assertEqual(infer_publication_date_selector(BeautifulSoup(html, 'lxml').select('.SecondList__list--list')), '')

    def test_two_different_metadata_dates_are_left_unresolved(self):
        html = page(1, ['2024-01-01'] * 3).replace('<li>作者：测试作者</li>', '<li>2023-12-31</li>')
        self.assertEqual(infer_publication_date_selector(BeautifulSoup(html, 'lxml').select('.SecondList__list--list')), '')


class MetadataDateCollectionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.source = Department(school_id=self.fixture.school_id, name='校区新闻', list_url=URL,
            list_selector='.SecondList__list > .SecondList__list--list',
            title_selector='h3', link_selector='.SecondList__details--link', date_selector=GENERIC_DATE)
        db.session.add(self.source)
        db.session.commit()
        self.reads = []

    def tearDown(self):
        self.fixture.tearDown()

    def collect(self, pages, *, saved=None):
        from backend.scraper.engine import scrape_department
        def fetch(url, **kwargs):
            self.reads.append(url)
            self.assertIn(url, pages, 'Collection fetched beyond the confirmed date boundary')
            return pages[url]
        with patch('backend.scraper.engine._fetch_html', side_effect=fetch), \
             patch('backend.services.collection_settings.since_date', return_value=datetime(2024, 1, 1)), \
             patch('backend.services.source_governance._column_scope', return_value=(None, [], {'container': '#column', 'heading': '#heading'})), \
             patch('backend.services.source_collection.collection_progress', return_value=saved or {}), \
             patch('backend.services.source_collection.save_collection_progress') as checkpoint:
            result = scrape_department(self.source, 'https://example.edu.cn/')
        return result, checkpoint

    def test_cutoff_and_saved_dates_share_the_inferred_field_and_keep_mixed_pages(self):
        pages = {URL: page(1, ['2024-01-01', '2024-02-01', '2023-12-31'], stray_span=True),
            URL + '?page=2': page(2, ['2023-12-30', '2026-01-01', '2023-12-29']),
            URL + '?page=3': page(3, ['2023-12-28'] * 3),
            URL + '?page=4': page(4, ['2023-12-27'] * 3)}
        result, checkpoint = self.collect(pages)
        self.assertEqual(result, (3, 12))
        self.assertEqual(self.reads, list(pages))
        self.assertEqual(sorted(row.published_at for row in Announcement.query.all()),
            [datetime(2024, 1, 1), datetime(2024, 2, 1), datetime(2026, 1, 1)])
        self.assertTrue(checkpoint.call_args.args[1]['finished'])
        self.assertEqual(self.source.date_selector, GENERIC_DATE)

    def test_undated_entry_prevents_premature_cutoff_and_is_kept(self):
        pages = {URL: page(1, ['2023-12-31'] * 3),
            URL + '?page=2': page(2, ['2023-12-30', '', '2023-12-29']),
            URL + '?page=3': page(3, ['2023-12-28'] * 3),
            URL + '?page=4': page(4, ['2023-12-27'] * 3)}
        self.collect(pages)
        self.assertEqual(self.reads, list(pages))
        self.assertEqual(Announcement.query.count(), 1)
        self.assertIsNone(Announcement.query.one().published_at)

    def test_existing_pagination_cursor_and_saved_counts_are_preserved(self):
        from backend.scraper.engine import PAGINATION_SCOPE_VERSION
        from backend.services.source_governance import _hash, source_config
        saved = dict(new=7, total=20, page=3, page_url=URL + '?page=3', visited=[URL, URL + '?page=2'],
            incremental_mode=False, consecutive_existing=0, old_pages=0, finished=False,
            pagination_scope_version=PAGINATION_SCOPE_VERSION,
            scope_hash=_hash([source_config(self.source), None]),
            selectors={name: getattr(self.source, name) for name in
                ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')})
        pages = {URL + '?page=3': page(3, ['2023-12-28'] * 3),
            URL + '?page=4': page(4, ['2023-12-27'] * 3)}
        result, checkpoint = self.collect(pages, saved=saved)
        self.assertEqual(result, (7, 26))
        self.assertEqual(self.reads, list(pages))
        self.assertTrue(checkpoint.call_args.args[1]['finished'])

    def test_working_configured_publication_field_is_not_replaced_by_another_date(self):
        self.source.date_selector = 'time'
        db.session.commit()
        pages = {URL: page(1, ['2025-01-01'] * 3).replace('<ul class="SecondList__msg">', '<time>2023-12-31</time><ul class="SecondList__msg">'),
            URL + '?page=2': page(2, ['2025-01-01'] * 3).replace('<ul class="SecondList__msg">', '<time>2023-12-30</time><ul class="SecondList__msg">')}
        result, checkpoint = self.collect(pages)
        self.assertEqual(result, (0, 6))
        self.assertEqual(Announcement.query.count(), 0)
        self.assertTrue(checkpoint.call_args.args[1]['finished'])


if __name__ == '__main__':
    unittest.main()
