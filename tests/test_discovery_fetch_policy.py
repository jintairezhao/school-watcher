"""Shared permits and real slow HTTP streams obey the discovery trial policy."""
import gzip
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import socketserver
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from backend.scraper.acquisition import FetchRequest
from backend.scraper.acquisition.http_transport import fetch_http
from backend.services.discovery_policy import get_policy
from backend.services import runtime_leases, tasks
from backend.database.db import db
from backend.database.models import BackgroundTask, AppConfig
import test_shared_fetch_runtime as fixtures
import test_browser_runtime as browser_fixtures


class _SlowPages(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            if self.path == '/notice.pdf':
                payload = b'%PDF-1.7\nfixture\n%%EOF\n'
                self.send_response(200); self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Length', str(len(payload))); self.end_headers()
                self.wfile.write(payload); return
            if self.path == '/disconnect':
                self.server.disconnect_requests = getattr(self.server, 'disconnect_requests', 0) + 1
                self.close_connection = True
                return
            if self.path == '/headers':
                self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                for _ in range(80):
                    self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.025)
                return
            if self.path == '/body':
                self.send_response(200); self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', '10000'); self.end_headers()
                for _ in range(80):
                    self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.025)
                return
            if self.path == '/idle':
                self.send_response(200); self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', '100'); self.end_headers()
                self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.6)
                return
            if self.path.startswith('/slow-redirect/'):
                time.sleep(.09)
                target = '/slow-redirect/' + str(int(self.path.rsplit('/', 1)[1]) + 1)
            elif self.path.startswith('/redirect/'):
                target = '/redirect/' + str(int(self.path.rsplit('/', 1)[1]) + 1)
            elif self.path == '/loop':
                target = '/loop'
            else:
                target = None
            if target:
                self.send_response(302); self.send_header('Location', target); self.end_headers(); return
            payload = gzip.compress(b'<html>' + b'x' * 4096 + b'</html>')
            self.send_response(200); self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Encoding', 'gzip'); self.end_headers(); self.wfile.write(payload)
        except (ConnectionError, OSError):
            pass


