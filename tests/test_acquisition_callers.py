"""Rendered DOM, final URLs and confirmed empty states survive real callers."""
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
import tempfile

import test_lightweight as fixture
from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource
from backend.scraper.acquisition import FetchResult, FetchedHTML


class AcquisitionCallerTests(unittest.TestCase):
    setUp = fixture.LightweightTests.setUp
    tearDown = fixture.LightweightTests.tearDown
    client_as = fixture.LightweightTests.client_as

    def test_rendered_list_and_final_url_reach_ingestion_and_rule_inspection(self):
        from backend.scraper.engine import scrape_department
        self.news.list_selector = 'ul.notice-list li'
        self.news.title_selector = self.news.link_selector = 'a'
        self.news.date_selector = 'time'
        self.news.content_selector = 'article'
        db.session.commit()
        final = self.school.url + '/new-notices/'
        html = '<ul class="notice-list">' + ''.join(
            f'<li><a href="article/{i}.htm">关于教学安排的有效通知{i}</a><time>2026-09-23</time></li>'
            for i in range(3)) + '</ul>'
        shell = '<div id="app"></div><script src="main.js"></script>'
        with patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=FetchResult(self.news.list_url, status=200, html=shell)), \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch', return_value=FetchResult(final, status=200, html=html, transport='browser')) as browser, \
             patch('backend.scraper.engine.save_element_signatures') as signatures, \
             patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}) as inspect:
            new, total = scrape_department(self.news, self.school.url, strict_fetch=True)
        self.assertEqual((new, total), (3, 3))
        browser.assert_called_once()
        self.assertEqual(signatures.call_args.args[0], html)
        self.assertEqual(inspect.call_args.args[1], html)
        urls = {a.url for a in Announcement.query.filter(Announcement.title.like('关于教学安排的有效通知%'))}
        self.assertEqual(urls, {final + f'article/{i}.htm' for i in range(3)})
        self.assertEqual(self.news.list_url, self.school.url + '/news/')

    def test_article_relative_attachment_uses_redirected_page_address(self):
        from backend.services.content_cache import fetch_content
        final = self.school.url + '/archive/2026/page.htm'
        result = FetchResult(final, status=200, outcome='usable', transport='browser',
            html='<article><p>关于本次课程安排的具体说明。</p><a href="files/guide.pdf">附件</a></article>')
        with patch('backend.scraper.engine._fetch_html', return_value=FetchedHTML(result)):
            fetch_content(self.notice.id)
        self.assertIn(self.school.url + '/archive/2026/files/guide.pdf', self.notice.content_html)

    def test_validated_empty_list_is_success_and_keeps_history(self):
        from backend.services.source_collection import collect_source
        self.news.list_selector = 'ul.notices li'
        db.session.commit()
        before = Announcement.query.count()
        empty = FetchResult(self.news.list_url, status=200, html='<p>暂无通知</p>', outcome='empty')
        with patch('backend.scraper.engine._fetch_html', return_value=FetchedHTML(empty)):
            result = collect_source(self.news)
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(Announcement.query.count(), before)
        self.assertIsNotNone(self.news.last_scraped_at)

    def test_fragment_article_addresses_are_ingested_separately(self):
        from bs4 import BeautifulSoup
        from backend.scraper.engine import _process_announcement_item
        self.news.title_selector = self.news.link_selector = 'a'
        self.news.date_selector = 'time'
        for i in (1, 2):
            item = BeautifulSoup(f'<li><a href="#/news?id={i}">关于课程安排通知{i}</a><time>2026-09-23</time></li>', 'lxml').li
            self.assertTrue(_process_announcement_item(item, self.news, self.school.url))
        db.session.commit()
        announcements = Announcement.query.filter(Announcement.title.like('关于课程安排通知%')).all()
        self.assertEqual({a.url for a in announcements}, {self.news.list_url + f'#/news?id={i}' for i in (1, 2)})

    def test_page_two_browser_handoff_keeps_page_one_new_count(self):
        from backend.scraper.engine import scrape_department
        from backend.services import tasks
        self.news.list_selector = 'ul.notice-list li'
        self.news.title_selector = self.news.link_selector = 'a'
        self.news.date_selector = 'time'
        db.session.commit()
        task = tasks.enqueue('collect', self.news.id, {'department_id': self.news.id})
        handle = tasks.claim()
        def page(start, next_link=''):
            return '<ul class="notice-list">' + ''.join(
                f'<li><a href="article/{i}.htm">关于教学安排的有效通知{i}</a><time>2026-09-23</time></li>'
                for i in range(start, start + 3)) + '</ul>' + next_link
        first = page(0, '<a href="page2.htm">下一页</a>')
        with tasks.execution_scope(handle), \
             patch('backend.scraper.engine._fetch_html', side_effect=[first, tasks.TaskDeferred(capability='browser')]), \
             patch('backend.scraper.engine.save_element_signatures'), \
             self.assertRaises(tasks.TaskDeferred):
            scrape_department(self.news, self.school.url, strict_fetch=True)
        db.session.rollback()
        self.assertEqual(Announcement.query.filter(Announcement.title.like('关于教学安排的有效通知%')).count(), 3)
        with tasks.execution_scope(handle), patch('backend.scraper.engine._fetch_html', return_value=page(3)) as fetch:
            new, total = scrape_department(self.news, self.school.url, strict_fetch=True)
        self.assertEqual((new, total), (6, 6))
        fetch.assert_called_once()
        self.assertTrue(fetch.call_args.args[0].endswith('/news/page2.htm'))
        self.assertEqual(Announcement.query.filter(Announcement.title.like('关于教学安排的有效通知%')).count(), 6)


class DiscoveryRenderTests(unittest.TestCase):
    def test_rendered_fragment_directory_is_parsed_as_actual_route(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import inspect_page
        root = 'https://example.edu.cn/'
        route = root + '#/schools'
        html = '<title>院系设置</title><ul><li><a href="/engineering/">工程学院</a></li></ul>'
        with tempfile.TemporaryDirectory() as directory:
            inventory = Inventory(Path(directory) / 'inventory.sqlite3')
            key = inventory.ensure_site('示例大学', root)
            inventory.enqueue(key, route, '院系设置', 'directory', 1, [], 'school_domain')
            report = inventory.report(key)
            page = next(p for p in report['pages'] if p['url'] == route)
            inspect_page(inventory, report['site'], page, fetcher=lambda _: {
                'html': html, 'url': route, 'status': 200, 'transport': 'browser', 'outcome': 'usable'})
            report = inventory.report(key)
            saved = next(p for p in report['pages'] if p['url'] == route)
            self.assertNotEqual(saved['health'], 'dynamic_content')
            self.assertTrue(any(p['url'] == root + 'engineering/' for p in report['pages']))

    def test_lightweight_discovery_resolves_rendered_redirect(self):
        from backend.scraper.discovery.lightweight import discover_columns
        result = FetchResult('https://example.edu.cn/units/new/', status=200, outcome='usable',
                              html='<a href="notices/">通知公告</a>', transport='browser')
        with patch('backend.scraper.engine._fetch_html', return_value=FetchedHTML(result)):
            columns = discover_columns('https://example.edu.cn/old/')
        self.assertEqual(columns[0]['list_url'], 'https://example.edu.cn/units/new/notices/')


if __name__ == '__main__':
    unittest.main()
