"""Authenticated internal browser API. It never owns a second durable queue."""
import os
import time
from urllib.parse import quote

import requests

from .contracts import FetchResult


class BrowserClient:
    def __init__(self, base_url=None, token=None, session=None):
        self.base_url = (base_url or os.environ.get('WATCHER_BROWSER_URL', '')).rstrip('/')
        self.token = token if token is not None else os.environ.get('WATCHER_BROWSER_TOKEN', '')
        self.session = session or requests.Session()
        self.session.trust_env = False

    def _call(self, method, path, payload=None):
        if not self.base_url or not self.token:
            return 'failed', FetchResult('', outcome='unavailable', error_code='browser_not_configured',
                                         message='浏览器服务尚未配置，动态来源暂时无法更新')
        try:
            response = self.session.request(method, self.base_url + path, json=payload,
                    headers={'X-Watcher-Token': self.token}, timeout=(3, 8))
            try:
                value = response.json()
            except ValueError:
                value = {}
            if value.get('error'):
                error = value['error']
                code = error.get('code', 'browser_failed')
                if code in ('needs_manual', 'human_verification', 'verification_required'):
                    outcome = 'needs_manual'
                elif code in ('browser_busy', 'session_busy', 'verification_busy'):
                    outcome = 'busy'
                elif code in ('invalid_url', 'blocked_destination'):
                    outcome = 'denied'
                elif code in ('invalid_request', 'request_conflict', 'response_too_large'):
                    outcome = 'needs_adapter'
                elif code in ('browser_timeout', 'browser_failed'):
                    outcome = 'network_error'
                else:
                    outcome = 'unavailable'
                return ('busy' if outcome == 'busy' else value.get('state', 'failed')), FetchResult('', outcome=outcome, error_code=code,
                                message=error.get('message', '浏览器执行失败'),
                                retry_after=5 if outcome == 'busy' else None)
            if response.status_code in (429, 503):
                return 'busy', FetchResult('', outcome='busy', error_code='browser_busy',
                                           message='浏览器任务正在排队', retry_after=5)
            if response.status_code >= 400:
                return 'failed', FetchResult('', outcome='unavailable', error_code='browser_http_' + str(response.status_code),
                                             message='浏览器服务暂时不可用')
            state = value.get('state', 'done' if value.get('result') else 'failed')
            if value.get('result') is not None:
                return state, FetchResult.from_dict(value['result'])
            return state, None
        except (requests.RequestException, ValueError) as exc:
            return 'failed', FetchResult('', outcome='unavailable', error_code='browser_connection',
                                         message='浏览器服务连接失败', evidence=(type(exc).__name__,))

    def submit(self, request):
        return self._call('POST', '/v1/fetch', request.to_dict())

    def poll(self, request_id):
        return self._call('GET', '/v1/fetch/' + quote(str(request_id), safe=''))

    def fetch(self, request):
        state, result = self.submit(request)
        deadline = time.monotonic() + request.timeout_seconds + 5
        while state in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.2)
            state, result = self.poll(request.request_id)
        return result or FetchResult(request.url, transport='browser', outcome='busy',
                                      error_code='browser_pending', message='浏览器任务正在排队', retry_after=5)