class _StalledTLS(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(4096)
        time.sleep(.6)


class HTTPDeadlineTests(unittest.TestCase):
    def test_automatic_proxy_denial_yields_before_a_later_direct_attempt(self):
        from contextlib import ExitStack
        from backend.scraper.acquisition import classify_result
        url = 'http://www.example.edu.cn/info/1.htm'
        handle = {'attempts': 0, 'checkpoint': {}}
        sent = []
        class Response:
            headers = {'Content-Type': 'text/html', 'Retry-After': '90'}
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield b'<article>Public notification</article>'
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, target, **kwargs):
                sent.append(kwargs.get('proxy'))
                return Response(403 if kwargs.get('proxy') else 200)
        def remember(address):
            handle['checkpoint']['http_retry_direct'] = {'www.example.edu.cn': handle['attempts']}
        with ExitStack() as stack:
            for name, value in [('_shared_permit', None), ('_release_permit', None),
                                ('select_proxy', 'http://proxy.example:8080'),
                                ('public_addresses', ['8.8.8.8']), ('proxy_required', False)]:
                stack.enter_context(patch('backend.scraper.acquisition.http_transport.' + name, return_value=value))
            stack.enter_context(patch('backend.scraper.acquisition.http_transport.aiohttp.ClientSession', Session))
            stack.enter_context(patch('backend.scraper.acquisition.http_transport.remember_proxy_failure', side_effect=remember))
            with tasks.execution_scope(handle):
                request = FetchRequest(url, purpose='article')
                first = classify_result(request, fetch_http(request))
                self.assertEqual(sent, ['http://proxy.example:8080'])
                self.assertEqual(first.outcome, 'network_error')
                self.assertEqual(first.error_code, 'http_proxy_access_denied')
                self.assertEqual(first.retry_after, 90)
                handle['attempts'] = 1  # The durable scheduler owns the wait.
                second = classify_result(request, fetch_http(request))
                self.assertEqual(second.status, 200)
                self.assertEqual(sent, ['http://proxy.example:8080', None])
                self.assertIn('direct_on_durable_retry', second.evidence)
            # An explicitly required proxy cannot be bypassed, even on a retry.
            with patch('backend.scraper.acquisition.http_transport.proxy_required', return_value=True):
                with tasks.execution_scope(handle):
                    restricted = classify_result(request, fetch_http(request))
            self.assertEqual(restricted.outcome, 'denied')
            self.assertEqual(restricted.error_code, 'http_403')
            self.assertEqual(sent[-1], 'http://proxy.example:8080')

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _SlowPages)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self, path, dns=None, *, tls=False, port=None, purpose='directory'):
        policy = dict(get_policy(), http_total_seconds=.35, http_connect_seconds=.2,
                      http_read_seconds=.15, html_max_bytes=1024)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for target, value in [('get_policy', policy), ('_shared_permit', None),
                    ('_release_permit', None), ('validate_public_url', None), ('select_proxy', None)]:
                stack.enter_context(patch('backend.scraper.acquisition.http_transport.' + target, return_value=value))
            stack.enter_context(patch('backend.scraper.acquisition.http_transport.public_addresses',
                                     side_effect=dns or (lambda url: ['127.0.0.1'])))
            stack.enter_context(patch('backend.scraper.acquisition.http_transport._pinned_url', side_effect=lambda url, address: url))
            started = time.monotonic()
            scheme = 'https' if tls else 'http'
            result = fetch_http(FetchRequest(f'{scheme}://127.0.0.1:{port or self.server.server_port}{path}', purpose=purpose))
            return result, time.monotonic() - started

    def test_pdf_bytes_are_read_with_shared_http_limits_only_for_article_samples(self):
        result, _ = self.request('/notice.pdf', purpose='article')
        self.assertEqual(result.status, 200)
        self.assertTrue(result.attachment['verified'])
        self.assertEqual(result.attachment['bytes'], len(b'%PDF-1.7\nfixture\n%%EOF\n'))
        self.assertFalse(result.html)
        unsupported, _ = self.request('/notice.pdf')
        self.assertEqual(unsupported.error_code, 'non_html_resource')

    def test_steady_slow_headers_and_body_cannot_extend_total_deadline(self):
        for path in ('/headers', '/body', '/slow-redirect/0'):
            with self.subTest(path=path):
                result, elapsed = self.request(path)
                self.assertEqual(result.error_code, 'http_budget_exhausted')
                self.assertLess(elapsed, .7)

    def test_dns_is_bounded_independently_and_late_resolution_cannot_send(self):
        def slow_dns(url):
            time.sleep(.6)
            return ['127.0.0.1']
        result, elapsed = self.request('/gzip', slow_dns)
        self.assertEqual(result.error_code, 'http_connect_timeout')
        self.assertLess(elapsed, .45)

    def test_idle_body_uses_read_timeout_before_total_deadline(self):
        result, elapsed = self.request('/idle')
        self.assertEqual(result.error_code, 'http_idle_timeout')
        self.assertLess(elapsed, .3)

    def test_stalled_tls_handshake_has_connect_timeout_not_total_or_idle(self):
        with socketserver.ThreadingTCPServer(('127.0.0.1', 0), _StalledTLS) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                result, elapsed = self.request('/', tls=True, port=server.server_address[1])
                self.assertEqual(result.error_code, 'http_connect_timeout')
                self.assertLess(elapsed, .35)
            finally:
                server.shutdown(); thread.join()

    def test_disconnect_does_not_trigger_aiohttp_hidden_idempotent_resend(self):
        before = getattr(self.server, 'disconnect_requests', 0)
        result, _ = self.request('/disconnect')
        self.assertEqual(result.error_code, 'http_serverdisconnectederror')
        self.assertEqual(self.server.disconnect_requests - before, 1)

    def test_redirect_loops_and_long_chains_stop_explicitly(self):
        self.assertEqual(self.request('/loop')[0].error_code, 'redirect_loop')
        self.assertEqual(self.request('/redirect/0')[0].error_code, 'redirect_limit')

    def test_limit_counts_decompressed_html_and_preserves_failure_reason(self):
        result, _ = self.request('/gzip')
        self.assertEqual(result.error_code, 'response_too_large')
        self.assertEqual(result.outcome, 'denied')
        self.assertEqual(result.html, '')

    def test_public_address_pinning_preserves_host_tls_name_and_timeout_components(self):
        captured = {}
        class Response:
            status = 200
            headers = {'Content-Type': 'text/html'}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield b'<html>hello</html>'
        class Session:
            def __init__(self, **kwargs): captured['session'] = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url, **kwargs):
                captured.update(url=url, **kwargs)
                return Response()
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, value in [('_shared_permit', None), ('_release_permit', None),
                                ('select_proxy', None), ('public_addresses', ['8.8.8.8'])]:
                stack.enter_context(patch('backend.scraper.acquisition.http_transport.' + name, return_value=value))
            stack.enter_context(patch('backend.scraper.acquisition.http_transport.aiohttp.ClientSession', Session))
            result = fetch_http(FetchRequest('https://www.example.edu.cn/notices?kind=student'))
        self.assertEqual(result.html, '<html>hello</html>')
        self.assertEqual(captured['url'], 'https://8.8.8.8/notices?kind=student')
        self.assertEqual(captured['headers']['Host'], 'www.example.edu.cn')
        self.assertEqual(captured['server_hostname'], 'www.example.edu.cn')
        self.assertFalse(captured['allow_redirects'])
        self.assertFalse(captured['session']['auto_decompress'])
        self.assertTrue(0 < captured['timeout'].connect <= 6)
        self.assertEqual(captured['timeout'].sock_read, 16)
        self.assertTrue(0 < captured['timeout'].total <= 45)


class BrowserPolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixtures.BrowserRuntimeTests.asyncSetUp
    asyncTearDown = browser_fixtures.BrowserRuntimeTests.asyncTearDown
    payload = browser_fixtures.BrowserRuntimeTests.payload
    done = browser_fixtures.BrowserRuntimeTests.done

    async def test_browser_has_hard_deadline_even_when_dom_read_never_finishes(self):
        async def slow_content(page):
            await asyncio.sleep(1)
            return browser_fixtures.HTML
        policy = dict(get_policy(), browser_total_seconds=.08)
        payload = dict(self.payload(), timeout_seconds=90)
        with patch('backend.browser_service.runtime.get_policy', return_value=policy), \
             patch.object(browser_fixtures.FakePage, 'content', slow_content):
            started = time.monotonic()
            await self.runtime.submit(payload)
            result = await self.done('job-1')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error']['code'], 'browser_timeout')
        self.assertLess(time.monotonic() - started, .3)
        self.assertTrue(self.browsers[0].contexts[0].pages[0].closed)
        self.assertFalse(self.runtime.admissions)

    async def test_browser_oversized_dom_returns_clear_failure_and_releases_page(self):
        with patch('backend.browser_service.runtime.get_policy', return_value=dict(get_policy(), html_max_bytes=32)):
            await self.runtime.submit(self.payload())
            result = await self.done('job-1')
        self.assertEqual(result['error']['code'], 'response_too_large')
        self.assertTrue(self.browsers[0].contexts[0].pages[0].closed)
        self.assertFalse(self.runtime.admissions)

    async def test_only_explicit_service_configuration_allows_ninety_second_trial(self):
        payload = dict(self.payload(), timeout_seconds=90)
        real_timeout = asyncio.timeout
        for configured, expected in [('{}', 45), ('{"browser_total_seconds":90}', 90)]:
            with patch.dict(os.environ, WATCHER_DISCOVERY_POLICY=configured), \
                 patch.object(self.runtime, '_navigate_page', AsyncMock(return_value={})), \
                 patch('backend.browser_service.runtime.asyncio.timeout', wraps=real_timeout) as timer:
                await self.runtime._navigate(None, payload)
                self.assertEqual(timer.call_args.args[0], expected)


