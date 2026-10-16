"""Optional engine boundary tests; real-browser comparison is a separate command."""
import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.acquisition.contracts import FetchRequest
from backend.scraper.acquisition.crawl4ai_transport import (
    Crawl4AIFixtureTransport, Crawl4AIPublicDiagnosticTransport, fixture_validator,
    normalize_observation, readiness_wait)


BASE = 'https://example.edu.cn/'
LIST = '<h2>通知公告</h2><ul><li><a href="/notice/1">关于学期安排的通知</a></li></ul>'


class Crawl4AIBoundaryTests(unittest.TestCase):
    def test_readiness_keeps_observed_selector_as_data_and_does_not_require_visibility(self):
        import json
        selector = '[data-label="复杂\\\"值"];window.untrusted=true;//'
        wait = readiness_wait(selector)
        self.assertEqual(json.loads(wait[len('js:() => document.querySelector('):-len(') !== null')]), selector)
        self.assertIsNone(readiness_wait(''))

    def request(self, **kwargs):
        return FetchRequest(BASE, purpose='directory', **kwargs)

    def observe(self, **kwargs):
        return dict(html=LIST, status_code=200, url=BASE, success=True, **kwargs)

    def test_optional_module_import_does_not_import_engine(self):
        real_import = importlib.import_module
        def guard(name, *args, **kwargs):
            if name == 'crawl4ai':
                raise AssertionError('Optional dependency imported during module import')
            return real_import(name, *args, **kwargs)
        with patch('importlib.import_module', side_effect=guard):
            import backend.scraper.acquisition.crawl4ai_transport as module
            importlib.reload(module)

    def test_success_flag_does_not_turn_404_into_success(self):
        observed = self.observe()
        observed.update(status_code=404, html='<title>404</title><p>Not found</p>')
        result = normalize_observation(self.request(), observed)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 404)

    def test_failed_engine_with_actual_denial_preserves_denial(self):
        observed = self.observe()
        observed.update(status_code=403, success=False, html='<title>Access denied</title>Forbidden')
        result = normalize_observation(self.request(), observed)
        self.assertEqual(result.outcome, 'denied')
        self.assertEqual(result.status, 403)

    def test_missing_http_status_is_not_proof(self):
        observed = self.observe()
        observed['status_code'] = None
        self.assertEqual(normalize_observation(self.request(), observed).error_code,
                         'crawl4ai_no_http_observation')

    def test_blank_200_is_not_a_usable_directory(self):
        observed = self.observe()
        observed['html'] = '<html><body></body></html>'
        self.assertFalse(normalize_observation(self.request(), observed).ok)

    def test_complete_html_and_locators_remain_unchanged(self):
        result = normalize_observation(self.request(), self.observe())
        self.assertTrue(result.ok)
        self.assertEqual(result.html, LIST)
        self.assertEqual(result.transport, 'crawl4ai')

    def test_html_limit_is_utf8_bytes_and_never_truncates(self):
        observed = self.observe()
        observed['html'] = '院' * 11
        with patch('backend.scraper.acquisition.crawl4ai_transport.get_policy',
                   return_value={'html_max_bytes': 32}):
            result = normalize_observation(self.request(), observed)
        self.assertEqual(result.error_code, 'response_too_large')
        self.assertEqual(result.html, '')

    def test_engine_failure_cannot_promote_valid_looking_partial_material(self):
        observed = self.observe()
        observed['success'] = False
        self.assertEqual(normalize_observation(self.request(), observed).error_code,
                         'crawl4ai_read_incomplete')

    def test_short_navigation_engine_heuristic_defers_to_actual_dom_validation(self):
        observed = self.observe()
        observed.update(success=False, error_message='Blocked by anti-bot protection: '
            'Structural: minimal_text on small page (209 bytes, 25 chars visible)')
        result = normalize_observation(self.request(), observed)
        self.assertTrue(result.ok)
        self.assertIn('engine_minimal_text_heuristic', result.evidence)

    def test_engine_heuristic_does_not_override_missing_requested_region(self):
        observed = self.observe()
        observed.update(success=False, error_message='Blocked by anti-bot protection: '
            'Structural: minimal_text on small page (209 bytes, 25 chars visible)')
        result = normalize_observation(self.request(readiness_selector='.missing'), observed)
        self.assertFalse(result.ok)

    def test_engine_heuristic_does_not_override_real_challenge(self):
        observed = self.observe()
        observed.update(success=False, html='<p>请输入验证码</p>',
            error_message='Blocked by anti-bot protection: Structural: minimal_text on small page (20 bytes, 5 chars visible)')
        self.assertFalse(normalize_observation(self.request(), observed).ok)

    def test_redirect_to_local_address_is_rejected(self):
        observed = self.observe(redirected_url='http://127.0.0.1/private')
        self.assertEqual(normalize_observation(self.request(), observed).error_code,
                         'blocked_destination')

    def test_article_reading_is_outside_this_experiment(self):
        result = normalize_observation(FetchRequest(BASE, purpose='article'), self.observe())
        self.assertEqual(result.error_code, 'crawl4ai_diagnostic_scope')

    def test_rendered_shell_is_incomplete_not_a_second_render_request(self):
        observed = self.observe()
        observed['html'] = '<div id="app"></div><script src="/app.js"></script>'
        result = normalize_observation(self.request(), observed)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.outcome, 'requires_render')

    def test_fixture_origin_must_be_explicit_numeric_loopback(self):
        for origin in ('https://example.edu.cn', 'http://localhost:1234',
                       'http://127.0.0.1', 'http://127.0.0.1:1234/path'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                fixture_validator(origin)

    def test_fixture_validation_rejects_egress_and_other_ports(self):
        validate = fixture_validator('http://127.0.0.1:1234')
        validate('http://127.0.0.1:1234/units?q=1')
        for address in ('http://127.0.0.1:1235/', 'https://example.edu.cn/',
                        'file:///private', 'http://x:secret@127.0.0.1:1234/'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate(address)

    def test_missing_optional_dependency_is_explicit_and_does_not_install(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Crawl4AIFixtureTransport(fixture_origin='http://127.0.0.1:1234',
                                                 base_directory=folder)
            with patch.dict(os.environ), patch(
                    'backend.scraper.acquisition.crawl4ai_transport.importlib.import_module',
                    side_effect=ImportError):
                result = asyncio.run(transport.fetch(FetchRequest('http://127.0.0.1:1234/')))
            self.assertEqual(result.error_code, 'crawl4ai_not_installed')

    def test_fixture_article_request_is_rejected_before_engine_import(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Crawl4AIFixtureTransport(fixture_origin='http://127.0.0.1:1234',
                                                 base_directory=folder)
            with patch('backend.scraper.acquisition.crawl4ai_transport.importlib.import_module',
                       side_effect=AssertionError('Article must not start this engine')):
                result = asyncio.run(transport.fetch(FetchRequest('http://127.0.0.1:1234/', purpose='article')))
            self.assertEqual(result.error_code, 'crawl4ai_diagnostic_scope')

    def test_browser_disallowed_request_is_rejected_before_engine_import(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Crawl4AIFixtureTransport(fixture_origin='http://127.0.0.1:1234',
                                                 base_directory=folder)
            with patch('backend.scraper.acquisition.crawl4ai_transport.importlib.import_module',
                       side_effect=AssertionError('Disabled browser must not start')):
                result = asyncio.run(transport.fetch(FetchRequest('http://127.0.0.1:1234/', browser_allowed=False)))
            self.assertEqual(result.error_code, 'browser_not_allowed')

    def test_preimported_engine_cannot_reuse_another_global_database(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Crawl4AIFixtureTransport(fixture_origin='http://127.0.0.1:1234',
                                                 base_directory=folder)
            with patch.dict(sys.modules, {'crawl4ai': SimpleNamespace(),
                  'crawl4ai.async_database': SimpleNamespace(DB_PATH=str(Path(folder).parent / 'user.db'))}):
                result = asyncio.run(transport.fetch(FetchRequest('http://127.0.0.1:1234/')))
            self.assertEqual(result.error_code, 'crawl4ai_storage_not_isolated')


class Crawl4AIPublicDiagnosticTests(unittest.TestCase):
    def test_public_comparison_is_opt_in_and_cannot_import_an_engine_without_a_broker(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                Crawl4AIPublicDiagnosticTransport(base_directory=folder)
            transport = Crawl4AIPublicDiagnosticTransport(base_directory=folder, allow_public_diagnostics=True)
            with patch.dict(os.environ, {'WATCHER_ORIGIN_BROKER_URL': '', 'WATCHER_BROWSER_TOKEN': ''}), \
                 patch('backend.scraper.acquisition.crawl4ai_transport.importlib.import_module',
                       side_effect=AssertionError('No broker must not start a browser')):
                result = asyncio.run(transport.fetch(FetchRequest(BASE)))
            self.assertEqual(result.error_code, 'crawl4ai_broker_required')

    def test_article_and_disabled_browser_are_rejected_before_engine_import(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Crawl4AIPublicDiagnosticTransport(base_directory=folder, allow_public_diagnostics=True)
            with patch('backend.scraper.acquisition.crawl4ai_transport.importlib.import_module',
                       side_effect=AssertionError('Out-of-scope reads must not start an engine')):
                self.assertEqual(asyncio.run(transport.fetch(FetchRequest(BASE, purpose='article'))).error_code,
                                 'crawl4ai_diagnostic_scope')
                self.assertEqual(asyncio.run(transport.fetch(FetchRequest(BASE, browser_allowed=False))).error_code,
                                 'browser_not_allowed')

    def test_wrong_imported_engine_database_is_never_reused_for_a_public_comparison(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                {'WATCHER_ORIGIN_BROKER_URL': 'http://127.0.0.1:1234/internal/browser-origin',
                 'WATCHER_BROWSER_TOKEN': 't' * 64, 'CRAWL4_AI_BASE_DIRECTORY': folder}), \
             patch.dict(sys.modules, {'crawl4ai': SimpleNamespace(),
                 'crawl4ai.async_database': SimpleNamespace(DB_PATH=str(Path(folder).parent / 'user.db'))}):
            transport = Crawl4AIPublicDiagnosticTransport(base_directory=folder, allow_public_diagnostics=True)
            result = asyncio.run(transport.fetch(FetchRequest(BASE)))
            self.assertEqual(result.error_code, 'crawl4ai_storage_not_isolated')


if __name__ == '__main__':
    unittest.main()
