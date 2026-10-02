"""Offline acquisition contracts: shells, challenges, redirects and API evidence."""
from dataclasses import replace
import os
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.acquisition import (FetchRequest, FetchResult, FetchFailure,
    FetchedHTML, classify_result, execution_context, fetch, fetch_or_raise)
from backend.scraper.acquisition.browser_client import BrowserClient

ROOT = 'https://example.edu.cn/'
LIST = '<h2>通知公告</h2><ul>' + ''.join(
    f'<li><a href="info/{n}.htm">关于学期安排的通知{n}</a><time>2026-09-23</time></li>'
    for n in range(3)) + '</ul>'
SHELL = '<html><div id="app"></div><script src="app.js"></script></html>'


class AcquisitionTests(unittest.TestCase):
    def raw(self, html, **kwargs):
        return FetchResult(ROOT, status=200, html=html, **kwargs)

    def test_round_trip_contract_preserves_route_and_metadata(self):
        request = FetchRequest(ROOT + '#/news?id=1', purpose='article', policy={'empty_selector': '.empty'})
        self.assertEqual(FetchRequest.from_dict(request.to_dict()), request)
        raw = self.raw(LIST, evidence=('content',), headers={'Content-Type': 'text/html'})
        self.assertEqual(FetchResult.from_dict(raw.to_dict()), raw)
        text = FetchedHTML(raw)
        self.assertEqual(str(text), LIST)
        self.assertEqual(text.final_url, ROOT)

    def test_ssr_app_container_with_list_does_not_open_browser(self):
        browser = Mock(side_effect=AssertionError('SSR must remain HTTP'))
        result = fetch(FetchRequest(ROOT, purpose='list'), http_transport=lambda _: self.raw('<div id="app">' + LIST + '</div><script src="app.js"></script>'), browser_transport=browser)
        self.assertEqual(result.outcome, 'usable')
        browser.assert_not_called()

    def test_delayed_javascript_renders_once_and_returns_final_dom(self):
        browser = Mock(return_value=replace(self.raw(LIST), final_url=ROOT + 'notices/'))
        result = fetch(FetchRequest(ROOT, purpose='list'), http_transport=lambda _: self.raw(SHELL), browser_transport=browser)
        self.assertEqual(result.html, LIST)
        self.assertEqual(result.final_url, ROOT + 'notices/')
        self.assertEqual(result.transport, 'browser')
        browser.assert_called_once()

    def test_shell_after_rendering_is_never_success(self):
        result = fetch(FetchRequest(ROOT, purpose='list'), http_transport=lambda _: self.raw(SHELL),
                       browser_transport=lambda _: self.raw(SHELL))
        self.assertEqual(result.outcome, 'needs_adapter')

    def test_fragment_route_does_not_use_other_route_static_links(self):
        request = FetchRequest(ROOT + '#/news?id=1', purpose='list')
        browser = Mock(return_value=self.raw(LIST))
        result = fetch(request, http_transport=lambda _: replace(self.raw(LIST), final_url=request.url), browser_transport=browser)
        self.assertEqual(result.outcome, 'usable')
        self.assertEqual(browser.call_args.args[0].url, request.url)

    def test_automatic_challenge_becomes_manual_when_still_present(self):
        raw = replace(self.raw('<script>$_ts={};</script>'), status=202)
        result = fetch(FetchRequest(ROOT), http_transport=lambda _: raw, browser_transport=lambda _: raw)
        self.assertEqual(result.outcome, 'needs_manual')
        self.assertFalse(FetchFailure(result).retryable)

    def test_cloudflare_header_is_challenge_even_with_status_200(self):
        raw = self.raw('<p>Please wait</p>', headers={'cf-mitigated': 'challenge'})
        self.assertEqual(classify_result(FetchRequest(ROOT), raw).outcome, 'requires_render')

    def test_human_challenge_never_enters_normal_parser(self):
        raw = self.raw('<form>请完成安全验证<input name="captcha"></form>')
        self.assertEqual(classify_result(FetchRequest(ROOT), raw).outcome, 'needs_manual')

    def test_network_error_does_not_trigger_browser_fallback(self):
        raw = FetchResult(ROOT, outcome='network_error', error_code='timeout', message='超时')
        browser = Mock(side_effect=AssertionError('Network failure is not JS evidence'))
        with self.assertRaises(FetchFailure) as caught:
            fetch_or_raise(FetchRequest(ROOT), http_transport=lambda _: raw, browser_transport=browser)
        self.assertTrue(caught.exception.retryable)
        browser.assert_not_called()

    def test_http_denial_never_turns_into_empty_list(self):
        result = classify_result(FetchRequest(ROOT, purpose='list'), replace(self.raw(''), status=403))
        self.assertEqual(result.outcome, 'denied')
        self.assertFalse(FetchFailure(result).retryable)

    def test_explicit_empty_is_success_but_unrecognized_page_is_not(self):
        request = FetchRequest(ROOT, purpose='list')
        self.assertEqual(classify_result(request, self.raw('<p>暂无通知</p>')).outcome, 'empty')
        self.assertEqual(classify_result(request, self.raw('<nav><a href="/">学校首页</a></nav>')).outcome, 'needs_adapter')

    def test_empty_widget_does_not_override_a_real_notice_list(self):
        request = FetchRequest(ROOT, purpose='list')
        self.assertEqual(classify_result(request, self.raw(LIST + '<aside>暂无通知</aside>')).outcome, 'usable')
        self.assertNotEqual(classify_result(request, self.raw('<p hidden>暂无通知</p><div id="app"></div><script src="app.js"></script>')).outcome, 'empty')

    def test_short_valid_list_is_not_challenge(self):
        from backend.scraper.selector_monitor import is_challenge_shell
        self.assertLess(len(LIST), 2000)
        self.assertFalse(is_challenge_shell(LIST))

    def test_document_redirect_uses_final_url(self):
        calls = []
        def http(request):
            calls.append(request.url)
            return (self.raw('<script>window.location="/notices/"</script>') if len(calls) == 1 else
                    replace(self.raw(LIST), final_url=request.url))
        result = fetch(FetchRequest(ROOT, purpose='list'), http_transport=http)
        self.assertEqual(calls, [ROOT, ROOT + 'notices/'])
        self.assertEqual(result.final_url, ROOT + 'notices/')

    def test_redirect_script_inside_real_page_does_not_navigate(self):
        http = Mock(return_value=self.raw(LIST + '<script>function login(){window.location="/login"}</script>'))
        self.assertEqual(fetch(FetchRequest(ROOT, purpose='list'), http_transport=http).outcome, 'usable')
        http.assert_called_once()

    def test_private_redirect_is_rejected_before_transport(self):
        http = Mock(return_value=self.raw('<script>window.location="http://127.0.0.1/"</script>'))
        with self.assertRaises(ValueError):
            fetch(FetchRequest(ROOT), http_transport=http)
        http.assert_called_once()

    def test_worker_handoff_preserves_source_policy_and_does_not_wait(self):
        class Deferred(BaseException):
            pass
        dispatch = Mock(side_effect=Deferred())
        request = FetchRequest(ROOT, purpose='list', source_id='7', policy_version='3')
        with execution_context(browser_dispatch=dispatch), self.assertRaises(Deferred):
            fetch(request, http_transport=lambda _: self.raw(SHELL))
        self.assertEqual(dispatch.call_args.args[0].source_id, '7')
        self.assertEqual(dispatch.call_args.args[0].policy_version, '3')

    def test_validated_public_api_uses_fixed_mapping(self):
        profile = {'verified': True, 'version': '1', 'evidence_url': ROOT + 'app.js', 'url': ROOT + 'api/notices',
                   'items_path': 'data.rows', 'fields': {'title': 'name', 'url': 'address', 'date': 'published'}, 'empty_is_valid': True}
        request = FetchRequest(ROOT, purpose='list', policy={'api': profile})
        http = Mock(return_value=FetchResult(profile['url'], status=200,
                    json_data={'data': {'rows': [{'name': '课程安排通知', 'address': 'info/1.htm', 'published': '2026-09-23'}]}}))
        result = fetch(request, http_transport=http)
        self.assertEqual(result.transport, 'api')
        self.assertEqual(result.outcome, 'usable')
        self.assertIn(ROOT + 'info/1.htm', result.html)
        self.assertEqual(http.call_args.args[0].url, profile['url'])

    def test_unverified_endpoint_never_makes_network_request(self):
        http = Mock(side_effect=AssertionError('Unverified endpoint'))
        result = fetch(FetchRequest(ROOT, policy={'api': {'url': ROOT + 'guessed'}}), http_transport=http)
        self.assertEqual(result.error_code, 'invalid_api_profile')
        http.assert_not_called()

    def test_api_mapping_change_is_not_an_empty_success(self):
        request = FetchRequest(ROOT, purpose='list', policy={'api': {'verified': True, 'version': '1',
                    'evidence_url': ROOT, 'url': ROOT + 'api', 'items_path': 'rows'}})
        result = fetch(request, http_transport=lambda _: FetchResult(ROOT, status=200, json_data={'results': []}))
        self.assertEqual(result.outcome, 'needs_adapter')

    def test_browser_client_preserves_idempotent_id_and_auth(self):
        session = Mock()
        session.request.return_value.status_code = 202
        session.request.return_value.json.return_value = {'state': 'running'}
        client = BrowserClient('http://browser:5010', 'test-token', session)
        self.assertEqual(client.submit(FetchRequest(ROOT, request_id='stable-id')), ('running', None))
        self.assertEqual(session.request.call_args.kwargs['json']['request_id'], 'stable-id')
        self.assertEqual(session.request.call_args.kwargs['headers']['X-Watcher-Token'], 'test-token')
        client.poll('stable-id')
        self.assertTrue(session.request.call_args.args[1].endswith('/v1/fetch/stable-id'))

    def test_completed_checkpoint_prevents_repeating_http(self):
        completed = self.raw(LIST, outcome='usable')
        lookup = Mock(return_value=completed)
        before = Mock()
        http = Mock(side_effect=AssertionError('Completed page must be replayed'))
        with execution_context(cache_lookup=lookup, before_fetch=before):
            self.assertEqual(fetch(FetchRequest(ROOT), http_transport=http), completed)
        before.assert_called_once()
        http.assert_not_called()

    def test_only_usable_results_are_checkpointed(self):
        store = Mock()
        with execution_context(cache_store=store):
            fetch(FetchRequest(ROOT, purpose='list'), http_transport=lambda _: self.raw(LIST))
            fetch(FetchRequest(ROOT, purpose='list'), http_transport=lambda _: self.raw('<nav>首页</nav>'))
        store.assert_called_once()

    def test_profile_scope_cannot_match_a_hostname_suffix(self):
        from backend.scraper.acquisition.profiles import configured_request, profile_fingerprint
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            catalog = {'version': 1, 'profiles': [{'version': 'r2', 'url_prefix': ROOT.rstrip('/'),
                        'purpose': 'list', 'readiness_selector': '.notices li'}]}
            path.write_text(json.dumps(catalog), encoding='utf-8')
            with patch.dict(os.environ, {'WATCHER_SOURCE_PROFILES': str(path)}):
                initial = profile_fingerprint()
                matched = configured_request(FetchRequest(ROOT + 'news/', purpose='list'))
                self.assertEqual(matched.readiness_selector, '.notices li')
                explicit = configured_request(FetchRequest(ROOT, purpose='list', readiness_selector='#observed-region > div'))
                self.assertEqual(explicit.readiness_selector, '#observed-region > div')
                exploration = configured_request(FetchRequest(ROOT, purpose='list', policy={'exploration': True}))
                self.assertEqual(exploration.readiness_selector, '')
                fake = FetchRequest('https://example.edu.cn.attacker.example/', purpose='list')
                self.assertEqual(configured_request(fake), fake)
                catalog['profiles'][0]['version'] = 'r3'
                path.write_text(json.dumps(catalog), encoding='utf-8')
                self.assertNotEqual(profile_fingerprint(), initial)
                self.assertNotEqual(configured_request(FetchRequest(ROOT, purpose='list')).policy_version, matched.policy_version)

    def test_profile_ambiguous_scopes_fail_closed(self):
        from backend.scraper.acquisition.profiles import configured_request
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            profile = {'version': '1', 'url_prefix': ROOT}
            path.write_text(json.dumps({'version': 1, 'profiles': [profile, profile]}), encoding='utf-8')
            with patch.dict(os.environ, {'WATCHER_SOURCE_PROFILES': str(path)}), self.assertRaisesRegex(ValueError, 'overlap'):
                configured_request(FetchRequest(ROOT))

    def test_browser_manual_error_retains_nonretryable_classification(self):
        session = Mock()
        session.request.return_value.status_code = 409
        session.request.return_value.json.return_value = {'error': {'code': 'verification_required', 'message': '需要验证'}}
        state, result = BrowserClient('http://browser:5010', 'test-token', session).submit(FetchRequest(ROOT))
        self.assertEqual(state, 'failed')
        self.assertEqual(result.outcome, 'needs_manual')
        self.assertFalse(FetchFailure(result).retryable)


if __name__ == '__main__':
    unittest.main()