class SharedPolicyTests(unittest.TestCase):
    setUp = fixtures.SharedRuntimeTests.setUp
    tearDown = fixtures.SharedRuntimeTests.tearDown
    database_uri = None
    job = fixtures.SharedRuntimeTests.job

    def test_non_task_http_uses_same_database_slots_and_host_interval(self):
        from backend.scraper.http_client import _shared_permit, _release_permit, FetchAdmissionBusy
        self.assertIsNone(tasks.current_execution())
        first, _ = runtime_leases.reserve_origin('https://browser.edu.cn/', 'browser', transport='browser')
        with self.assertRaises(FetchAdmissionBusy):
            _shared_permit('http://browser.edu.cn/', 45)
        second = _shared_permit('https://http.edu.cn/', 45)
        with self.assertRaises(FetchAdmissionBusy):
            _shared_permit('https://third.edu.cn/', 45)
        _release_permit(second)
        with self.assertRaises(FetchAdmissionBusy) as caught:
            _shared_permit('https://http.edu.cn/', 45)
        self.assertTrue(0 < caught.exception.retry_after <= 1)
        _release_permit(first)

    def test_policy_environment_is_shared_with_browser_process_and_app_can_override(self):
        with patch.dict(os.environ, WATCHER_DISCOVERY_POLICY='{"browser_total_seconds":90,"not_a_setting":999}'):
            code = 'import json; from backend.services.discovery_policy import get_policy; print(json.dumps(get_policy()))'
            result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, check=True, timeout=20)
            self.assertEqual(json.loads(result.stdout)['browser_total_seconds'], 90)
            self.assertNotIn('not_a_setting', json.loads(result.stdout))
            self.assertEqual(get_policy()['browser_total_seconds'], 90)
            self.app.config['DISCOVERY_POLICY'] = {'browser_total_seconds': 40}
            self.assertEqual(get_policy()['browser_total_seconds'], 40)

    def test_global_slots_are_shared_with_a_separate_process(self):
        first, _ = runtime_leases.reserve_origin('https://one.edu.cn/', 'parent')
        code = '''import json,sys
from backend import create_app
from backend.services.runtime_leases import reserve_origin
app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':sys.argv[1],'SOURCE_CATALOG_PATH':sys.argv[2]})
with app.app_context():
 print(json.dumps(reserve_origin('https://two.edu.cn/','child')[0]))
'''
        result = subprocess.run([sys.executable, '-c', code, self.app.config['SQLALCHEMY_DATABASE_URI'],
            str(self.root / 'child-catalog.db')], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=True, timeout=20)
        second = json.loads(result.stdout.strip())
        self.assertTrue(second)
        self.assertIsNone(runtime_leases.reserve_origin('https://three.edu.cn/', 'parent')[0])
        runtime_leases.release_origin(first['token'])
        self.assertTrue(runtime_leases.reserve_origin('https://three.edu.cn/', 'parent')[0])

    def test_browser_has_one_global_slot_and_redirects_share_it(self):
        first, _ = runtime_leases.reserve_origin('https://one.edu.cn/', 'browser', token='a' * 64, transport='browser')
        self.assertIsNone(runtime_leases.reserve_origin('https://two.edu.cn/', 'browser', transport='browser')[0])
        redirect, _ = runtime_leases.reserve_origin('https://redirect.edu.cn/', 'browser',
            token='b' * 64, global_token='a' * 64, transport='browser')
        self.assertTrue(redirect)
        self.assertTrue(runtime_leases.reserve_origin('https://http.edu.cn/', 'http')[0])
        runtime_leases.release_origin(first['token'])
        self.assertIsNone(runtime_leases.reserve_origin('https://two.edu.cn/', 'browser', transport='browser')[0])
        runtime_leases.release_origin(redirect['token'])
        self.assertTrue(runtime_leases.reserve_origin('https://two.edu.cn/', 'browser', transport='browser')[0])

    def test_retry_delays_and_six_hour_recovery_survive_completion(self):
        from datetime import datetime, timedelta
        from backend.scraper.fetch_errors import SourceNetworkError
        job = self.job()
        for attempt, expected in enumerate((30, 120, 21600), 1):
            db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == job.id).values(available_at=datetime.utcnow()))
            db.session.commit()
            handle = tasks.claim()
            with patch('random.uniform', return_value=1):
                self.assertTrue(tasks.finish(handle, error=SourceNetworkError('offline')))
            db.session.expire_all()
            row = db.session.get(BackgroundTask, job.id)
            self.assertAlmostEqual((row.available_at - row.checked_at).total_seconds(), expected, delta=.1)
            self.assertEqual(row.attempts, attempt)
            self.assertEqual(row.state, 'pending' if attempt < 3 else 'failed')
            self.assertGreaterEqual(row.next_run_at - row.checked_at, timedelta(hours=6))

    def test_daily_interval_keeps_user_setting_and_bounded_jitter(self):
        from backend.services.inbox_refresh import collection_interval_seconds, next_collection_interval_seconds
        self.assertEqual(collection_interval_seconds(), 1800)
        for _ in range(20):
            self.assertTrue(1620 <= next_collection_interval_seconds() <= 1980)
        AppConfig.set('scrape_interval', '10')
        self.assertEqual(collection_interval_seconds(), 600)
        for _ in range(20):
            self.assertTrue(540 <= next_collection_interval_seconds() <= 660)


