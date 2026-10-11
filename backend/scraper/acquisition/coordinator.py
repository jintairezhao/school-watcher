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
from .contracts import FetchRequest, FetchResult
from .classifier import classify_result
from .profiles import api_request, configured_request, map_api_response

_browser_dispatch = ContextVar('acquisition_browser_dispatch', default=None)
_cache_lookup = ContextVar('acquisition_cache_lookup', default=None)
_cache_store = ContextVar('acquisition_cache_store', default=None)
_before_fetch = ContextVar('acquisition_before_fetch', default=None)


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
            result = classify_result(request, observed)
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
    if rendered.outcome in ('busy', 'unavailable', 'network_error', 'needs_manual') and rendered.error_code:
        return rendered
    return classify_result(render_request, replace(rendered, transport='browser'))


def fetch_or_raise(request, **kwargs):
    result = fetch(request, **kwargs)
    if not result.ok:
        raise FetchFailure(result)
    return result
