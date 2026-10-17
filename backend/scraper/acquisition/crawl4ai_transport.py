"""Optional Crawl4AI experiment; never a default or unguarded public transport.

Production acquisition still belongs to BrowserRuntime and its origin broker.
This module normalizes completed observations and runs explicitly isolated
diagnostics. Public diagnostics require the existing authenticated origin
broker and a separate sandboxed system browser. Import starts neither engine.
"""
import asyncio
from dataclasses import replace
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from backend.scraper.http_client import validate_public_url
from backend.services.discovery_policy import get_policy
from .classifier import classify_result
from .contracts import FetchResult


DIAGNOSTIC_ONLY = 'crawl4ai_diagnostic_only'


def readiness_wait(selector):
    """Match the shared DOM-presence contract, including hidden menu entries.

    Upstream CSS waiting requires its first match to become visible; one hidden
    desktop/mobile menu copy can block every later valid match indefinitely.
    The selector is JSON string data, never executable page/model instructions.
    """
    return 'js:() => document.querySelector(' + json.dumps(selector) + ') !== null' if selector else None


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def normalize_observation(request, observed, *, elapsed_seconds=0, validator=None):
    """Apply the existing verdict to complete HTML; engine success is no proof.

    The default validator performs syntax/address checks without DNS or I/O.
    Only the fixture runner supplies its stricter, single-loopback-origin check.
    No markdown replaces the original DOM or its CSS locators.
    """
    if request.purpose == 'article':
        return FetchResult(request.url, transport='crawl4ai', outcome='denied',
                           error_code='crawl4ai_diagnostic_scope',
                           message='本轮评估仅包含官网结构与栏目入口', evidence=(DIAGNOSTIC_ONLY,))
    validate = validator or (lambda url: validate_public_url(url, resolve=False))
    final_url = _field(observed, 'redirected_url') or _field(observed, 'url') or request.url
    try:
        validate(request.url)
        validate(final_url)
    except (ValueError, OSError):
        return FetchResult(request.url, transport='crawl4ai', outcome='denied',
                           error_code='blocked_destination', message='读取地址不符合允许范围',
                           evidence=(DIAGNOSTIC_ONLY,))
    html = _field(observed, 'html') or ''
    if not isinstance(html, str):
        return FetchResult(final_url, transport='crawl4ai', outcome='needs_adapter',
                           error_code='invalid_engine_material', message='爬取引擎未提供完整 HTML')
    if len(html.encode('utf-8')) > get_policy()['html_max_bytes']:
        return FetchResult(final_url, transport='crawl4ai', outcome='denied',
                           error_code='response_too_large', message='官网页面超过 HTML 读取上限，已停止读取',
                           evidence=(DIAGNOSTIC_ONLY, 'complete_response_rejected'))
    try:
        status = int(_field(observed, 'status_code') or 0)
    except (ValueError, TypeError):
        status = 0
    headers = _field(observed, 'response_headers') or {}
    raw = FetchResult(final_url, status=status, html=html, transport='crawl4ai',
                      headers=dict(headers), evidence=(DIAGNOSTIC_ONLY,),
                      timings={'total_ms': round(elapsed_seconds * 1000)})
    # Crawl4AI may report success=True for an HTTP 404, and success=False while
    # retaining an actual HTTP denial. Preserve these real observations first.
    if status:
        verdict = replace(classify_result(request, replace(raw, transport='browser')),
                          transport='crawl4ai')
        if not _field(observed, 'success', False) and verdict.ok:
            # 0.9.4 mislabels short, fully populated Chinese navigation as an
            # anti-bot block. Only this verified engine heuristic can defer to
            # our DOM/readiness/content validation. All real access, partial
            # material, timeout and other engine failures remain failures.
            minimal_text_heuristic = re.fullmatch(
                r'Blocked by anti-bot protection: Structural: minimal_text on small page '
                r'\(\d+ bytes, \d+ chars visible\)', str(_field(observed, 'error_message') or ''))
            if minimal_text_heuristic and 200 <= status < 300:
                return replace(verdict, evidence=(*verdict.evidence,
                    'engine_minimal_text_heuristic', 'shared_material_validation_passed'))
            return replace(raw, outcome='network_error', error_code='crawl4ai_read_incomplete',
                           message='爬取引擎未完成本次读取')
        return verdict
    return replace(raw, outcome='network_error', error_code='crawl4ai_no_http_observation',
                   message='爬取引擎未取得可验证的 HTTP 响应')