class ProxyRetryTests(unittest.TestCase):
    setUp = fixtures.SharedRuntimeTests.setUp
    tearDown = fixtures.SharedRuntimeTests.tearDown
    database_uri = None
    job = fixtures.SharedRuntimeTests.job

    def transport(self, errors, routes):
        from contextlib import ExitStack
        class Response:
            status = 200
            headers = {'Content-Type': 'text/html'}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield b'<html>public notice</html>'
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url, **kwargs):
                assert self._retry_connection is False
                routes.append(kwargs.get('proxy'))
                error = errors.pop(0) if errors else None
                if error:
                    raise error
                return Response()
        stack = ExitStack()
        for name, value in [('_shared_permit', None), ('_release_permit', None),
                            ('select_proxy', 'http://proxy.invalid'), ('public_addresses', ['8.8.8.8'])]:
            stack.enter_context(patch('backend.scraper.acquisition.http_transport.' + name, return_value=value))
        stack.enter_context(patch('backend.scraper.acquisition.http_transport.aiohttp.ClientSession', Session))
        return stack

    def proxy_connect_error(self):
        import aiohttp
        return aiohttp.ClientProxyConnectionError(SimpleNamespace(host='proxy.invalid', port=80, ssl=False),
                                                 ConnectionRefusedError('refused before request'))

    def test_proven_unsent_proxy_connection_failure_can_fall_back_once(self):
        routes = []
        with self.transport([self.proxy_connect_error(), None], routes):
            result = fetch_http(FetchRequest('https://example.edu.cn/'))
        self.assertTrue(result.html)
        self.assertEqual(routes, ['http://proxy.invalid', None])
        self.assertIn('direct_after_unsent_proxy_failure:ClientProxyConnectionError', result.evidence)

    def test_explicit_proxy_requirement_prevents_even_unsent_route_switch(self):
        routes = []
        with self.transport([self.proxy_connect_error()], routes):
            result = fetch_http(FetchRequest('https://example.edu.cn/', policy={'proxy_required': True}))
        self.assertEqual(result.outcome, 'network_error')
        self.assertEqual(routes, ['http://proxy.invalid'])

    def test_three_durable_attempts_send_three_times_and_switch_route_only_after_retry(self):
        import aiohttp
        from datetime import datetime
        from backend.scraper.acquisition import FetchFailure
        job, routes = self.job(), []
        with self.transport([aiohttp.ServerDisconnectedError() for _ in range(3)], routes):
            for index, expected_delay in enumerate((30, 120, 21600), 1):
                db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == job.id).values(available_at=datetime.utcnow()))
                db.session.commit()
                handle = tasks.claim()
                with tasks.execution_scope(handle):
                    result = fetch_http(FetchRequest('https://example.edu.cn/'))
                self.assertEqual(result.error_code, 'http_serverdisconnectederror')
                self.assertEqual(len(routes), index)
                with patch('random.uniform', return_value=1):
                    tasks.finish(handle, error=FetchFailure(result))
                row = db.session.get(BackgroundTask, job.id, populate_existing=True)
                self.assertAlmostEqual((row.available_at - row.checked_at).total_seconds(), expected_delay, delta=.1)
            self.assertEqual(row.state, 'failed')
        self.assertEqual(routes, ['http://proxy.invalid', None, None])


if __name__ == '__main__':
    unittest.main()
