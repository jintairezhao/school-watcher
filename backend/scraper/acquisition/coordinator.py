"""One HTTP/browser policy for directory, publication list and article fetching."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from bs4 import BeautifulSoup

from backend.scraper.fetch_errors import SourceAccessError, describe_fetch_error
from backend.scraper.http_client import validate_public_url
from backend.scraper.auth_routes import is_sign_in_route
from .contracts import FetchRequest, FetchResult
from .classifier import classify_result
from .profiles import api_request, configured_request, map_api_response

_browser_dispatch = ContextVar('acquisition_browser_dispatch', default=None)
_cache_lookup = ContextVar('acquisition_cache_lookup', default=None)
_cache_store = ContextVar('acquisition_cache_store', default=None)
_before_fetch = ContextVar('acquisition_before_fetch', default=None)
_SOURCE_NEGOTIATION_STATUSES = frozenset((406, 415, 421, 422))


def _source_transport_verdict(request, observed, result):
    """A source GET refusal can describe the HTTP route, not a missing source.

    One ordinary browser observation distinguishes that case. Authentication,
    explicit restrictions, size failures, API profiles and ordinary article reads keep
    their existing handling; browser responses never trigger another probe.
    """
    source_trial = False
    if request.purpose == 'article' and request.policy.get('exploration') is True:
        from backend.services.tasks import current_execution
        source_trial = (current_execution() or {}).get('kind') in ('onboard', 'source_review')
    source_read = request.purpose in ('directory', 'list') or source_trial
    if (not source_read or not request.browser_allowed
            or observed.transport != 'http' or is_sign_in_route(observed.final_url)):
        return result
    # Some configured proxies close pinned-IP HTTP requests while their browser
    # route can read the original public host. Waiting behind every unvisited
    # unit before trying that route strands a readable university roster.
    # This is a transport observation, not evidence of JavaScript or bad rules.
    # Keep DNS/connect/idle/total timeouts and explicit HTTP refusals unchanged.
    if (observed.status == 0 and observed.outcome == 'network_error'
            and observed.error_code == 'http_serverdisconnectederror'):
        return replace(result, outcome='requires_render', error_code='source_http_transport_probe',
            message='普通读取连接断开，正在尝试浏览器读取这个官网入口',
            evidence=tuple(result.evidence) + ('http_source_transport_probe:' + observed.error_code,))
    if (observed.status in (502, 503, 504) and not observed.retry_after
            and 'proxy_route_retry_available' in observed.evidence
            and result.outcome == 'network_error'):
        content = classify_result(request, replace(observed, status=200, error_code='', outcome='needs_adapter'))
        if content.outcome not in ('denied', 'needs_manual'):
            return replace(result, outcome='requires_render', error_code='source_http_transport_probe',
                message='普通读取出现代理网关错误，正在尝试浏览器读取这个官网入口',
                evidence=tuple(result.evidence) + (f'http_source_transport_probe:proxy_gateway_{observed.status}',))
    if (observed.status not in _SOURCE_NEGOTIATION_STATUSES
            or result.error_code != f'http_{observed.status}'
            or observed.error_code not in ('', f'http_{observed.status}')):
        return result
    content = classify_result(request, replace(observed, status=200, error_code='', outcome='needs_adapter'))
    if content.outcome in ('denied', 'needs_manual'):
        return result
    return replace(result, outcome='requires_render', error_code='source_http_transport_probe',
        message='普通读取返回异常，正在尝试浏览器读取这个官网入口',
        evidence=tuple(result.evidence) + (f'http_source_transport_probe:{observed.status}',))


class FetchFailure(SourceAccessError):
    def __init__(self, result):
        self.result = result
        self.outcome = result.outcome
        self.error_code = result.error_code
        # A still-loading public document can recover on another bounded read.
        # Keep its material verdict; it is neither a parser repair nor success.
        self.retryable = (result.outcome in ('network_error', 'unavailable') or
                          result.outcome in ('needs_adapter', 'requires_render') and
                          result.error_code in ('dynamic_material_pending', 'dynamic_material_incomplete'))
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
    from .http_transport import fetch_http
    return fetch_http(request)


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
    scripts = [node.get_text() for node in soup.select('script:not([src])')]
    for node in soup.select('script, style, noscript'):
        node.decompose()
    if len(soup.get_text(' ', strip=True)) > 30 or soup.select_one('a[href], article'):
        return None
    # A substring match cannot establish JavaScript control flow: a login
    # handler, comment or obsolete-browser branch may contain a location
    # assignment. Only recognize a complete literal assignment statement;
    # application shells otherwise proceed to ordinary browser rendering.
    for script in scripts:
        match = re.fullmatch(r'\s*(?:window\.)?(?:top\.)?location(?:\.href)?\s*=\s*'
                             r'([\"\'])([^\"\'\\\r\n]+)\1\s*;?\s*', script)
        if match:
            return urljoin(raw.final_url, match.group(2))
    return None


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
            observed = FetchResult.from_dict(cached) if isinstance(cached, dict) else cached
            # Replay the complete observation under the current program's
            # classification. A fixed CMS recognizer must not inherit an old
            # "unrecognized" verdict and pay to read or identify it again.
            # Explicit network/access failures retain their typed error codes.
            result = _source_transport_verdict(request, observed, classify_result(request, observed))
            if result.outcome != 'requires_render' or not request.browser_allowed:
                return result
            # The retained HTTP observation may have been called complete by an
            # older classifier. Reuse it and read the missing rendered material,
            # instead of either repeating HTTP or permanently replaying the shell.
            result = _render(request, replace(request, url=observed.final_url or request.url), result,
                             browser_transport=browser_transport)
        else:
            result = _fetch(request, http_transport=http_transport, browser_transport=browser_transport)
    else:
        result = _fetch(request, http_transport=http_transport, browser_transport=browser_transport)
    store = _cache_store.get()
    if result.replayable and store is not None:
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
    # After an actual failed HTTP observation, the second scheduled attempt can
    # test the configured browser route. This replaces that attempt's HTTP read;
    # it does not add an immediate retry or rewrite a working extraction rule.
    from backend.scraper.http_client import inventory_retry_count
    if request.browser_allowed and inventory_retry_count(request.url) == 1:
        from .browser_client import BrowserClient
        configured = browser_transport is not None or _browser_dispatch.get() is not None
        if not configured:
            client = BrowserClient()
            configured = bool(client.base_url and client.token)
            if configured:
                browser_transport = client.fetch
        if configured:
            return _render(request,request,FetchResult(request.url,outcome='requires_render',
                error_code='network_route_probe'),browser_transport=browser_transport)
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
    result = _source_transport_verdict(effective, raw, classify_result(effective, raw))
    return _render(request, effective, result, browser_transport=browser_transport)


def _render(request, effective, result, *, browser_transport=None):
    if result.outcome != 'requires_render':
        return result
    if not request.browser_allowed:
        return result
    from backend.services.discovery_policy import get_policy
    render_request = replace(effective, timeout_seconds=min(request.timeout_seconds, get_policy()['browser_total_seconds']))
    if result.error_code == 'dynamic_material_pending':
        # Once missing destinations were observed, a partially populated DOM
        # must not become "complete" merely because only one placeholder remains.
        render_request = replace(render_request, policy=dict(render_request.policy, dynamic_material_observed=True))
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
    if result.error_code == 'source_http_transport_probe':
        rendered = replace(rendered, evidence=tuple(dict.fromkeys((*result.evidence, *rendered.evidence))))
    if rendered.outcome in ('busy', 'unavailable', 'network_error', 'needs_manual') and rendered.error_code:
        return rendered
    return classify_result(render_request, replace(rendered, transport='browser'))


def fetch_or_raise(request, **kwargs):
    result = fetch(request, **kwargs)
    if not result.ok:
        raise FetchFailure(result)
    return result
