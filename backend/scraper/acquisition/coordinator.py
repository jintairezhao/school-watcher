"""One HTTP/browser policy for directory, publication list and article fetching."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from bs4 import BeautifulSoup, UnicodeDammit

from backend.scraper.fetch_errors import SourceAccessError, describe_fetch_error
from backend.scraper.http_client import requests, validate_public_url
from .contracts import FetchRequest, FetchResult
from .classifier import classify_result
from .profiles import api_request, configured_request, map_api_response

MAX_PAGE_BYTES = 3 * 1024 * 1024
_browser_dispatch = ContextVar('acquisition_browser_dispatch', default=None)
_cache_lookup = ContextVar('acquisition_cache_lookup', default=None)
_cache_store = ContextVar('acquisition_cache_store', default=None)
_before_fetch = ContextVar('acquisition_before_fetch', default=None)


class FetchFailure(SourceAccessError):
    def __init__(self, result):
        self.result = result
        self.outcome = result.outcome
        self.error_code = result.error_code
        self.retryable = result.outcome in ('network_error', 'unavailable')
        super().__init__(result.message or result.error_code or result.outcome)


class FetchDeferred(FetchFailure):
    """Useful to standalone clients; the worker supplies its own control flow hook."""
    def __init__(self, request, result):
        self.request = request
        super().__init__(result)


@contextmanager
def execution_context(*, browser_dispatch=None, cache_lookup=None, cache_store=None, before_fetch=None):
    tokens = [(variable, variable.set(value)) for variable, value in
              ((_browser_dispatch, browser_dispatch), (_cache_lookup, cache_lookup),
               (_cache_store, cache_store), (_before_fetch, before_fetch))]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _assert_execution():
    callback = _before_fetch.get()
    if callback is not None:
        callback()


def _final_url(requested, returned):
    # HTTP responses omit the fragment because it was never sent over the wire.
    original, final = urlsplit(requested), urlsplit(returned or requested)
    if original.fragment and not final.fragment and original[:4] == final[:4]:
        final = final._replace(fragment=original.fragment)
    return urlunsplit(final)


def http_fetch(request):
    start = time.monotonic()
    response = None
    try:
        response = requests.get(request.url, timeout=(min(6, request.timeout_seconds), min(16, request.timeout_seconds)),
                stream=True, headers={'User-Agent': 'SchoolWatcher/3.0 (public university notifications)',
                'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'})
        headers = dict(response.headers)
        status = response.status_code
        final = _final_url(request.url, response.url)
        mime = headers.get('Content-Type', '').lower()
        if mime and not any(kind in mime for kind in ('text/', 'application/xhtml', 'application/json', '+json')):
            return FetchResult(final, status=status, headers=headers, error_code='non_html_resource',
                               message='该官网地址返回了非网页资源，需要核对来源地址')
        payload = bytearray()
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload) > MAX_PAGE_BYTES:
                return FetchResult(final, status=status, headers=headers, error_code='page_size_requires_review',
                                   message='官网页面超出读取大小限制，需要核对来源')
            if time.monotonic() - start > request.timeout_seconds:
                return FetchResult(final, status=status, outcome='network_error', error_code='http_budget_exhausted',
                                   message='读取官网超时，请稍后重试')
        html = UnicodeDammit(bytes(payload), is_html=True).unicode_markup or ''
        data = None
        if 'json' in mime or request.policy.get('api'):
            try:
                data = json.loads(html)
            except ValueError:
                pass
        evidence = tuple(getattr(response, '_watcher_transport', []) or [])
        dns = getattr(response, '_watcher_dns', None)
        if dns:
            evidence += ('public_dns_ipv4:' + str(dns.get('public_ip', '')),)
        retry_after = headers.get('Retry-After', '')
        return FetchResult(final, status=status, html=html, json_data=data, headers=headers,
                           evidence=evidence, timings={'http_ms': round((time.monotonic() - start) * 1000)},
                           retry_after=float(retry_after) if str(retry_after).isdigit() else None)
    except Exception as exc:
        from backend.scraper.auth_routes import LOGIN_REQUIRED_MESSAGE
        from backend.scraper.fetch_errors import SourceLoginRequired
        if isinstance(exc, SourceLoginRequired):
            # The site sent us to its identity provider and the hand-off did not
            # complete. This is the site's access rule, not a program fault, and
            # must not be retried as if the network were merely unwell.
            return FetchResult(request.url, outcome='denied', error_code='source_login_required',
                               message=LOGIN_REQUIRED_MESSAGE,
                               evidence=('sign_in_hop:' + str(exc),),
                               timings={'http_ms': round((time.monotonic() - start) * 1000)})
        error = describe_fetch_error(exc)
        return FetchResult(request.url, outcome='network_error' if error.retryable else 'denied',
                           error_code='http_' + type(exc).__name__.lower(), message=str(error),
                           timings={'http_ms': round((time.monotonic() - start) * 1000)})
    finally:
        if response is not None:
            response.close()


def _document_redirect(raw):
    """Only actual meta refresh or a script-only redirect document is followed."""
    if '$_ts' in raw.html[:50000] or any(str(k).lower() == 'cf-mitigated' for k in raw.headers):
        return None
    soup = BeautifulSoup(raw.html, 'lxml')
    refresh = soup.find('meta', attrs={'http-equiv': re.compile('^refresh$', re.I)})
    if refresh:
        match = re.search(r'url\s*=\s*[\"\']?([^\"\']+)', refresh.get('content', ''), re.I)
        if match:
            return urljoin(raw.final_url, match.group(1).strip())
    for node in soup.select('script, style, noscript'):
        node.decompose()
    if len(soup.get_text(' ', strip=True)) > 30 or soup.select_one('a[href], article'):
        return None
    stripped = re.sub(r'<!--\[if\s[^\]]*\]>.*?<!\[endif\]-->', '', raw.html, flags=re.I | re.S)
    match = re.search(r'(?:window\.)?location(?:\.href)?\s*=\s*[\"\']([^\"\']+)[\"\']', stripped, re.I)
    return urljoin(raw.final_url, match.group(1)) if match else None


def fetch(request: FetchRequest, *, http_transport=None, browser_transport=None) -> FetchResult:
    _assert_execution()
    validate_public_url(request.url, resolve=False)
    try:
        request = configured_request(request)
    except ValueError as exc:
        return FetchResult(request.url, error_code='invalid_source_profile', message=str(exc))
    lookup = _cache_lookup.get()
    if lookup is not None:
        cached = lookup(request)
        if cached is not None:
            return FetchResult.from_dict(cached) if isinstance(cached, dict) else cached
    result = _fetch(request, http_transport=http_transport, browser_transport=browser_transport)
    store = _cache_store.get()
    if result.ok and store is not None:
        _assert_execution()
        store(request, result)
    return result


def _fetch(request: FetchRequest, *, http_transport=None, browser_transport=None) -> FetchResult:
    started = time.monotonic()
    request = replace(request, request_id=request.request_id or uuid4().hex)
    http = http_transport or http_fetch
    try:
        api = api_request(request)
    except ValueError as exc:
        return FetchResult(request.url, error_code='invalid_api_profile', message=str(exc))
    if api:
        validate_public_url(api.url, resolve=False)
        _assert_execution()
        return map_api_response(request, http(api))
    _assert_execution()
    raw = http(request)
    if isinstance(raw, dict):
        raw = FetchResult.from_dict(raw)
    visited = {request.url}
    while request.policy.get('follow_document_redirects', True) and len(visited) < 4:
        if raw.status >= 400 or raw.error_code:
            break
        target = _document_redirect(raw)
        if not target or target in visited:
            break
        validate_public_url(target, resolve=False)
        visited.add(target)
        remaining = request.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return replace(raw, outcome='network_error', error_code='http_budget_exhausted', message='读取官网超时')
        _assert_execution()
        raw = http(replace(request, url=target, timeout_seconds=remaining))
    effective = replace(request, url=raw.final_url or request.url)
    result = classify_result(effective, raw)
    if result.outcome != 'requires_render':
        return result
    if not request.browser_allowed:
        return result
    render_request = replace(effective, timeout_seconds=min(90, max(request.timeout_seconds,
                                90 if 'automatic_challenge' in result.evidence else request.timeout_seconds)))
    dispatch = _browser_dispatch.get()
    _assert_execution()
    if dispatch is not None:
        rendered = dispatch(render_request)
    else:
        if browser_transport is None:
            from .browser_client import BrowserClient
            browser_transport = BrowserClient().fetch
        rendered = browser_transport(render_request)
    if isinstance(rendered, dict):
        rendered = FetchResult.from_dict(rendered)
    if not rendered.final_url:
        rendered = replace(rendered, final_url=effective.url)
    if rendered.outcome in ('busy', 'unavailable', 'network_error', 'needs_manual') and rendered.error_code:
        return rendered
    return classify_result(render_request, replace(rendered, transport='browser'))


def fetch_or_raise(request, **kwargs):
    result = fetch(request, **kwargs)
    if not result.ok:
        raise FetchFailure(result)
    return result
