"""Opt-in real Chromium tests. Only local fixture URLs bypass public-address checks.

Set WATCHER_REAL_BROWSER_EXECUTABLE to a test Chromium binary, or 'bundled' after
installing the pinned Playwright browser. No school or external network requests.
"""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestServer
from backend.browser_service.runtime import BrowserRuntime
from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result


class RealBrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        async def delayed(request):
            return web.Response(content_type='text/html', text='''<html><body><div id="app"></div>
                <script>setTimeout(()=>{document.querySelector('#app').innerHTML=
                '<ul class="notices"><li><a href="/article/42">新学期课程安排的重要通知</a>2026-09-23</li></ul>';},250);</script></body></html>''')
        async def challenge(request):
            return web.Response(status=403, content_type='text/html', headers={'cf-mitigated': 'challenge'},
                text='''<html><body>Checking your browser<script>setTimeout(()=>location.href='/delayed',200)</script></body></html>''')
        async def denied(request):
            return web.Response(status=403, content_type='text/html', text='<html><title>Access denied</title><body>Forbidden</body></html>')
        async def empty(request):
            return web.Response(content_type='text/html', text='<html><body><div class="empty">暂无通知</div></body></html>')
        async def api(request):
            return web.json_response({'title': '通过异步接口读取的教学安排通知'})
        async def xhr(request):
            return web.Response(content_type='text/html', text='''<div id="app"></div><script>
                fetch('/api/list').then(r=>r.json()).then(d=>{document.querySelector('#app').innerHTML=
                '<ul class="notices"><li><a href="/article/42">'+d.title+'</a></li></ul>'});</script>''')
        async def spa(request):
            return web.Response(content_type='text/html', text='''<div id="app"></div><script>
                if(location.hash==='#/notice')setTimeout(()=>{document.querySelector('#app').innerHTML=
                '<ul class="notices"><li><a href="#/article/42">前端路由中的学校课程安排通知</a></li></ul>'},100);</script>''')
        app.router.add_get('/delayed', delayed)
        app.router.add_get('/challenge', challenge)
        app.router.add_get('/denied', denied)
        app.router.add_get('/empty', empty)
        app.router.add_get('/api/list', api)
        app.router.add_get('/xhr', xhr)
        app.router.add_get('/spa', spa)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url('')).rstrip('/')
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        executable = os.environ.get('WATCHER_REAL_BROWSER_EXECUTABLE')
        channel = os.environ.get('WATCHER_TEST_BROWSER_CHANNEL')
        if not executable and not channel and not Path(self.playwright.chromium.executable_path).is_file():
            await self.playwright.stop()
            await self.server.close()
            self.skipTest('Install pinned Chromium or set WATCHER_TEST_BROWSER_CHANNEL')
        async def launch(headed, env):
            return await self.playwright.chromium.launch(headless=not headed, env=env or None,
                executable_path=None if executable in (None, 'bundled') else executable,
                channel=channel, chromium_sandbox=True)
        async def fixture_only(url):
            parsed = urlsplit(url)
            if not url.startswith(self.base + '/') or parsed.hostname != '127.0.0.1':
                raise ValueError('Only isolated fixture origin is allowed')
        self.runtime = BrowserRuntime(launcher=launch, validator=fixture_only)

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.playwright.stop()
        await self.server.close()

    async def fetch(self, path, **values):
        request = FetchRequest(url=self.base + path, purpose='list', request_id=path,
            timeout_seconds=2, **values)
        await self.runtime.submit(request.to_dict())
        await self.runtime.executions[path].task
        value = self.runtime.get_execution(path)
        self.assertEqual(value['state'], 'done', value)
        raw = FetchResult.from_dict(value['result'])
        return classify_result(request, raw)

    async def test_delayed_javascript_is_rendered(self):
        result = await self.fetch('/delayed', readiness_selector='.notices')
        self.assertEqual(result.outcome, 'usable')
        self.assertIn('新学期课程安排', result.html)

    async def test_challenge_reload_preserves_final_status_and_url(self):
        result = await self.fetch('/challenge', readiness_selector='.notices')
        self.assertEqual(result.outcome, 'usable')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, self.base + '/delayed')
        self.assertNotIn('cf-mitigated', result.headers)

    async def test_denial_is_not_empty_success(self):
        result = await self.fetch('/denied')
        self.assertEqual(result.outcome, 'denied')
        self.assertFalse(result.ok)

    async def test_explicit_empty_remains_distinct(self):
        result = await self.fetch('/empty', policy={'empty_selector': '.empty'})
        self.assertEqual(result.outcome, 'empty')

    async def test_xhr_response_populates_business_list(self):
        result = await self.fetch('/xhr', readiness_selector='.notices', expected_response_url='**/api/list')
        self.assertEqual(result.outcome, 'usable')
        self.assertIn('异步接口', result.html)

    async def test_frontend_route_fragment_is_preserved(self):
        result = await self.fetch('/spa#/notice', readiness_selector='.notices')
        self.assertEqual(result.outcome, 'usable')
        self.assertEqual(result.final_url, self.base + '/spa#/notice')

    @unittest.skipIf(os.name == 'nt', 'Linux Xvfb/TigerVNC acceptance')
    async def test_linux_manual_display_revocation_and_context_reuse(self):
        import shutil
        if not shutil.which('Xvfb') or not (shutil.which('X0tigervnc') or shutil.which('x0vncserver')):
            self.skipTest('Install Linux manual display dependencies')
        payload = FetchRequest(url=self.base + '/delayed', purpose='list', request_id='manual-fixture',
            readiness_selector='.notices', timeout_seconds=3).to_dict()
        payload.update(id='manual-fixture', generation='private-test-generation')
        state = await self.runtime.open_manual(payload)
        self.assertTrue(state['remote'])
        active = self.runtime.verifications['manual-fixture']
        context = active['session'].context
        port = active['session'].display.port
        server = active['session'].display.vnc
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        self.assertTrue((await reader.read(12)).startswith(b'RFB '))
        writer.close(); await writer.wait_closed()
        ticket = self.runtime.issue_ticket('manual-fixture', payload['generation'])['ticket']
        await self.runtime.verify_manual('manual-fixture', payload['generation'])
        self.assertIsNone(active['session'].display.port)
        self.assertIsNotNone(server.returncode)
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)
        from backend.browser_service.runtime import RuntimeFault
        with self.assertRaises(RuntimeFault):
            self.runtime.consume_ticket('manual-fixture', ticket)
        reused = await self.runtime._session(payload)
        self.assertIs(reused.context, context)


if __name__ == '__main__': unittest.main()