def fixture_validator(origin):
    """Explicit diagnostics can access exactly one numeric loopback origin."""
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
            not parsed.port or parsed.username or parsed.password or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('A dedicated http://127.0.0.1:port fixture origin is required')
    authority = (parsed.scheme, parsed.hostname, parsed.port)

    def validate(url):
        target = urlsplit(url)
        if (target.scheme, target.hostname, target.port) != authority or target.username or target.password:
            raise ValueError('Only the explicit fixture origin is allowed')

    return validate


class Crawl4AIFixtureTransport:
    """One bounded, serial local fixture read, using an existing Edge install.

    This deliberately cannot fetch public sites. A future production adapter
    must use BrowserRuntime's complete admission/egress lifecycle, not this
    fixture exception. No retries, deep crawling, previews, extraction model,
    embedding, browser download, user session, or persistent task queue exists.
    """
    def __init__(self, *, fixture_origin, base_directory, browser_channel='msedge'):
        self.validate = fixture_validator(fixture_origin)
        if browser_channel not in ('msedge', 'chrome'):
            raise ValueError('Use an already installed system browser channel')
        self.base_directory = Path(base_directory).resolve()
        self.channel = browser_channel
        self.lock = asyncio.Lock()
        self.last_diagnostics = {}

    async def fetch(self, request):
        self.validate(request.url)
        if request.purpose == 'article':
            return normalize_observation(request, {}, validator=self.validate)
        if not request.browser_allowed:
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                               error_code='browser_not_allowed', message='本次读取未启用浏览器')
        async with self.lock:
            return await self._read(request)

    async def _read(self, request):
        started = time.monotonic()
        # 0.9.4's global database ignores AsyncWebCrawler(base_directory=...).
        # Set the actual upstream variable before import; an already imported
        # engine bound to a different database must not reuse user-home state.
        if 'crawl4ai' in sys.modules:
            database = sys.modules.get('crawl4ai.async_database')
            db_path = getattr(database, 'DB_PATH', '')
            if not db_path or not Path(db_path).resolve().is_relative_to(self.base_directory):
                return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                    error_code='crawl4ai_storage_not_isolated',
                    message='可选引擎已绑定其他存储位置，请在独立评估进程运行',
                    evidence=(DIAGNOSTIC_ONLY,))
        else:
            os.environ['CRAWL4_AI_BASE_DIRECTORY'] = str(self.base_directory)
        try:
            engine = importlib.import_module('crawl4ai')
        except ImportError:
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                               error_code='crawl4ai_not_installed',
                               message='隔离评估环境未安装可选爬取引擎', evidence=(DIAGNOSTIC_ONLY,))
        crawler = None
        blocked = []
        try:
            browser_config = engine.BrowserConfig(
                channel=self.channel, chrome_channel=self.channel, headless=True,
                ignore_https_errors=False, accept_downloads=False, verbose=False)
            crawler = engine.AsyncWebCrawler(config=browser_config,
                base_directory=str(self.base_directory), verbose=False)

            async def guard_context(page, context, **kwargs):
                async def guard(route):
                    try:
                        self.validate(route.request.url)
                    except (ValueError, OSError):
                        blocked.append('out_of_fixture_request')
                        await route.abort('blockedbyclient')
                        return
                    await route.continue_()
                await context.route('**/*', guard)
                if hasattr(context, 'route_web_socket'):
                    async def deny_socket(route):
                        blocked.append('fixture_websocket_blocked')
                        await route.close()
                    await context.route_web_socket('**/*', deny_socket)
                return page

            crawler.crawler_strategy.set_hook('on_page_context_created', guard_context)
            config = engine.CrawlerRunConfig(
                cache_mode=engine.CacheMode.BYPASS, wait_until='domcontentloaded',
                page_timeout=math.ceil(request.timeout_seconds * 1000),
                wait_for=readiness_wait(request.readiness_selector),
                delay_before_return_html=0, check_robots_txt=False,
                process_iframes=False, extraction_strategy=None, max_retries=0,
                verbose=False)
            async with asyncio.timeout(request.timeout_seconds):
                await crawler.start()
                observed = await crawler.arun(url=request.url, config=config)
            # Only this explicit loopback diagnostic exposes the engine's
            # message for root-cause analysis. It is not sent to product UI.
            self.last_diagnostics = {'engine_success': bool(observed.success),
                'engine_error': observed.error_message or '',
                'engine_crawl_stats': getattr(observed, 'crawl_stats', None)}
            result = normalize_observation(request, observed,
                elapsed_seconds=time.monotonic() - started, validator=self.validate)
            return replace(result, evidence=(*result.evidence, *sorted(set(blocked))))
        except TimeoutError:
            return FetchResult(request.url, transport='crawl4ai', outcome='network_error',
                               error_code='crawl4ai_timeout', message='隔离读取超过本次时限',
                               evidence=(DIAGNOSTIC_ONLY,),
                               timings={'total_ms': round((time.monotonic() - started) * 1000)})
        except Exception as exc:
            # Engine exceptions can contain full headers or request data. Keep
            # the type for diagnosis, never reflect their raw text into the UI.
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                               error_code='crawl4ai_fixture_failed', message='隔离引擎评估未完成',
                               evidence=(DIAGNOSTIC_ONLY, type(exc).__name__))
        finally:
            if crawler is not None:
                try:
                    await asyncio.wait_for(crawler.close(), timeout=5)
                except Exception:
                    pass


