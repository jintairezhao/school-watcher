"""One event loop owns every Playwright object; jobs are transient executions only."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlsplit
from aiohttp import ClientError

from backend.scraper.http_client import validate_public_url
from .display import PrivateDisplay
from .admission import OriginAdmission, OriginBusy


class RuntimeFault(Exception):
    def __init__(self, code, message, status=400, retryable=False):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable

    def public(self):
        return {'code': self.code, 'message': str(self), 'retryable': self.retryable}


def origin_of(url):
    parsed = urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc.lower()}'


@dataclass
class BrowserSession:
    key: str
    origin: str
    context: object
    browser: object = None
    display: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)
    page: object = None


@dataclass
class Execution:
    fingerprint: str
    payload: dict
    state: str = 'queued'
    result: dict | None = None
    error: dict | None = None
    touched: float = field(default_factory=time.monotonic)
    task: object = None

    def public(self):
        value = {'request_id': self.payload['request_id'], 'state': self.state}
        if self.result is not None:
            value['result'] = self.result
        if self.error is not None:
            value['error'] = self.error
        return value


class BrowserRuntime:
    def __init__(self, *, concurrency=1, max_sessions=8, max_executions=128,
                 session_ttl=1800, verification_ttl=600, validator=None, launcher=None):
        self.concurrency = max(1, concurrency)
        self.slots = asyncio.Semaphore(self.concurrency)
        self.max_sessions = max(1, max_sessions)
        self.max_executions = max(1, max_executions)
        self.session_ttl, self.verification_ttl = session_ttl, verification_ttl
        self.validator = validator or self._public_url
        self.launcher = launcher
        self.playwright = self.browser = None
        self.boot_id = secrets.token_hex(16)
        self.sessions, self.executions, self.verifications, self.tickets = {}, {}, {}, {}
        self.operation_deadlines = {}
        self.admissions = {}
        self.admission_errors = {}
        self.session_guard = asyncio.Lock()
        self.manual_guard = asyncio.Lock()
        self.closed = False

    async def _public_url(self, url):
        await asyncio.to_thread(validate_public_url, url)

    async def _launch(self, headed=False, env=None):
        if self.launcher:
            return await self.launcher(headed, env)
        if self.playwright is None:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
        # Browser sandbox is deliberately enabled; container needs documented seccomp policy.
        args = ['--disable-dev-shm-usage']
        proxy = os.environ.get('WATCHER_BROWSER_EGRESS_PROXY')
        if proxy:
            args.append('--proxy-bypass-list=<-loopback>')
        channel = os.environ.get('WATCHER_BROWSER_CHANNEL') or None
        if channel not in (None, 'chrome', 'msedge'):
            raise RuntimeFault('browser_configuration', '浏览器通道配置不正确')
        return await self.playwright.chromium.launch(headless=not headed, env=env,
            chromium_sandbox=True, args=args, channel=channel,
            proxy={'server': proxy} if proxy else None)

    async def _guard_context(self, context):
        async def limit_pages(page):
            if len(context.pages) > 1:
                await page.close()
        if hasattr(context, 'on'):
            context.on('page', limit_pages)
        async def guard(route):
            url = route.request.url
            try:
                await self.validator(url)
                admission = self.admissions.get(id(context))
                if admission:
                    await admission.admit(url)
            except OriginBusy as exc:
                self.admission_errors[id(context)] = exc
                await route.abort('blockedbyclient')
                return
            except (ValueError, OSError, ClientError, TimeoutError):
                await route.abort('blockedbyclient')
                return
            await route.continue_()
        await context.route('**/*', guard)
        # Official pages may use public WebSockets. Validate their HTTP-equivalent origin.
        async def guard_socket(route):
            url = route.url.replace('wss:', 'https:', 1).replace('ws:', 'http:', 1)
            try:
                await self.validator(url)
                admission = self.admissions.get(id(context))
                if admission:
                    await admission.admit(url)
            except OriginBusy as exc:
                self.admission_errors[id(context)] = exc
                await route.close()
                return
            except (ValueError, OSError, ClientError, TimeoutError):
                await route.close()
                return
            route.connect_to_server()
        if hasattr(context, 'route_web_socket'):
            await context.route_web_socket('**/*', guard_socket)

    def _session_key(self, payload):
        return str(payload.get('source_id') or origin_of(payload['url'])) + '|' + origin_of(payload['url'])

    async def _session(self, payload, headed=False):
        key = self._session_key(payload)
        async with self.session_guard:
            current = self.sessions.get(key)
            owner = (current.browser or self.browser) if current else None
            if current and owner and not owner.is_connected():
                with suppress(Exception):
                    await self._close_session(current)
                self.sessions.pop(key, None)
                current = None
            if current and (not headed or current.display):
                current.touched = time.monotonic()
                return current
            if current:
                if current.lock.locked():
                    raise RuntimeFault('session_busy', '该来源正在更新，请稍后处理验证', 409)
                await self._close_session(current)
                self.sessions.pop(key, None)
            if len(self.sessions) >= self.max_sessions:
                candidates = [s for s in self.sessions.values() if not s.lock.locked() and not self._active_session(s)]
                if not candidates:
                    raise RuntimeFault('browser_busy', '浏览器资源使用中，请稍后重试', 503, True)
                oldest = min(candidates, key=lambda s: s.touched)
                await self._close_session(oldest)
                del self.sessions[oldest.key]
            display = PrivateDisplay() if headed else None
            browser = None
            try:
                if headed:
                    env = await display.start()
                    browser = await self._launch(True, env or None)
                else:
                    if self.browser is None or not self.browser.is_connected():
                        self.browser = await self._launch()
                    browser = self.browser
                context = await browser.new_context(accept_downloads=False, service_workers='block',
                    viewport={'width': 1280, 'height': 900})
                await self._guard_context(context)
            except Exception:
                if headed and browser:
                    await browser.close()
                if display:
                    await display.close()
                raise
            current = BrowserSession(key, origin_of(payload['url']), context,
                browser if headed else None, display)
            self.sessions[key] = current
            return current

    def _active_session(self, session):
        return any(v['session'] is session and v['state'] == 'active' for v in self.verifications.values())

    async def submit(self, payload):
        payload = dict(payload)
        ident = payload.get('request_id')
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise RuntimeFault('invalid_request', '必须提供有效的执行标识')
        if payload.get('purpose') not in ('directory', 'list', 'article'):
            raise RuntimeFault('invalid_request', '抓取用途不正确')
        try:
            await self.validator(payload.get('url', ''))
            timeout = float(payload.get('timeout_seconds', 45))
        except (ValueError, TypeError, OSError) as exc:
            raise RuntimeFault('invalid_url', '请输入可公开访问的官网地址') from exc
        payload['timeout_seconds'] = max(1, min(timeout, 90))
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        existing = self.executions.get(ident)
        if existing:
            if existing.fingerprint != fingerprint:
                raise RuntimeFault('request_conflict', '执行标识已用于其他请求', 409)
            return existing.public()
        await self.sweep()
        if sum(e.state in ('queued', 'running') for e in self.executions.values()) >= self.concurrency:
            # Resource waiting remains in the durable worker queue and consumes
            # no navigation time budget or failure attempts.
            raise RuntimeFault('browser_busy', '浏览器资源使用中，请稍后重试', 503, True)
        if len(self.executions) >= self.max_executions:
            raise RuntimeFault('browser_busy', '浏览器任务已满，请稍后重试', 503, True)
        execution = Execution(fingerprint, payload)
        self.executions[ident] = execution
        execution.task = asyncio.create_task(self._execute(execution))
        return execution.public()

    def get_execution(self, ident):
        execution = self.executions.get(ident)
        if execution is None:
            raise RuntimeFault('execution_missing', '执行记录已失效，可以重新提交', 404, True)
        return execution.public()

    async def _execute(self, execution):
        try:
            async with self.slots:
                execution.state = 'running'
                session = await self._session(execution.payload)
                if self._active_session(session):
                    raise RuntimeFault('verification_required', '请先完成该来源的访问验证', 409)
                async with session.lock:
                    execution.result = await self._navigate(session, execution.payload)
                execution.state = 'done'
        except RuntimeFault as exc:
            execution.state, execution.error = 'failed', exc.public()
        except Exception as exc:
            # No raw exception/URLs/cookies are sent to clients or logs.
            code = 'browser_timeout' if 'timeout' in type(exc).__name__.lower() else 'browser_failed'
            execution.state = 'failed'
            execution.error = {'code': code, 'message': '浏览器未能完成本次访问', 'retryable': True}
        finally:
            execution.touched = time.monotonic()
            self._trim_results()

    def _trim_results(self):
        completed = sorted((e for e in self.executions.values() if e.result), key=lambda e: e.touched)
        total = sum(len((e.result.get('html') or '').encode('utf-8')) for e in completed)
        while completed and total > 64 * 1024 * 1024:
            execution = completed.pop(0)
            total -= len((execution.result.get('html') or '').encode('utf-8'))
            self.executions.pop(execution.payload['request_id'], None)

    async def _navigate(self, session, payload, keep_page=False, wait_for_content=True):
        start = time.monotonic()
        timeout = max(1000, min(float(payload.get('timeout_seconds', 45)) * 1000, 90000))
        operation_id = secrets.token_hex(16)
        self.operation_deadlines[operation_id] = start + timeout / 1000 + 15
        old_admission = self.admissions.pop(id(session.context), None)
        if old_admission:
            await old_admission.close()
        self.admission_errors.pop(id(session.context), None)
        admission = OriginAdmission(payload, ttl=650 if keep_page else int(timeout / 1000) + 40)
        self.admissions[id(session.context)] = admission
        page = session.page or await session.context.new_page()
        session.page = page
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(timeout)
        final_document = [None]
        def observe_document(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                final_document[0] = response
        page.on('response', observe_document)
        try:
            expected = payload.get('expected_response_url')
            if expected and wait_for_content:
                try:
                    async with page.expect_response(expected, timeout=timeout) as awaited:
                        response = await page.goto(payload['url'], wait_until='domcontentloaded', timeout=timeout)
                    await awaited.value
                except Exception as exc:
                    if 'timeout' not in type(exc).__name__.lower():
                        raise
                    response = final_document[0]
            else:
                response = await page.goto(payload['url'], wait_until='domcontentloaded', timeout=timeout)
            if wait_for_content:
                # Reuse the business-content validator while scripts settle. A challenge
                # often already has plenty of text, so a body-length wait is insufficient.
                from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
                request = FetchRequest.from_dict(payload)
                while time.monotonic() < start + timeout / 1000:
                    try:
                        document = final_document[0] or response
                        raw = FetchResult(final_url=page.url, html=await page.content(),
                            status=document.status if document else 200, transport='browser',
                            headers=await document.all_headers() if document else {})
                        verdict = classify_result(request, raw)
                        if verdict.ok or verdict.outcome in ('denied', 'network_error') or 'manual_challenge' in verdict.evidence:
                            break
                    except Exception as exc:
                        # Main-frame reloads invalidate a read of its old JS context.
                        if 'Execution context was destroyed' not in str(exc) and 'navigating' not in str(exc):
                            raise
                    await asyncio.sleep(min(.25, max(0, start + timeout / 1000 - time.monotonic())))
            if id(session.context) in self.admission_errors:
                busy = self.admission_errors[id(session.context)]
                return {'final_url': page.url, 'transport': 'browser', 'outcome': 'busy',
                        'error_code': 'origin_busy', 'retry_after': busy.retry_after, 'message': '官网访问正在排队'}
            await self.validator(page.url)
            html = await page.content()
            if len(html.encode('utf-8')) > 8 * 1024 * 1024:
                raise RuntimeFault('response_too_large', '页面超过抓取大小限制')
            response = final_document[0] or response
            return {'url': payload['url'], 'final_url': page.url,
                'status_code': response.status if response else 200,
                'headers': await response.all_headers() if response else {}, 'html': html,
                'transport': 'browser', 'timings': {'total_ms': round((time.monotonic() - start) * 1000)},
                'evidence': []}
        except Exception:
            if id(session.context) in self.admission_errors:
                busy = self.admission_errors[id(session.context)]
                return {'final_url': payload['url'], 'transport': 'browser', 'outcome': 'busy',
                        'error_code': 'origin_busy', 'retry_after': busy.retry_after, 'message': '官网访问正在排队'}
            raise
        finally:
            self.operation_deadlines.pop(operation_id, None)
            page.remove_listener('response', observe_document)
            session.touched = time.monotonic()
            if not keep_page:
                await page.close()
                session.page = None
                await admission.close()
                self.admissions.pop(id(session.context), None)

    async def open_manual(self, payload):
        ident, generation = payload.get('id'), payload.get('generation')
        if not ident or not generation:
            raise RuntimeFault('invalid_request', '验证标识无效')
        await self.validator(payload.get('url', ''))
        async with self.manual_guard:
            existing = self.verifications.get(ident)
            if existing:
                if existing['generation'] == generation:
                    return self.manual_status(ident, generation)
                if existing['state'] == 'active':
                    raise RuntimeFault('stale_session', '验证会话仍在使用，请先结束', 409)
                self.verifications.pop(ident, None)
            if any(v['state'] == 'active' for v in self.verifications.values()):
                raise RuntimeFault('verification_busy', '已有一个来源正在进行验证，请完成后重试', 409)
            session = await self._session(payload, headed=True)
            verification = {'id': ident, 'generation': generation, 'payload': payload,
                'session': session, 'state': 'active', 'expires': time.monotonic() + self.verification_ttl,
                'sockets': set(), 'error': None}
            self.verifications[ident] = verification
            try:
                async with session.lock:
                    await self._navigate(session, payload, keep_page=True, wait_for_content=False)
                    await session.display.expose()
            except Exception:
                await self.end_manual(ident, generation, 'failed')
                raise
            return self.manual_status(ident, generation)

    @staticmethod
    def _check_generation(verification, generation):
        if not secrets.compare_digest(verification['generation'], str(generation)):
            raise RuntimeFault('stale_session', '验证会话已更新，请刷新页面', 409)

    def manual_status(self, ident, generation):
        verification = self.verifications.get(ident)
        if verification is None:
            raise RuntimeFault('session_missing', '验证窗口已关闭，请重新打开', 404)
        self._check_generation(verification, generation)
        return {'id': ident, 'state': verification['state'], 'runtime_id': self.boot_id,
            'remote': os.name != 'nt', 'remaining_seconds': max(0, int(verification['expires'] - time.monotonic()))}

    async def verify_manual(self, ident, generation):
        state = self.manual_status(ident, generation)
        if state['state'] != 'active' or state['remaining_seconds'] <= 0:
            raise RuntimeFault('session_closed', '验证窗口已结束，请重新打开', 409)
        verification = self.verifications[ident]
        session = verification['session']
        async with session.lock:
            raw = await self._navigate(session, verification['payload'], keep_page=True)
            # Verification uses the same acquisition classifier, not an administrator assertion.
            from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
            verdict = classify_result(FetchRequest.from_dict(verification['payload']), FetchResult.from_dict(raw))
            if not verdict.ok:
                raise RuntimeFault('verification_incomplete', '尚未检测到有效通知内容，请完成官网验证后重试', 409)
        await self.end_manual(ident, generation, 'verified', retain=True)
        return self.manual_status(ident, generation)

    async def end_manual(self, ident, generation, state='cancelled', retain=False):
        self.manual_status(ident, generation)
        verification = self.verifications[ident]
        if verification['state'] != 'active':
            return self.manual_status(ident, generation)
        verification['state'] = state
        for key in [key for key, value in self.tickets.items() if value['id'] == ident]:
            del self.tickets[key]
        for socket in list(verification['sockets']):
            await socket.close(code=1000, message=b'verification closed')
        session = verification['session']
        await session.display.hide()
        admission = self.admissions.pop(id(session.context), None)
        if admission:
            await admission.close()
        if retain:
            if session.page:
                await session.page.close()
                session.page = None
            session.touched = time.monotonic()
        else:
            async with session.lock:
                await self._close_session(session)
            self.sessions.pop(session.key, None)
        return self.manual_status(ident, generation)

    def issue_ticket(self, ident, generation):
        state = self.manual_status(ident, generation)
        if state['state'] != 'active' or state['remaining_seconds'] <= 0:
            raise RuntimeFault('session_closed', '验证窗口已结束', 409)
        for key in [key for key, value in self.tickets.items() if value['id'] == ident]:
            del self.tickets[key]
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = {'id': ident, 'generation': generation,
            'expires': time.monotonic() + min(60, state['remaining_seconds'])}
        return {'ticket': ticket, 'expires_in': 60}

    def consume_ticket(self, ident, ticket):
        value = self.tickets.pop(ticket, None)
        if value is None or value['id'] != ident or value['expires'] <= time.monotonic():
            raise RuntimeFault('invalid_ticket', '验证链接已过期，请刷新页面', 403)
        state = self.manual_status(ident, value['generation'])
        if state['state'] != 'active' or state['remaining_seconds'] <= 0:
            raise RuntimeFault('session_closed', '验证窗口已结束', 403)
        return self.verifications[ident]

    async def sweep(self):
        now = time.monotonic()
        for ident, value in list(self.verifications.items()):
            if value['state'] == 'active' and value['expires'] <= now:
                await self.end_manual(ident, value['generation'], 'expired')
            if value['state'] != 'active' and value['expires'] + 600 < now:
                self.verifications.pop(ident, None)
        for ident, execution in list(self.executions.items()):
            if execution.state in ('done', 'failed') and execution.touched + 300 < now:
                del self.executions[ident]
        for ticket, value in list(self.tickets.items()):
            if value['expires'] < now:
                del self.tickets[ticket]
        for key, session in list(self.sessions.items()):
            if not session.lock.locked() and not self._active_session(session) and session.touched + self.session_ttl < now:
                await self._close_session(session)
                self.sessions.pop(key, None)

    async def _close_session(self, session):
        admission = self.admissions.pop(id(session.context), None)
        if admission:
            await admission.close()
        self.admission_errors.pop(id(session.context), None)
        try:
            await session.context.close()
        finally:
            try:
                if session.browser:
                    await session.browser.close()
            finally:
                if session.display:
                    await session.display.close()

    async def close(self):
        self.closed = True
        # Native Playwright timeouts bound operations; do not cancel an in-flight API call.
        pending = [e.task for e in self.executions.values() if e.task and not e.task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for value in list(self.verifications.values()):
            if value['state'] == 'active':
                await self.end_manual(value['id'], value['generation'], 'expired')
        for session in list(self.sessions.values()):
            await self._close_session(session)
        self.sessions.clear()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
