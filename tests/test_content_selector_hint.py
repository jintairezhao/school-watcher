"""Stored article CSS may age; accepting a replacement still needs article proof."""
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result


TITLE = '关于研究生报名安排的通知'
URL = 'https://example.edu.cn/info/1042/123.htm'
OLD_SELECTOR = 'div.v_news_content, div.article-content, article'
BODY = '请考生按照本通知公布的安排提交报名材料，具体要求详见附件。'
PAGE = ('<html><body><div class="custom-detail"><h1>' + TITLE + '</h1>'
        '<div>发布时间：2026-10-09</div><div class="custom-body"><p>' + BODY + '</p>'
        '<a href="/notice.pdf">报名附件</a></div></div>'
        '<aside><p>无关的全站新闻与栏目介绍不得被保存为当前通知正文。</p></aside></body></html>')


def request(**changes):
    values = dict(url=URL, purpose='article', readiness_selector=OLD_SELECTOR,
                  policy={'expected_article_title': TITLE, 'article_selector_hint': True})
    values.update(changes)
    return FetchRequest(**values)


class ContentSelectorHintTests(unittest.TestCase):
    def test_title_bound_body_can_satisfy_an_aged_article_hint(self):
        result = classify_result(request(), FetchResult(URL, status=200, html=PAGE))
        self.assertTrue(result.ok, result)
        self.assertIn('title_bound_article', result.evidence)

    def test_explicit_contract_is_unchanged_without_the_reader_policy(self):
        result = classify_result(request(policy={'expected_article_title': TITLE}),
                                FetchResult(URL, status=200, html=PAGE, transport='browser'))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'readiness_missing')

    def test_hint_never_borrows_a_different_article_or_missing_metadata(self):
        for html in (PAGE.replace(TITLE, '另一条无关通知'),
                     PAGE.replace('发布时间：2026-10-09', ''),
                     PAGE.replace('<p>' + BODY + '</p>', '').replace('<a href="/notice.pdf">报名附件</a>', '')):
            with self.subTest(html=html):
                result = classify_result(request(), FetchResult(URL, status=200, html=html, transport='browser'))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, 'readiness_missing')

    def test_hint_keeps_pending_material_in_the_browser_wait_path(self):
        pending = PAGE.replace('class="custom-body"', 'class="custom-body" aria-busy="true"')
        initial = classify_result(request(), FetchResult(URL, status=200, html=pending))
        self.assertEqual(initial.outcome, 'requires_render')
        self.assertIn('article_material_pending', initial.evidence)
        still_pending = classify_result(request(), FetchResult(URL, status=200, html=pending, transport='browser'))
        self.assertFalse(still_pending.ok)
        self.assertEqual(still_pending.error_code, 'dynamic_material_incomplete')
        self.assertTrue(classify_result(request(), FetchResult(URL, status=200, html=PAGE, transport='browser')).ok)


class ContentSelectorHintCacheTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.source = Department(school_id=self.fixture.school_id, name='报名公告',
            list_url='https://example.edu.cn/notices/', content_selector=OLD_SELECTOR)
        db.session.add(self.source)
        db.session.flush()
        self.ann = Announcement(school_id=self.source.school_id, department_id=self.source.id,
                                title=TITLE, url=URL)
        db.session.add(self.ann)
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_cached_reader_saves_a_verified_static_body_without_browser_or_rule_edit(self):
        from backend.services.content_cache import fetch_content
        with patch('backend.scraper.acquisition.coordinator.http_fetch',
                   return_value=FetchResult(URL, status=200, html=PAGE)) as http, \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',
                   side_effect=AssertionError('Ready public prose needs no browser')):
            self.assertTrue(fetch_content(self.ann.id)['saved'])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.source.content_selector, OLD_SELECTOR)
        self.assertIn(BODY, self.ann.content_text)
        self.assertNotIn('无关的全站新闻', self.ann.content_text)
        self.assertEqual(self.ann.published_at.strftime('%Y-%m-%d'), '2026-10-09')

    def test_cached_reader_still_renders_the_pending_body_before_saving(self):
        from backend.services.content_cache import fetch_content
        pending = PAGE.replace('class="custom-body"', 'class="custom-body" aria-busy="true"')
        with patch('backend.scraper.acquisition.coordinator.http_fetch',
                   return_value=FetchResult(URL, status=200, html=pending)), \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',
                   return_value=FetchResult(URL, status=200, html=PAGE, transport='browser')) as browser:
            self.assertTrue(fetch_content(self.ann.id)['saved'])
        browser.assert_called_once()
        self.assertEqual(browser.call_args.args[0].readiness_selector, OLD_SELECTOR)
        self.assertTrue(browser.call_args.args[0].policy['article_selector_hint'])
        self.assertIn(BODY, self.ann.content_text)


class ContentSelectorHintBrowserWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_observes_ready_title_bound_material_without_waiting_for_old_css(self):
        from backend.browser_service.runtime import BrowserRuntime
        from tests import test_browser_runtime as fixture
        reads = []
        pending = PAGE.replace('class="custom-body"', 'class="custom-body" aria-busy="true"')
        async def content(page):
            reads.append(True)
            return pending if len(reads) < 3 else PAGE
        async def launch(*args):
            return fixture.FakeBrowser()
        runtime = BrowserRuntime(launcher=launch, validator=fixture.valid_public, concurrency=1)
        payload = request(url='https://example.edu/notice.htm', request_id='body-hint', timeout_seconds=2).to_dict()
        try:
            with patch.object(fixture.FakePage, 'content', content):
                await runtime.submit(payload)
                await runtime.executions['body-hint'].task
                value = runtime.get_execution('body-hint')
            self.assertEqual(value['state'], 'done', value)
            self.assertEqual(value['result']['html'], PAGE)
            # Two pending reads, one ready read, and one final validated read.
            # Continuing until a missing CSS selector times out adds more reads.
            self.assertEqual(len(reads), 4)
        finally:
            await runtime.close()


if __name__ == '__main__':
    unittest.main()
