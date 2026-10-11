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
DYNAMIC_PENDING = (Path(__file__).parent / 'fixtures/acquisition/dynamic-publication-template.html').read_text(encoding='utf8')


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

    async def test_dated_placeholder_dom_keeps_waiting_for_actual_targets(self):
        reads = []
        async def content(page):
            reads.append(True)
            return DYNAMIC_PENDING if len(reads) < 3 else HTML
        with patch.object(FakePage, 'content', content):
            await self.runtime.submit(self.payload())
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done')
        self.assertGreaterEqual(len(reads), 3)
        self.assertIn('课程安排', value['result']['html'])
        self.assertNotIn('本科招生报名', value['result']['html'])

    async def test_render_deadline_returns_typed_incomplete_material_not_success(self):
        async def content(page): return DYNAMIC_PENDING
        with patch.object(FakePage, 'content', content):
            await self.runtime.submit(dict(self.payload(), timeout_seconds=1))
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done')
        self.assertEqual(value['result']['outcome'], 'needs_adapter')
        self.assertEqual(value['result']['error_code'], 'dynamic_material_incomplete')
        self.assertIn('render_wait_exhausted', value['result']['evidence'])

    async def test_partial_material_survives_slow_final_validation(self):
        started = time.monotonic()
        async def validator(url):
            await valid_public(url)
            if time.monotonic() - started > .8:
                await asyncio.sleep(.2)
        self.runtime.validator = validator
        async def content(page): return DYNAMIC_PENDING
        with patch.object(FakePage, 'content', content):
            await self.runtime.submit(dict(self.payload(), timeout_seconds=1))
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done', value)
        self.assertEqual(value['result']['error_code'], 'dynamic_material_incomplete')
        self.assertEqual(value['result']['html'], DYNAMIC_PENDING)

    async def test_partial_material_survives_bounded_cleanup_after_read_deadline(self):
        async def content(page): return DYNAMIC_PENDING
        async def close(page):
            await asyncio.sleep(.2)
            page.closed = True
        with patch.object(FakePage, 'content', content), patch.object(FakePage, 'close', close):
            await self.runtime.submit(dict(self.payload(), timeout_seconds=1))
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done', value)
        self.assertEqual(value['result']['error_code'], 'dynamic_material_incomplete')
        self.assertTrue(self.browsers[0].contexts[0].pages[0].closed)
        self.assertFalse(self.runtime.admissions)

    async def test_own_deadline_preserves_last_valid_partial_snapshot(self):
        reads = 0
        async def content(page):
            nonlocal reads
            reads += 1
            if reads > 1:
                await asyncio.sleep(2)
            return DYNAMIC_PENDING
        with patch.object(FakePage, 'content', content):
            await self.runtime.submit(dict(self.payload(), timeout_seconds=1))
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done', value)
        result = value['result']
        self.assertEqual(result['error_code'], 'dynamic_material_incomplete')
        self.assertEqual(result['html'], DYNAMIC_PENDING)
        self.assertEqual(result['final_url'], self.payload()['url'])
        self.assertIn('render_wait_exhausted', result['evidence'])

    async def test_partial_snapshot_cannot_hide_cancel_invalid_navigation_or_oversize(self):
        from backend.services.discovery_policy import get_policy
        for outcome in ('cancel', 'invalid_navigation', 'new_navigation', 'oversize'):
            with self.subTest(outcome=outcome):
                started = asyncio.Event()
                reads = 0
                async def content(page):
                    nonlocal reads
                    reads += 1
                    if reads > 1:
                        started.set()
                        if outcome == 'invalid_navigation':
                            page.url = 'http://127.0.0.1/private'
                        if outcome == 'new_navigation' and 'request' in page.listeners:
                            class Request:
                                frame = page.main_frame
                                def is_navigation_request(self): return True
                            page.listeners['request'](Request())
                        if outcome == 'oversize':
                            return DYNAMIC_PENDING * 3
                        await asyncio.sleep(2)
                    return DYNAMIC_PENDING
                payload = dict(self.payload(outcome), timeout_seconds=1)
                policy = dict(get_policy(), html_max_bytes=len(DYNAMIC_PENDING.encode('utf8')) * 2)
                with patch.object(FakePage, 'content', content), \
                     patch('backend.browser_service.runtime.get_policy', return_value=policy):
                    await self.runtime.submit(payload)
                    if outcome == 'cancel':
                        await started.wait()
                        self.runtime.executions[outcome].task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await self.done(outcome)
                        self.assertIsNone(self.runtime.executions[outcome].result)
                    else:
                        value = await self.done(outcome)
                        self.assertEqual(value['state'], 'failed', value)
                        self.assertNotIn('result', value)
                        if outcome == 'oversize':
                            self.assertEqual(value['error']['code'], 'response_too_large')
                self.assertFalse(self.runtime.admissions)

    async def test_stalled_page_cleanup_still_releases_permit_and_discards_context(self):
        from backend.browser_service.admission import OriginAdmission
        releases = []
        original = FakePage.goto
        async def goto(page, url, **kwargs):
            result = await original(page, url, **kwargs)
            self.runtime.admissions[id(page.context)].tokens.add('a' * 64)
            return result
        async def close(page): await asyncio.Event().wait()
        async def broker(admission, action, data):
            if action == 'release': releases.append(data['token'])
            return {'released': True}
        started = time.monotonic()
        with patch.dict('os.environ', {'WATCHER_ORIGIN_BROKER_URL': 'http://fixture.invalid'}), \
             patch('backend.browser_service.runtime._CLEANUP_TIMEOUT_SECONDS', .03, create=True), \
             patch.object(FakePage, 'goto', goto), patch.object(FakePage, 'close', close), \
             patch.object(OriginAdmission, '_request', broker):
            await self.runtime.submit(dict(self.payload(), timeout_seconds=1))
            value = await self.done('job-1')
        self.assertEqual(value['state'], 'done', value)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(releases, ['a' * 64])
        self.assertTrue(self.browsers[0].contexts[0].closed)
        self.assertFalse(self.runtime.admissions)
        self.assertFalse(self.runtime.sessions)

    async def test_cancellation_during_cleanup_is_not_a_success_and_isolates_page(self):
        from backend.browser_service.admission import OriginAdmission
        closing, releasing = asyncio.Event(), asyncio.Event()
        releases = []
        original = FakePage.goto
        async def goto(page, url, **kwargs):
            result = await original(page, url, **kwargs)
            self.runtime.admissions[id(page.context)].tokens.add('a' * 64)
            return result
        async def broker(admission, action, data):
            if action == 'release':
                releases.append(data['token'])
                if len(releases) == 1:
                    releasing.set()
                    await asyncio.Event().wait()
            return {'released': True}
        async def close(page):
            closing.set()
            await asyncio.Event().wait()
        with patch.dict('os.environ', {'WATCHER_ORIGIN_BROKER_URL': 'http://fixture.invalid'}), \
             patch.object(FakePage, 'goto', goto), patch.object(FakePage, 'close', close), \
             patch.object(OriginAdmission, '_request', broker):
            await self.runtime.submit(self.payload())
            await asyncio.gather(closing.wait(), releasing.wait())
            self.runtime.executions['job-1'].task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self.done('job-1')
        self.assertIsNone(self.runtime.executions['job-1'].result)
        self.assertTrue(self.browsers[0].contexts[0].closed)
        self.assertEqual(releases, ['a' * 64, 'a' * 64])
        self.assertFalse(self.runtime.admissions)
        self.assertFalse(self.runtime.sessions)

    async def test_closing_page_cannot_start_unadmitted_http_or_socket_requests(self):
        routes = []
        class Route:
            class request: url = 'https://example.edu/late-resource'
            url = 'wss://example.edu/late-socket'
            aborted = continued = False
            async def abort(self, code): self.aborted = True
            async def continue_(self): self.continued = True
            async def close(self): self.aborted = True
            def connect_to_server(self): self.continued = True
        async def close(page):
            for handler in (page.context.route_handler, page.context.socket_handler):
                route = Route()
                routes.append(route)
                await handler(route)
            page.closed = True
        with patch.object(FakePage, 'close', close):
            await self.runtime.submit(self.payload())
            self.assertEqual((await self.done('job-1'))['state'], 'done')
        self.assertTrue(all(route.aborted and not route.continued for route in routes))

    async def test_guard_cannot_continue_a_resource_with_a_replaced_navigation_permit(self):
        from backend.browser_service.admission import OriginAdmission
        await self.runtime.submit(self.payload())
        await self.done('job-1')
        context = self.browsers[0].contexts[0]
        class Route:
            class request: url = 'https://example.edu/late-resource'
            url = 'wss://example.edu/late-socket'
            aborted = continued = False
            async def abort(self, code): self.aborted = True
            async def continue_(self): self.continued = True
            async def close(self): self.aborted = True
            def connect_to_server(self): self.continued = True
        for handler in (context.route_handler, context.socket_handler):
            entered, released = asyncio.Event(), asyncio.Event()
            async def admit(url):
                entered.set()
                await released.wait()
            old = OriginAdmission(self.payload())
            old.admit = admit
            self.runtime.admissions[id(context)] = old
            route = Route()
            task = asyncio.create_task(handler(route))
            await entered.wait()
            self.runtime.admissions[id(context)] = OriginAdmission(self.payload('replacement'))
            released.set()
            await task
            self.assertTrue(route.aborted)
            self.assertFalse(route.continued)

    async def test_finished_origin_busy_retries_only_after_delay_and_poll_never_restarts(self):
        from backend.browser_service.admission import OriginBusy
        original = FakePage.goto
        navigations = 0
        async def once_busy(page, url, **kwargs):
            nonlocal navigations
            navigations += 1
            response = await original(page, url, **kwargs)
            if navigations == 1:
                self.runtime.admission_errors[id(page.context)] = OriginBusy(2)
            return response
        with patch.object(FakePage, 'goto', once_busy):
            await self.runtime.submit(self.payload())
            first = await self.done('job-1')
            self.assertEqual(first['result']['error_code'], 'origin_busy')
            self.assertEqual((await self.runtime.submit(self.payload()))['result']['error_code'], 'origin_busy')
            self.runtime.executions['job-1'].touched -= 2.1
            self.assertEqual(self.runtime.get_execution('job-1')['result']['error_code'], 'origin_busy')
            self.assertEqual(navigations, 1)
            await asyncio.gather(self.runtime.submit(self.payload()), self.runtime.submit(self.payload()))
            finished = await self.done('job-1')
            self.assertEqual(finished['state'], 'done')
            self.assertNotIn('error_code', finished['result'])
            self.assertEqual(navigations, 2)
            await self.runtime.submit(self.payload())
            self.assertEqual(navigations, 2)

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
