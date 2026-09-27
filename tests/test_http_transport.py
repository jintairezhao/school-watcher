"""A failing local proxy must not make public official directories disappear."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from backend.scraper.http_client import PublicHTTPClient


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.pacing = patch('backend.scraper.http_client._pace_host')
        self.pacing.start()
        self.addCleanup(self.pacing.stop)
        self.env = patch.dict('os.environ', {'WATCHER_ENHANCED_HTTP': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def response(self, status=200, location=None):
        response = requests.Response()
        response.status_code = status
        response.url = 'https://www.example.edu.cn/'
        response._content = b'<html>Official directory</html>'
        response._content_consumed = True
        if location:
            response.headers['Location'] = location
        return response

    def test_retry_public_get_directly_after_environment_proxy_timeout(self):
        direct = MagicMock()
        direct.request.return_value = self.response()
        with patch('requests.utils.get_environ_proxies', return_value={'https': 'http://127.0.0.1:8086'}), \
             patch('requests.request', side_effect=requests.exceptions.ReadTimeout('proxy timeout')), \
             patch('requests.Session') as session:
            session.return_value.__enter__.return_value = direct
            result = PublicHTTPClient().get('https://www.example.edu.cn/', timeout=(6, 16))
        self.assertEqual(result.status_code, 200)
        self.assertFalse(direct.trust_env)
        self.assertEqual(result._watcher_transport, ['direct_after_proxy_error:ReadTimeout'])
        self.assertEqual(direct.request.call_args.kwargs['allow_redirects'], False)
        self.assertNotEqual(direct.request.call_args.kwargs.get('verify'), False)

    def test_explicit_proxy_or_non_read_request_does_not_silently_switch_transport(self):
        with patch('requests.utils.get_environ_proxies', return_value={'https': 'http://127.0.0.1:8086'}), \
             patch('requests.request', side_effect=requests.exceptions.ReadTimeout('timeout')), \
             patch('requests.Session') as session:
            for method, kwargs in [('POST', {}), ('GET', {'proxies': {'https': 'http://proxy.example'}})]:
                with self.assertRaises(requests.exceptions.ReadTimeout):
                    PublicHTTPClient().request(method, 'https://www.example.edu.cn/', **kwargs)
            session.assert_not_called()

    def test_direct_retry_redirect_still_cannot_reach_a_private_address(self):
        direct = MagicMock()
        direct.request.return_value = self.response(302, 'http://127.0.0.1/secret')
        with patch('requests.utils.get_environ_proxies', return_value={'https': 'http://127.0.0.1:8086'}), \
             patch('requests.request', side_effect=requests.exceptions.ProxyError('offline')), \
             patch('requests.Session') as session:
            session.return_value.__enter__.return_value = direct
            with self.assertRaises(ValueError):
                PublicHTTPClient().get('https://www.example.edu.cn/')
        self.assertEqual(direct.request.call_count, 1)

    def test_failure_without_a_proxy_has_no_duplicate_retry(self):
        with patch('requests.utils.get_environ_proxies', return_value={}), \
             patch('requests.request', side_effect=requests.exceptions.ConnectionError('offline')), \
             patch('requests.Session') as session:
            with self.assertRaises(requests.exceptions.ConnectionError):
                PublicHTTPClient().get('https://www.example.edu.cn/')
            session.assert_not_called()

    def test_http_access_denial_does_not_trigger_transport_retry(self):
        with patch('requests.request', return_value=self.response(403)), patch('requests.Session') as session:
            self.assertEqual(PublicHTTPClient().get('https://www.example.edu.cn/').status_code, 403)
            session.assert_not_called()

    def test_exact_same_host_duplicated_https_redirect_preserves_path_and_query(self):
        url = 'http://www.example.edu.cn/notices/?page=2'
        malformed = 'https://www.example.edu.cn' + url
        with patch('requests.request', side_effect=[self.response(302, malformed), self.response()]) as request:
            result = PublicHTTPClient().get(url)
        self.assertEqual(request.call_args_list[1].args[1], 'https://www.example.edu.cn/notices/?page=2')
        self.assertIn('repaired_same_host_https_redirect:' + malformed, result._watcher_transport)

    def test_nested_redirect_pointing_elsewhere_is_not_rewritten_to_another_destination(self):
        url = 'http://www.example.edu.cn/notices/'
        location = 'https://www.example.edu.cnhttp://other.example.org/secret'
        with patch('requests.request', side_effect=[self.response(302, location), self.response()]) as request:
            result = PublicHTTPClient().get(url)
        self.assertEqual(request.call_args_list[1].args[1], location)
        self.assertEqual(result._watcher_transport, [])

    def test_outbound_concurrency_is_shared_by_client_instances(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Lock
        import time
        lock = Lock()
        active = peak = 0

        def transport(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.02)
                return self.response()
            finally:
                with lock:
                    active -= 1

        with patch('requests.request', side_effect=transport), ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: PublicHTTPClient().get('https://www.example.edu.cn/'), range(16)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.assertEqual(peak, 2)


class HostPacingTests(unittest.TestCase):
    def test_all_request_paths_share_host_start_spacing(self):
        from backend.scraper import http_client
        clock=[0.0]
        sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds);clock[0]+=seconds
        http_client._host_next.clear()
        try:
            with patch.object(http_client.time,'monotonic',side_effect=lambda:clock[0]),patch.object(http_client.time,'sleep',side_effect=sleep):
                http_client._pace_host('https://www.example.edu.cn/a')
                http_client._pace_host('https://other.edu.cn/a')
                http_client._pace_host('http://www.example.edu.cn/b')
                http_client._pace_host('https://www.example.edu.cn/c')
            self.assertEqual(sleeps,[1.0,1.0])
        finally:http_client._host_next.clear()


if __name__ == '__main__':
    unittest.main()
