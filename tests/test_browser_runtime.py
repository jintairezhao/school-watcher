"""Offline execution, ownership, isolation and one-use verification transport tests."""
import asyncio
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.browser_service.runtime import BrowserRuntime, RuntimeFault
from backend.browser_service.server import create_service
from aiohttp.test_utils import TestClient, TestServer


HTML = '<html><body><main><h1>通知公告</h1><a href="/2026/notice.htm">关于新学期课程安排的重要通知</a><time>2026-09-23</time></main></body></html>'


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = ''
        self.closed = False
        self.main_frame = object()
        self.listeners = {}
        self.html = HTML
    def set_default_timeout(self, value): pass
    def set_default_navigation_timeout(self, value): pass
    def on(self, name, callback): self.listeners[name] = callback
    def remove_listener(self, name, callback): self.listeners.pop(name, None)
    async def goto(self, url, **kwargs):
        self.url = url
        self.context.visits.append(url)
        await asyncio.sleep(.01)
        page = self
        class Response:
            status = 200
            frame = page.main_frame
            class request:
                @staticmethod
                def is_navigation_request(): return True
            async def all_headers(self): return {'content-type': 'text/html'}
        result = Response()
        if 'response' in self.listeners: self.listeners['response'](result)
        return result
    async def content(self): return self.html
    async def wait_for_function(self, *args, **kwargs): pass
    async def close(self): self.closed = True


class FakeContext:
    def __init__(self):
        self.closed = False
        self.visits = []
        self.pages = []
        self.route_handler = None
    async def route(self, expression, handler): self.route_handler = handler
    async def route_web_socket(self, expression, handler): self.socket_handler = handler
    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page
    async def close(self): self.closed = True


