"""Regressions for the retained real-page failure: dated root placeholders and async loaders."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result, execution_context, fetch

URL = 'https://example.edu/'
PENDING = (Path(__file__).parent / 'fixtures/acquisition/dynamic-publication-template.html').read_text(encoding='utf8')
READY = PENDING.replace('href="/"', 'href="/article/42"')


class DynamicMaterialAcquisitionTests(unittest.TestCase):
    def raw(self, html=PENDING, **values):
        return FetchResult(URL, status=200, html=html, **values)

    def test_readable_navigation_and_dated_placeholders_render_before_ai(self):
        for purpose in ('directory', 'list'):
            with self.subTest(purpose=purpose):
                browser = Mock(return_value=self.raw(READY, transport='browser'))
                result = fetch(FetchRequest(URL, purpose=purpose),
                               http_transport=lambda _: self.raw(), browser_transport=browser)
                browser.assert_called_once()
                self.assertEqual(result.transport, 'browser')
                self.assertEqual(result.outcome, 'usable')

    def test_rendered_placeholders_are_incomplete_and_never_usable(self):
        for purpose in ('directory', 'list'):
            with self.subTest(purpose=purpose):
                result = classify_result(FetchRequest(URL, purpose=purpose), self.raw(transport='browser'))
                self.assertEqual(result.outcome, 'needs_adapter')
                self.assertEqual(result.error_code, 'dynamic_material_incomplete')
                self.assertIn('尚未加载完整', result.message)

    def test_failed_dynamic_request_does_not_authorize_complete_page(self):
        html = PENDING.replace('.then(renderCards)', ".catch(()=>console.log('unavailable'))")
        result = classify_result(FetchRequest(URL, purpose='list'), self.raw(html, transport='browser'))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'dynamic_material_incomplete')

    def test_single_real_static_notification_with_other_scripts_stays_http(self):
        html = '<h2>通知公告</h2><li><a href="/article/1">新学期课程安排通知</a>2026-09-23</li><script>fetch("/stats")</script>'
        browser = Mock(side_effect=AssertionError('A real static list requires no browser'))
        result = fetch(FetchRequest(URL, purpose='list'), http_transport=lambda _: self.raw(html), browser_transport=browser)
        self.assertEqual(result.outcome, 'usable')
        browser.assert_not_called()

    def test_verified_target_does_not_wait_for_another_widget(self):
        html = '<section id="target"><a href="/article/1">真实公开通知</a></section>' + PENDING
        result = classify_result(FetchRequest(URL, purpose='list', readiness_selector='#target'), self.raw(html))
        self.assertEqual(result.outcome, 'usable')

    def test_actual_empty_and_attachment_article_remain_distinct(self):
        empty = '<h2>通知公告</h2><p>暂无通知</p><script>fetch("/stats")</script>'
        self.assertEqual(classify_result(FetchRequest(URL, purpose='list'), self.raw(empty)).outcome, 'empty')
        article = '<article><a href="/notice.pdf">通知附件</a></article>' + PENDING
        self.assertEqual(classify_result(FetchRequest(URL, purpose='article'), self.raw(article)).outcome, 'usable')

    def test_access_challenge_and_login_take_precedence(self):
        denied = replace(self.raw(), status=403)
        self.assertEqual(classify_result(FetchRequest(URL), denied).outcome, 'denied')
        challenge = self.raw(PENDING + '<script>$_ts={}</script>')
        self.assertEqual(classify_result(FetchRequest(URL), challenge).error_code, 'access_challenge')
        login = replace(self.raw(PENDING.replace('本科招生信息平台', '统一身份认证')),
                        final_url=URL + 'cas/login?service=https://example.edu/notices')
        self.assertEqual(classify_result(FetchRequest(URL), login).error_code, 'source_login_required')

    def test_partial_dynamic_material_is_not_returned_as_a_discovery_document(self):
        from backend.scraper.discovery.inventory_crawler import fetch_page
        partial = classify_result(FetchRequest(URL), self.raw(transport='browser'))
        with patch('backend.scraper.acquisition.fetch', return_value=partial):
            response = fetch_page(URL)
        self.assertEqual(response['html'], '')
        self.assertEqual(response['result'].html, PENDING)
        unknown = replace(partial, error_code='content_not_recognized')
        with patch('backend.scraper.acquisition.fetch', return_value=unknown):
            self.assertEqual(fetch_page(URL)['html'], PENDING)

    def test_dynamic_sample_is_material_failure_while_real_identity_conflict_stays_a_rule_error(self):
        from backend.services.source_validation import sample_failure_kind
        samples = [{'matched': False, 'error_code': 'dynamic_material_incomplete'}]
        self.assertEqual(sample_failure_kind(samples), 'material_incomplete')
        self.assertEqual(sample_failure_kind(samples + [{'matched': False, 'reason': 'article_title_mismatch'}]), 'rule_error')

    def test_previous_usable_http_snapshot_can_render_without_another_http_request(self):
        cached = self.raw(outcome='usable')
        http = Mock(side_effect=AssertionError('Already observed static material should be reused'))
        browser = Mock(return_value=self.raw(READY, transport='browser'))
        store = Mock()
        with execution_context(cache_lookup=lambda _: cached, cache_store=store):
            result = fetch(FetchRequest(URL), http_transport=http, browser_transport=browser)
        self.assertTrue(result.ok)
        self.assertEqual(result.transport, 'browser')
        browser.assert_called_once()
        http.assert_not_called()
        self.assertEqual(store.call_args.args[1], result)

    def test_incomplete_dynamic_dom_is_not_a_completed_replay_but_other_material_is(self):
        partial = classify_result(FetchRequest(URL), self.raw(transport='browser'))
        self.assertFalse(partial.replayable)
        self.assertTrue(replace(partial, error_code='content_not_recognized').replayable)
        store = Mock()
        with execution_context(cache_store=store):
            result = fetch(FetchRequest(URL), http_transport=lambda _: self.raw(),
                           browser_transport=lambda _: self.raw(transport='browser'))
        self.assertEqual(result.error_code, 'dynamic_material_incomplete')
        store.assert_not_called()

    def test_partially_populated_dynamic_widget_keeps_its_original_readiness_contract(self):
        partial = PENDING.replace('href="/"', 'href="/article/42"', 2)
        browser = Mock(return_value=self.raw(partial, transport='browser'))
        request = FetchRequest(URL)
        result = fetch(request, http_transport=lambda _: self.raw(), browser_transport=browser)
        self.assertEqual(result.error_code, 'dynamic_material_incomplete')
        self.assertTrue(browser.call_args.args[0].policy['dynamic_material_observed'])
        self.assertNotIn('dynamic_material_observed', request.policy)

    def test_month_day_template_dates_are_not_mistaken_for_complete_destinations(self):
        result = classify_result(FetchRequest(URL), self.raw(PENDING.replace('12-16｜2025', '12月 29')))
        self.assertEqual(result.error_code, 'dynamic_material_pending')

    def test_completed_notice_cards_do_not_hide_an_unfinished_column_destination(self):
        partial = PENDING.replace('<a href="/">本科招生', '<a href="/article/42">本科招生')
        request = FetchRequest(URL, policy={'dynamic_material_observed': True})
        result = classify_result(request, self.raw(partial, transport='browser'))
        self.assertEqual(result.error_code, 'dynamic_material_incomplete')
        self.assertIn('placeholder_column_targets:1', result.evidence)
        self.assertTrue(classify_result(request, self.raw(READY, transport='browser')).ok)

    def test_real_text_heading_with_pending_more_link_is_also_unfinished(self):
        partial = PENDING.replace('<a href="/">本科招生', '<a href="/article/42">本科招生')
        partial = partial.replace('<img src="/assets/title公告.png" alt="">', '<h2>通知公告</h2>')
        result = classify_result(FetchRequest(URL, policy={'dynamic_material_observed': True}),
                                 self.raw(partial, transport='browser'))
        self.assertEqual(result.error_code, 'dynamic_material_incomplete')


if __name__ == '__main__': unittest.main()
