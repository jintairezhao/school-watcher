"""Connect a college's notice list before expanding peripheral school pages."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import crawl_site

ROOT = 'https://example.edu.cn/'
COLLEGE = 'https://college.example.edu.cn/'


class CollegePriorityTests(unittest.TestCase):
    def test_college_notice_route_finishes_before_peripheral_navigation(self):
        notices = '<h1>通知公告</h1><ul>' + ''.join(
            f'<li><a href="/2026/1001/c1a{i}/page.htm">关于开展学生课程报名工作的通知{i}</a><span>2026-10-01</span></li>'
            for i in range(3)) + '</ul>'
        pages = {
            ROOT: '<nav><a href="/org/">组织机构</a><a href="/colleges/">院系设置</a><a href="/admissions/">招生通知</a></nav>',
            ROOT + 'colleges/': '<h1>院系设置</h1><main><a href="' + COLLEGE + '">工程学院</a></main>',
            ROOT + 'org/': '<h1>组织机构</h1>',
            ROOT + 'admissions/': '<h1>招生通知</h1>',
            COLLEGE: '<nav><a href="/teaching/">教学成果</a><a href="/notices/">通知公告</a><a href="/teachers/">教师名录</a></nav>',
            COLLEGE + 'notices/': notices,
            COLLEGE + 'teaching/': '<h1>教学成果</h1>',
        }
        reads = []
        def fetch(url):
            reads.append(url)
            return {'url': url, 'status': 200, 'html': pages[url]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Inventory(Path(tmp) / 'inventory.db')
            key = store.ensure_site('测试大学', ROOT)
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
                # Separate slices reproduce the durable worker's small batches.
                crawl_site(store, key, max_pages=3, workers=1, focus='layered', fetcher=fetch)
                crawl_site(store, key, max_pages=2, workers=1, focus='layered', fetcher=fetch)
            self.assertEqual(reads[:4], [ROOT, ROOT + 'colleges/', COLLEGE, COLLEGE + 'notices/'])
            self.assertNotIn(COLLEGE + 'teaching/', reads)
            self.assertNotIn(ROOT + 'admissions/', reads)
            self.assertIsNotNone(store.get_page(key, COLLEGE + 'teachers/') or
                                 store.get_page(key, COLLEGE + 'teaching/'))

    def test_colleges_precede_mentor_entries_and_keep_other_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Inventory(Path(tmp) / 'inventory.db')
            key = store.ensure_site('测试大学', ROOT)
            store.finish(key, ROOT, state='fetched')
            store.enqueue(key, ROOT + 'mentor/', '工程学院', 'unit', 2, ['研究生导师'], 'school_domain')
            store.enqueue(key, COLLEGE, '工程学院', 'unit', 2, [], 'school_domain')
            store.enqueue(key, ROOT + 'teaching/', '教务处', 'unit', 2, [], 'school_domain')
            first = store.claim(key, focus='layered')
            self.assertEqual(first['url'], COLLEGE)
            store.finish(key, first['url'], state='fetched')
            remaining = []
            while page := store.claim(key, focus='layered'):
                remaining.append(page['url'])
                store.finish(key, page['url'], state='fetched')
            self.assertEqual(set(remaining), {ROOT + 'mentor/', ROOT + 'teaching/'})


class CollegeListTests(unittest.TestCase):
    def test_image_first_cards_keep_title_date_and_notice_list(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<html><title>通知公告</title><body><ul class="news_list">' + ''.join(
            f'<li class="news"><div class="news_imgs"><a href="/2026/1001/c1a{i}/page.htm" style="background:url(pic.jpg)"></a></div>'
            f'<div class="news_wz"><div class="news_meta"><span class="news_year">2026</span><span class="news_day">10.01</span></div>'
            f'<div class="news_title"><a href="/2026/1001/c1a{i}/page.htm" title="关于开展学生课程报名工作的通知{i}">关于开展学生课程报名工作的通知{i}</a></div></div></li>'
            for i in range(3)) + '</ul></body></html>'
        feeds = publication_lists(html, COLLEGE + 'notices/')
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '通知公告')
        self.assertEqual(feeds[0]['item_count'], 3)
        self.assertTrue(all(s['date'] == '2026-10-01' for s in feeds[0]['samples']))

    def test_pdf_notices_pass_real_fetch_classification_before_onboarding(self):
        from backend.scraper.acquisition import FetchRequest, FetchResult, fetch
        html = '<html><h1>研究生录取结果公示</h1><div class="wp_articlecontent"><span pdfsrc="/files/result.pdf"></span></div></html>'
        for selector in ('', '.wp_articlecontent'):
            with self.subTest(selector=selector):
                result = fetch(FetchRequest(COLLEGE + 'notice.htm', purpose='article',
                    readiness_selector=selector, browser_allowed=False),
                    http_transport=lambda request: FetchResult(request.url, status=200, html=html))
                self.assertTrue(result.ok, result.to_dict())

    def test_navigation_pdf_and_private_resources_do_not_prove_an_article(self):
        from backend.scraper.acquisition import FetchRequest, FetchResult, fetch
        for html in ('<nav><a href="/files/map.pdf">下载地图</a></nav>',
                     '<article><span pdfsrc="http://127.0.0.1/result.pdf"></span></article>'):
            result = fetch(FetchRequest(COLLEGE + 'notice.htm', purpose='article', browser_allowed=False),
                http_transport=lambda request: FetchResult(request.url, status=200, html=html))
            self.assertFalse(result.ok)
