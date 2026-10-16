"""Source transport recovery uses one browser observation, never a model call."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from backend.scraper.acquisition import FetchRequest, FetchResult, execution_context, fetch

URL = 'http://college.example.edu.cn/'
HTML = '<title>测试学院</title><nav><a href="/news/">通知公告</a><a href="/teaching/">本科教学</a></nav>'


class SourceDisconnectProbeTests(unittest.TestCase):
    def raw(self, **changes):
        return replace(FetchResult(URL, outcome='network_error', error_code='http_serverdisconnectederror'), **changes)

    def test_source_disconnect_reads_original_address_once_in_browser(self):
        for purpose in ('directory', 'list'):
            with self.subTest(purpose=purpose):
                http = Mock(return_value=self.raw())
                browser = Mock(return_value=FetchResult(URL, status=200, html=HTML))
                result = fetch(FetchRequest(URL, purpose=purpose), http_transport=http, browser_transport=browser)
                self.assertEqual((result.outcome, result.transport), ('usable', 'browser'))
                self.assertIn('http_source_transport_probe:http_serverdisconnectederror', result.evidence)
                self.assertEqual(browser.call_args.args[0].url, URL)
                self.assertLessEqual(browser.call_args.args[0].timeout_seconds, 45)
                http.assert_called_once(); browser.assert_called_once()

    def test_article_and_disabled_browser_keep_original_network_failure(self):
        for purpose, allowed in (('article', True), ('directory', False)):
            with self.subTest(purpose=purpose, allowed=allowed):
                browser = Mock(side_effect=AssertionError('No browser probe'))
                result = fetch(FetchRequest(URL, purpose=purpose, browser_allowed=allowed),
                               http_transport=lambda _: self.raw(), browser_transport=browser)
                self.assertEqual(result.error_code, 'http_serverdisconnectederror')
                browser.assert_not_called()

    def test_other_network_failures_and_access_restrictions_are_not_probed(self):
        for raw in [self.raw(error_code=code) for code in
                    ('http_connect_timeout', 'http_idle_timeout', 'http_budget_exhausted', 'http_dns_failure', 'invalid_public_url')] + [
                        self.raw(status=403, outcome='denied', error_code='http_403')]:
            with self.subTest(code=raw.error_code):
                browser = Mock(side_effect=AssertionError('Must retain original failure'))
                result = fetch(FetchRequest(URL, purpose='directory'), http_transport=lambda _:raw, browser_transport=browser)
                self.assertEqual(result.error_code, raw.error_code)
                browser.assert_not_called()

    def test_cached_http_disconnect_is_not_repeated_before_browser_probe(self):
        browser = Mock(return_value=FetchResult(URL, status=200, html=HTML))
        with execution_context(cache_lookup=lambda _:self.raw()):
            result = fetch(FetchRequest(URL, purpose='directory'), browser_transport=browser,
                           http_transport=Mock(side_effect=AssertionError('No second HTTP request')))
        self.assertEqual(result.outcome, 'usable'); browser.assert_called_once()

    def test_browser_disconnect_stays_network_failure_without_recursive_probe(self):
        browser = Mock(return_value=self.raw(transport='browser'))
        result = fetch(FetchRequest(URL, purpose='directory'), http_transport=lambda _:self.raw(), browser_transport=browser)
        self.assertEqual((result.outcome, result.error_code), ('network_error', 'http_serverdisconnectederror'))
        self.assertIn('http_source_transport_probe:http_serverdisconnectederror', result.evidence)
        browser.assert_called_once()

    def test_source_proxy_gateway_failure_gets_one_original_address_browser_read(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                raw = FetchResult(URL, status=status, evidence=('proxy_route_retry_available',))
                browser = Mock(return_value=FetchResult(URL, status=200, html=HTML))
                result = fetch(FetchRequest(URL, purpose='directory'), http_transport=lambda _:raw, browser_transport=browser)
                self.assertEqual((result.outcome, result.transport), ('usable', 'browser'))
                self.assertIn('http_source_transport_probe:proxy_gateway_' + str(status), result.evidence)
                self.assertEqual(browser.call_args.args[0].url, URL)
                browser.assert_called_once()

    def test_gateway_probe_honors_wait_and_requires_proxy_evidence(self):
        for raw in (FetchResult(URL, status=502), FetchResult(URL, status=503, retry_after=120,
                      evidence=('proxy_route_retry_available',)), FetchResult(URL, status=429,
                      evidence=('proxy_route_retry_available',))):
            browser = Mock(side_effect=AssertionError('No immediate browser probe'))
            fetch(FetchRequest(URL, purpose='directory'), http_transport=lambda _:raw, browser_transport=browser)
            browser.assert_not_called()

    def test_onboarding_article_sample_is_source_validation_not_the_normal_reader(self):
        from backend.services.tasks import execution_scope
        article = '<h1>学院公开通知</h1><article><p>现将本学期教学安排公布如下，请各位同学按学校通知要求做好准备工作。</p></article>'
        for kind in ('onboard', 'source_review'):
            with self.subTest(kind=kind), execution_scope({'kind': kind}):
                browser = Mock(return_value=FetchResult(URL, status=200, html=article))
                result = fetch(FetchRequest(URL, purpose='article', policy={'exploration': True}),
                               http_transport=lambda _:self.raw(), browser_transport=browser)
                self.assertTrue(result.ok)
                browser.assert_called_once()

    def test_normal_reader_does_not_inherit_source_trial_transport_changes(self):
        from backend.services.tasks import execution_scope
        for kind in ('content', 'saved_resource', 'publication_date'):
            with self.subTest(kind=kind), execution_scope({'kind': kind}):
                browser = Mock(side_effect=AssertionError('Normal reader retains its policy'))
                result = fetch(FetchRequest(URL, purpose='article', policy={'exploration': True}),
                               http_transport=lambda _:self.raw(), browser_transport=browser)
                self.assertEqual(result.error_code, 'http_serverdisconnectederror')
                browser.assert_not_called()