class FakeBrowser:
    def __init__(self): self.contexts = []; self.closed = False
    def is_connected(self): return not self.closed
    async def new_context(self, **kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context
    async def close(self): self.closed = True


class FakeDisplay:
    def __init__(self): self.port = None; self.hidden = False; self.closed = False
    async def start(self): return {}
    async def expose(self): self.port = 9999
    async def hide(self): self.port = None; self.hidden = True
    async def close(self): self.closed = True; await self.hide()


async def valid_public(url):
    if not url.startswith('https://example.edu/'):
        raise ValueError('blocked')


class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browsers = []
        async def launch(*args):
            browser = FakeBrowser(); self.browsers.append(browser); return browser
        self.runtime = BrowserRuntime(launcher=launch, validator=valid_public, concurrency=2)
        self.display_patch = patch('backend.browser_service.runtime.PrivateDisplay', FakeDisplay)
        self.display_patch.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.display_patch.stop()

    def payload(self, ident='job-1', source='7'):
        return {'request_id': ident, 'url': 'https://example.edu/notices',
            'purpose': 'list', 'source_id': source, 'timeout_seconds': 2}

    async def done(self, ident):
        await self.runtime.executions[ident].task
        return self.runtime.get_execution(ident)

    async def manual(self, ident='verify-1', generation='secret'):
        payload = self.payload(); payload.update(id=ident, generation=generation)
        await self.runtime.open_manual(payload)
        return self.runtime.verifications[ident]

    async def test_duplicate_execution_reuses_same_browser_visit(self):
        await self.runtime.submit(self.payload())
        await self.runtime.submit(self.payload())
        self.assertEqual((await self.done('job-1'))['state'], 'done')
        self.assertEqual(self.browsers[0].contexts[0].visits, ['https://example.edu/notices'])
        self.assertTrue(self.browsers[0].contexts[0].pages[0].closed)

    async def test_idempotency_key_cannot_change_request(self):
        await self.runtime.submit(self.payload())
        with self.assertRaises(RuntimeFault) as caught:
            await self.runtime.submit({**self.payload(), 'url': 'https://example.edu/other'})
        self.assertEqual(caught.exception.status, 409)

    async def test_source_contexts_are_isolated_and_same_source_is_reused(self):
        await self.runtime.submit(self.payload('one', '1'))
        await self.runtime.submit(self.payload('two', '2'))
        await self.done('one'); await self.done('two')
        await self.runtime.submit(self.payload('three', '1')); await self.done('three')
        self.assertEqual(len(self.browsers[0].contexts), 2)
        self.assertEqual(len(self.browsers[0].contexts[0].visits), 2)

    async def test_public_url_rejected_before_browser_launch(self):
        with self.assertRaises(RuntimeFault):
            await self.runtime.submit({**self.payload(), 'url': 'http://127.0.0.1/admin'})
        self.assertFalse(self.browsers)

    async def test_subresource_private_request_aborts(self):
        await self.runtime.submit(self.payload()); await self.done('job-1')
        class Route:
            class request: url = 'http://127.0.0.1/secret'
            aborted = False
            async def abort(self, code): self.aborted = True
            async def continue_(self): raise AssertionError('private route escaped')
        route = Route()
        await self.browsers[0].contexts[0].route_handler(route)
        self.assertTrue(route.aborted)

    async def test_manual_slot_is_single_and_generation_fenced(self):
        await self.manual()
        with self.assertRaises(RuntimeFault) as caught:
            await self.manual('verify-2')
        self.assertEqual(caught.exception.code, 'verification_busy')
        with self.assertRaises(RuntimeFault):
            await self.runtime.end_manual('verify-1', 'old-secret')
        self.assertEqual(self.runtime.manual_status('verify-1', 'secret')['state'], 'active')

    async def test_manual_ticket_one_use_and_revoked_on_close(self):
        await self.manual()
        ticket = self.runtime.issue_ticket('verify-1', 'secret')['ticket']
        self.runtime.consume_ticket('verify-1', ticket)
        with self.assertRaises(RuntimeFault): self.runtime.consume_ticket('verify-1', ticket)
        ticket = self.runtime.issue_ticket('verify-1', 'secret')['ticket']
        await self.runtime.end_manual('verify-1', 'secret')
        with self.assertRaises(RuntimeFault): self.runtime.consume_ticket('verify-1', ticket)

    async def test_expiry_kills_view_and_retains_no_context(self):
        value = await self.manual()
        value['expires'] = time.monotonic() - 1
        await self.runtime.sweep()
        self.assertEqual(value['state'], 'expired')
        self.assertTrue(value['session'].display.closed)
        self.assertFalse(self.runtime.sessions)

    async def test_failed_manual_can_reopen_new_generation(self):
        await self.manual()
        await self.runtime.end_manual('verify-1', 'secret')
        await self.manual(generation='new')
        self.assertEqual(self.runtime.manual_status('verify-1', 'new')['state'], 'active')

    async def test_verified_session_reuses_context_and_revokes_view(self):
        value = await self.manual()
        context = value['session'].context
        from backend.scraper.acquisition import FetchResult
        with patch('backend.scraper.acquisition.classify_result', return_value=FetchResult(
                final_url='https://example.edu/notices', outcome='usable')):
            await self.runtime.verify_manual('verify-1', 'secret')
        self.assertEqual(value['state'], 'verified')
        self.assertTrue(value['session'].display.hidden)
        self.assertFalse(context.closed)
        await self.runtime.submit(self.payload()); await self.done('job-1')
        self.assertEqual(len(context.visits), 3)

    async def test_challenge_dom_cannot_be_confirmed_by_admin(self):
        value = await self.manual()
        from backend.scraper.acquisition import FetchResult
        with patch('backend.scraper.acquisition.classify_result', return_value=FetchResult(
                final_url='https://example.edu/notices', outcome='needs_manual')):
            with self.assertRaises(RuntimeFault) as caught:
                await self.runtime.verify_manual('verify-1', 'secret')
        self.assertEqual(caught.exception.code, 'verification_incomplete')
        self.assertEqual(value['state'], 'active')

    async def test_private_api_token_and_origin_are_required(self):
        client = TestClient(TestServer(create_service(self.runtime, token='s'*32,
            public_origin='https://watcher.example')))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/health')).status, 401)
            response = await client.get('/health', headers={'X-Watcher-Token': 's'*32})
            self.assertEqual(response.status, 200)
            response = await client.get('/v1/manual/id/websocket', headers={'Origin': 'https://attacker.example'})
            self.assertEqual(response.status, 403)
        finally:
            await client.close()


if __name__ == '__main__': unittest.main()
