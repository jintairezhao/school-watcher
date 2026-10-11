"""Acceptance hooks are observation-only and never make paid provider calls."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp
import requests

from scripts.checks.acceptance_source_agent import HTTPMeasurements, pending_within_window, unsent_proxy_failure, write_json
from scripts.checks.acceptance_browser import instrument


HTML = '<html><body><h1>通知公告</h1><a href="/one.html">学生奖学金申请通知</a></body></html>'


class _Pages(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', '/body'); self.end_headers()
        else:
            data = HTML.encode('utf-8')
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)


class AcceptanceInstrumentationTests(unittest.TestCase):
    def test_transient_windows_report_lock_only_retries_the_local_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            replace = Path.replace
            calls = []
            def locked_once(source, destination):
                calls.append(destination)
                if len(calls) == 1:
                    raise PermissionError('temporary reader lock')
                return replace(source, destination)
            with patch.object(Path, 'replace', locked_once), patch('scripts.checks.acceptance_source_agent.time.sleep'):
                write_json(target, {'actual_requests': 3})
            self.assertEqual(json.loads(target.read_text()), {'actual_requests': 3})
            self.assertEqual(len(calls), 2)

    def test_retry_jitter_and_interrupted_lease_wait_until_window_end(self):
        now = datetime.utcnow()
        pending = SimpleNamespace(state='pending', available_at=now + timedelta(seconds=144))
        running = SimpleNamespace(state='running', lease_until=now + timedelta(seconds=120))
        self.assertTrue(pending_within_window([pending], 180, now))
        self.assertTrue(pending_within_window([running], 180, now))
        self.assertFalse(pending_within_window([pending], 140, now))
        self.assertFalse(pending_within_window([], 180, now))

    def test_only_official_hosts_are_measured_and_pinned_host_is_restored(self):
        meter = HTTPMeasurements([], lambda: None, threading.RLock(), 'https://www.cupk.edu.cn/')
        self.assertEqual(meter.destination('https://8.8.8.8/notices', {'Host': 'jwc.cupk.edu.cn'}),
                         ('jwc.cupk.edu.cn', 'https://jwc.cupk.edu.cn/notices'))
        self.assertIsNone(meter.destination('https://api.deepseek.com/chat/completions'))
        self.assertIsNone(meter.destination('http://127.0.0.1:1234/internal/browser-origin/reserve'))

    def test_proxy_connection_failure_is_distinguished_from_unknown_delivery(self):
        from urllib3.exceptions import ProxyError, NewConnectionError, MaxRetryError
        known = requests.exceptions.ProxyError(MaxRetryError(None, '/',
            ProxyError('proxy unavailable', NewConnectionError(None, 'refused'))))
        self.assertTrue(unsent_proxy_failure(known))
        self.assertFalse(unsent_proxy_failure(requests.ReadTimeout('after send')))
        self.assertFalse(unsent_proxy_failure(aiohttp.ServerDisconnectedError()))
        records = []
        meter = HTTPMeasurements(records, lambda: None, threading.RLock(), 'https://www.cupk.edu.cn/')
        observe = meter.begin('GET', 'https://www.cupk.edu.cn/', {}, 'requests', True)
        observe(error=known, complete=True)
        self.assertEqual(records[0]['request_evidence'], 'proxy_connection_failed_before_send')

    def test_each_http_hop_is_counted_with_body_bytes_without_changing_responses(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Pages)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        records = []
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with ExitStack() as stack:
                meter = HTTPMeasurements(records, lambda: None, threading.RLock(), url)
                meter.install(stack)
                with requests.Session() as session:
                    session.trust_env = False
                    with session.get(url + '/redirect', allow_redirects=False) as first:
                        self.assertEqual(first.status_code, 302)
                    with session.get(url + '/body', headers={'Authorization': 'fixture-secret'}) as second:
                        self.assertEqual(second.content.decode(), HTML)
                async def read():
                    async with aiohttp.ClientSession(trust_env=False) as session:
                        async with session.get(url + '/redirect', allow_redirects=False) as first:
                            self.assertEqual(first.status, 302)
                        async with session.get(url + '/body') as second:
                            self.assertEqual((await second.read()).decode(), HTML)
                asyncio.run(read())
            self.assertEqual([item['status'] for item in records], [302, 200, 302, 200])
            self.assertEqual([item['bytes'] for item in records], [0, len(HTML.encode()), 0, len(HTML.encode())])
            self.assertTrue(all(item['complete'] for item in records))
            self.assertNotIn('fixture-secret', json.dumps(records))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_browser_records_content_and_actual_busy_outcome(self):
        class Runtime:
            async def _navigate(self, session, payload):
                if payload.get('busy'):
                    return {'final_url': payload['url'], 'outcome': 'busy', 'error_code': 'origin_busy'}
                return {'final_url': payload['url'], 'html': HTML, 'status_code': 200, 'transport': 'browser'}
        payload = {'url': 'https://example.edu.cn/notices', 'purpose': 'list', 'request_id': 'fixture'}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'browser.jsonl'
            instrument(Runtime, output)
            result = asyncio.run(Runtime()._navigate(None, payload))
            asyncio.run(Runtime()._navigate(None, dict(payload, busy=True)))
            records = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(result['html'], HTML)
        self.assertEqual(records[0]['bytes'], len(HTML.encode()))
        self.assertEqual(records[0]['html_sha256'], hashlib.sha256(HTML.encode()).hexdigest())
        self.assertEqual(records[1]['outcome'], 'busy')
        self.assertEqual(records[1]['error_code'], 'origin_busy')


if __name__ == '__main__': unittest.main()