class Crawl4AIPublicDiagnosticTransport:
    """Opt-in public comparison using the existing origin and egress guards.

    Crawl4AI attaches over loopback CDP to a browser launched here with its
    sandbox enabled. It never launches its default no-sandbox browser. No AI,
    extraction model, automatic retry, user profile or production engine switch
    is involved. The comparison runner serializes browser use across engines.
    """
    def __init__(self, *, base_directory, allow_public_diagnostics=False, browser_channel='msedge'):
        if allow_public_diagnostics is not True:
            raise ValueError('Public engine diagnostics require explicit opt-in')
        if browser_channel not in ('msedge', 'chrome'):
            raise ValueError('Use an already installed system browser channel')
        self.base_directory = Path(base_directory).resolve()
        self.channel = browser_channel
        self.lock = asyncio.Lock()
        self.last_diagnostics = {}

    async def fetch(self, request):
        from backend.browser_service.admission import OriginAdmission, OriginBusy
        from backend.browser_service.runtime import BrowserRuntime
        if request.purpose not in ('directory', 'list'):
            return FetchResult(request.url, transport='crawl4ai', outcome='denied',
                error_code='crawl4ai_diagnostic_scope', message='本轮评估仅包含官网结构与栏目入口')
        if not request.browser_allowed:
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                error_code='browser_not_allowed', message='本次读取未启用浏览器')
        broker = urlsplit(os.environ.get('WATCHER_ORIGIN_BROKER_URL', ''))
        if (broker.scheme != 'http' or broker.hostname != '127.0.0.1' or not broker.port
                or not os.environ.get('WATCHER_BROWSER_TOKEN')):
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                error_code='crawl4ai_broker_required', message='真实官网对照需要现有共享限速服务')
        try:
            validate_public_url(request.url, resolve=False)
        except (ValueError, OSError):
            return FetchResult(request.url, transport='crawl4ai', outcome='denied',
                error_code='blocked_destination', message='读取地址不符合允许范围')
        if not 0 < request.timeout_seconds <= get_policy()['browser_total_seconds']:
            return FetchResult(request.url, transport='crawl4ai', outcome='denied',
                error_code='crawl4ai_timeout_policy', message='读取时限超过当前配置')
        existing = os.environ.get('CRAWL4_AI_BASE_DIRECTORY')
        if existing and Path(existing).resolve() != self.base_directory:
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                error_code='crawl4ai_storage_not_isolated', message='请在独立评估进程运行')
        if 'crawl4ai' in sys.modules:
            database = sys.modules.get('crawl4ai.async_database')
            db_path = getattr(database, 'DB_PATH', '')
            if not db_path or not Path(db_path).resolve().is_relative_to(self.base_directory):
                return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                    error_code='crawl4ai_storage_not_isolated', message='请在独立评估进程运行')
        os.environ['CRAWL4_AI_BASE_DIRECTORY'] = str(self.base_directory)
        try:
            engine = importlib.import_module('crawl4ai')
            from playwright.async_api import async_playwright
        except ImportError:
            return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                error_code='crawl4ai_not_installed', message='隔离评估环境未安装可选爬取引擎')
        async with self.lock:
            started = time.monotonic()
            admission = OriginAdmission(request.to_dict(), ttl=math.ceil(request.timeout_seconds) + 15)
            runtime = BrowserRuntime()
            crawler = owned_context = driver = None
            documents, resource_requests, stages = [], [], []
            self.last_diagnostics = {}
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    await admission.admit(request.url)
                    driver = await async_playwright().start()
                    import hashlib
                    identity = hashlib.sha256(str(request.request_id or request.url).encode()).hexdigest()[:24]
                    profile = self.base_directory / ('browser-' + identity)
                    owned_context = await driver.chromium.launch_persistent_context(str(profile),
                        channel=self.channel, headless=True, chromium_sandbox=True,
                        accept_downloads=False, ignore_https_errors=False, service_workers='block',
                        viewport={'width': 1280, 'height': 900},
                        args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1'])
                    port_file = profile / 'DevToolsActivePort'
                    while not port_file.exists():
                        await asyncio.sleep(.05)
                    port = int(port_file.read_text('utf-8').splitlines()[0])
                    agent = await owned_context.pages[0].evaluate('navigator.userAgent')
                    browser_config = engine.BrowserConfig(cdp_url=f'http://127.0.0.1:{port}',
                        use_managed_browser=True, cdp_cleanup_on_close=True,
                        user_agent=agent, viewport_width=1280, viewport_height=900,
                        headless=True, ignore_https_errors=False, accept_downloads=False, verbose=False)
                    crawler = engine.AsyncWebCrawler(config=browser_config,
                        base_directory=str(self.base_directory), verbose=False)

                    async def guard_context(page, context, **kwargs):
                        runtime.admissions[id(context)] = admission
                        await runtime._guard_context(context)
                        context.on('request', lambda req: resource_requests.append(req.resource_type))
                        page.on('response', lambda response: documents.append(response.status)
                            if response.request.resource_type == 'document' else None)
                        return page

                    async def stage_after_goto(page, **kwargs):
                        measured = await page.evaluate('''selector => {
                            const nodes = selector ? [...document.querySelectorAll(selector)] : [];
                            const first = nodes[0];
                            return {ready_state: document.readyState, matched_nodes: nodes.length,
                                first_visible: first ? !!(first.getClientRects().length) : null,
                                visible_matches: nodes.filter(node => node.getClientRects().length).length};
                        }''', request.readiness_selector)
                        stages.append({'stage': 'after_goto', 'seconds': round(time.monotonic() - started, 3),
                                       **measured})
                        return page

                    async def stage_before_html(page, **kwargs):
                        stages.append({'stage': 'before_retrieve_html',
                                       'seconds': round(time.monotonic() - started, 3)})
                        return page

                    crawler.crawler_strategy.set_hook('on_page_context_created', guard_context)
                    crawler.crawler_strategy.set_hook('after_goto', stage_after_goto)
                    crawler.crawler_strategy.set_hook('before_retrieve_html', stage_before_html)
                    config = engine.CrawlerRunConfig(cache_mode=engine.CacheMode.BYPASS,
                        wait_until='domcontentloaded', page_timeout=math.ceil(request.timeout_seconds * 1000),
                        wait_for=readiness_wait(request.readiness_selector),
                        delay_before_return_html=0, check_robots_txt=False,
                        process_iframes=False, extraction_strategy=None, max_retries=0, verbose=False)
                    await crawler.start()
                    observed = await crawler.arun(url=request.url, config=config)
                    if runtime.admission_errors:
                        raise next(iter(runtime.admission_errors.values()))
                    result = normalize_observation(request, observed,
                        elapsed_seconds=time.monotonic() - started)
                    await admission.cooldown(result.final_url, result.headers, result.status)
                    self.last_diagnostics = {'engine_success': bool(observed.success),
                        'engine_error': observed.error_message or '', 'sandbox_enabled': True,
                        'shared_origin_broker': True, 'resource_requests': len(resource_requests),
                        'document_response_statuses': documents, 'actual_model_requests': 0}
                    self.last_diagnostics['stages'] = stages
                    return result
            except OriginBusy as exc:
                return FetchResult(request.url, transport='crawl4ai', outcome='busy',
                    error_code='origin_busy', retry_after=exc.retry_after, message='官网访问正在排队')
            except TimeoutError:
                return FetchResult(request.url, transport='crawl4ai', outcome='network_error',
                    error_code='crawl4ai_timeout', message='隔离读取超过本次时限',
                    timings={'total_ms': round((time.monotonic() - started) * 1000)})
            except Exception as exc:
                return FetchResult(request.url, transport='crawl4ai', outcome='unavailable',
                    error_code='crawl4ai_diagnostic_failed', message='隔离引擎评估未完成',
                    evidence=(DIAGNOSTIC_ONLY, type(exc).__name__))
            finally:
                self.last_diagnostics.setdefault('resource_requests', len(resource_requests))
                self.last_diagnostics.setdefault('document_response_statuses', documents)
                self.last_diagnostics.setdefault('stages', stages)
                for resource, method in ((crawler, 'close'), (owned_context, 'close'), (driver, 'stop')):
                    if resource is not None:
                        try:
                            await asyncio.wait_for(getattr(resource, method)(), timeout=5)
                        except Exception:
                            pass
                runtime.admissions.clear()
                await admission.close()
