"""Article wrappers and navigation-only caches must not hide real notice text."""
from datetime import datetime
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result


TITLE = '关于研究生报名安排的通知'
URL = 'https://example.edu.cn/info/1042/123.htm'
NAVIGATION = ('<h3>硕士招生</h3><ul><li><a href="/recommend.htm">推荐免试</a></li>'
              '<li><a href="/register.htm">报名初试</a></li></ul>'
              '<div><a href="/">首页</a> &gt; <a href="/register.htm">报名初试</a> &gt; 正文</div>'
              '<h3>报名初试</h3>')
BODY = '<p>请考生按照本通知公布的安排提交报名材料，具体要求详见附件。</p><a href="/notice.pdf">附件</a>'
PAGE = ('<html><body><div class="main">' + NAVIGATION + '<form name="_newscontent_fromname">'
        '<h1>' + TITLE + '</h1><div>发布时间：2026-10-09</div>'
        '<div id="vsb_content"><div class="v_news_content">' + BODY + '</div></div>'
        '</form></div></body></html>')


class ArticleBodyCacheTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.source = Department(school_id=self.fixture.school_id, name='硕士招生',
            list_url='https://example.edu.cn/notices/', content_selector='div.content, div.main, article')
        db.session.add(self.source)
        db.session.flush()
        self.ann = Announcement(school_id=self.source.school_id, department_id=self.source.id,
            title=TITLE, url=URL)
        db.session.add(self.ann)
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_static_cms_form_preserves_body_and_excludes_column_navigation(self):
        from backend.services.content_cache import fetch_content
        with patch('backend.scraper.acquisition.coordinator.http_fetch',
                   return_value=FetchResult(URL, status=200, html=PAGE)):
            self.assertTrue(fetch_content(self.ann.id)['saved'])
        self.assertIn('提交报名材料', self.ann.content_text)
        self.assertNotIn('推荐免试', self.ann.content_text)
        self.assertNotIn('首页', self.ann.content_text)
        soup = BeautifulSoup(self.ann.content_html, 'lxml')
        self.assertIsNone(soup.form)
        self.assertEqual(soup.a['href'], 'https://example.edu.cn/notice.pdf')

    def test_inner_cms_body_wins_when_publication_metadata_is_not_recognized(self):
        from backend.services.content_cache import fetch_content
        html = PAGE.replace('发布时间：2026-10-09', '时间：2026-10-09 16:21:51')
        with patch('backend.scraper.acquisition.coordinator.http_fetch',
                   return_value=FetchResult(URL, status=200, html=html)):
            fetch_content(self.ann.id)
        self.assertIn('提交报名材料', self.ann.content_text)
        self.assertNotIn('推荐免试', self.ann.content_text)

    def test_navigation_only_readiness_is_not_a_successful_article(self):
        html = '<html><body><div class="main">' + NAVIGATION + '</div></body></html>'
        request = FetchRequest(URL, purpose='article', readiness_selector='div.main',
            policy={'expected_article_title': TITLE})
        result = classify_result(request, FetchResult(URL, status=200, html=html, transport='browser'))
        self.assertFalse(result.ok)

    def test_navigation_only_body_is_not_written_even_if_transport_accepts_it(self):
        from backend.services.content_cache import fetch_content
        html = '<div class="main">' + NAVIGATION + '</div>'
        with patch('backend.scraper.engine._fetch_html', return_value=html):
            with self.assertRaises(ValueError):
                fetch_content(self.ann.id)
        self.assertFalse(self.ann.content_cached_at)
        self.assertFalse(self.ann.content_html)

    def test_reading_old_navigation_cache_queues_repair_and_preserves_article(self):
        from backend.services.content_cache import request_content, status
        self.ann.content_html = '<div>' + NAVIGATION + '</div>'
        self.ann.content_text = '硕士招生 推荐免试 首页 正文'
        self.ann.content_cached_at = datetime.utcnow()
        self.ann.content_bytes = 1300
        db.session.commit()
        identity = (self.ann.id, self.ann.title, self.ann.url, self.ann.department_id)
        job = request_content(self.ann)
        self.assertIsNotNone(job)
        self.assertEqual(job.identity, 'content:' + str(self.ann.id))
        self.assertEqual(status(self.ann), 'loading')
        self.assertFalse(self.ann.content_html)
        self.assertEqual(identity, (self.ann.id, self.ann.title, self.ann.url, self.ann.department_id))

    def test_short_text_and_attachment_only_caches_remain_saved(self):
        from backend.services.content_cache import request_content, status
        for html in ('<p>报名已经截止。</p>', '<p><a href="https://example.edu.cn/notice.pdf">通知附件</a></p>'):
            with self.subTest(html=html):
                self.ann.content_html = html
                self.ann.content_cached_at = datetime.utcnow()
                db.session.commit()
                self.assertIsNone(request_content(self.ann))
                self.assertEqual(status(self.ann), 'saved')
                self.assertEqual(self.ann.content_html, html)


class ArticleBodyReaderRepairTests(unittest.TestCase):
    def setUp(self):
        from tests.test_lightweight import LightweightTests
        from backend.database.models import UserAnnouncementState
        self.fixture = LightweightTests()
        self.fixture.setUp()
        self.ann = self.fixture.notice
        self.ann.content_html = '<div>' + NAVIGATION + '</div>'
        self.ann.content_cached_at = datetime.utcnow()
        self.ann.content_bytes = 1300
        self.saved = UserAnnouncementState(user_id=self.fixture.user.id,
            announcement_id=self.ann.id, starred=True)
        db.session.add(self.saved)
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_both_reader_views_render_a_loader_for_a_stale_navigation_cache(self):
        for url in (f'/announcement/{self.ann.id}',
                    f'/?school={self.ann.school_id}&selected={self.ann.id}&period=all'):
            with self.subTest(url=url):
                self.ann.content_html = '<div>' + NAVIGATION + '</div>'
                self.ann.content_cached_at = datetime.utcnow()
                db.session.commit()
                response = self.fixture.client.get(url)
                self.assertEqual(response.status_code, 200)
                panel = BeautifulSoup(response.text, 'lxml').select_one('[data-content-id]')
                self.assertEqual(panel['data-cached'], 'false')
                self.assertIsNotNone(panel.select_one('.body-loader-retry'))
                self.assertNotIn('推荐免试', panel.get_text())
                self.assertTrue(self.saved.starred)

    def test_content_api_schedules_repair_without_returning_navigation_as_saved_body(self):
        response = self.fixture.client.get(f'/api/announcements/{self.ann.id}/content')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['content_status'], 'loading')
        self.assertEqual(response.json['content_html'], '')
        self.assertTrue(self.saved.starred)


if __name__ == '__main__':
    unittest.main()
