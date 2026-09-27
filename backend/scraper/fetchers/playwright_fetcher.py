"""Deprecated browser helpers forwarding to the process-owned browser service.

No caller imports Playwright here. Runtime ownership and session isolation belong
to the browser service; use acquisition.fetch for ordinary collection.
"""
import os
from uuid import uuid4

from backend.scraper.acquisition import FetchRequest, FetchResult, FetchedHTML, FetchFailure, classify_result
from backend.scraper.acquisition.browser_client import BrowserClient


def is_playwright_available():
    return bool(os.environ.get('WATCHER_BROWSER_URL') and os.environ.get('WATCHER_BROWSER_TOKEN'))


def fetch_html_with_browser(url, timeout_ms=45000, **kwargs):
    request = FetchRequest(url, timeout_seconds=min(90, max(1, timeout_ms / 1000)),
                           request_id=uuid4().hex, readiness_selector=kwargs.get('wait_selector', ''))
    result = BrowserClient().fetch(request)
    if not result.ok and result.error_code:
        raise FetchFailure(result)
    result = classify_result(request, result)
    if not result.ok:
        raise FetchFailure(result)
    return FetchedHTML(result)


def is_js_required(html):
    result = classify_result(FetchRequest('https://example.edu.cn/'),
                             FetchResult('https://example.edu.cn/', status=200, html=html))
    return result.outcome == 'requires_render'
