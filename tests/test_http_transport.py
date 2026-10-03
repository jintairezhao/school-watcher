"""HTTP fallback must preserve public destinations, TLS checks and shared budgets."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from backend.scraper.http_client import PublicHTTPClient

TRANSPORT = 'backend.scraper.pinned_transport.pinned_request'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.pacing = patch('backend.scraper.http_client._pace_host')
        self.pacing.start()
        self.addCleanup(self.pacing.stop)
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

    def test_proxy_fallback_reuses_validated_addresses(self):
        with patch('requests.utils.get_environ_proxies', return_value={'https':'http://127.0.0.1:8086'}), \
             patch(TRANSPORT, side_effect=[requests.ReadTimeout('proxy'), self.response()]) as transport:
            result = PublicHTTPClient().get('https://www.example.edu.cn/', timeout=(6,16))
        self.assertEqual(result.status_code, 200)
        first, second = transport.call_args_list
        self.assertEqual(first.kwargs['addresses'], ['8.8.8.8'])
        self.assertEqual(second.kwargs['addresses'], first.kwargs['addresses'])
        self.assertFalse(second.kwargs['trust_env'])
        self.assertEqual(result._watcher_transport, ['direct_after_proxy_error:ReadTimeout'])

    def test_explicit_proxy_and_writes_do_not_silently_switch_transport(self):
        for method, kwargs in [('POST', {}), ('GET', {'proxies':{'https':'http://proxy.example'}})]:
            with patch('requests.utils.get_environ_proxies', return_value={'https':'http://proxy.example'}), \
                 patch(TRANSPORT, side_effect=requests.ReadTimeout('timeout')) as transport:
                with self.assertRaises(requests.ReadTimeout):
                    PublicHTTPClient().request(method, 'https://www.example.edu.cn/', **kwargs)
                self.assertEqual(transport.call_count, 1)

    def test_retry_redirect_cannot_reach_private_address(self):
        with patch('requests.utils.get_environ_proxies', return_value={'https':'http://127.0.0.1:8086'}), \
             patch(TRANSPORT, side_effect=[requests.exceptions.ProxyError('offline'), self.response(302,'http://127.0.0.1/secret')]) as transport:
            with self.assertRaises(ValueError):
                PublicHTTPClient().get('https://www.example.edu.cn/')
            self.assertEqual(transport.call_count, 2)

    def test_failure_without_proxy_has_no_duplicate_retry(self):
        with patch('requests.utils.get_environ_proxies', return_value={}), \
             patch(TRANSPORT, side_effect=requests.ConnectionError('offline')) as transport:
            with self.assertRaises(requests.ConnectionError):
                PublicHTTPClient().get('https://www.example.edu.cn/')
            self.assertEqual(transport.call_count, 1)

    def test_http_access_denial_is_not_retried(self):
        with patch(TRANSPORT, return_value=self.response(403)) as transport:
            self.assertEqual(PublicHTTPClient().get('https://www.example.edu.cn/').status_code, 403)
            self.assertEqual(transport.call_count, 1)

    def test_a_sign_in_hop_that_cannot_be_completed_reports_the_site_rule(self):
        # A hand-off that cannot be completed never returns a page for the
        # classifier to read, so "this needs an account" would otherwise reach the
        # reader as a connection error, and onward as a parser problem. The TLS
        # EOF below stands for any transport failure on the provider's address --
        # refused handshake, reset, timeout. It is a shape, not a measurement of
        # one host: a real provider's host was observed failing this way once and
        # then answering normally on retest, which is exactly why the program must
        # not depend on which transport error it happens to get.
        from backend.scraper.fetch_errors import SourceLoginRequired
        sign_in = 'https://id.example.edu.cn/cas/login?service=https%3A%2F%2Fwww.example.edu.cn%2Ftzgg.htm'
        with patch('requests.utils.get_environ_proxies', return_value={}), \
             patch(TRANSPORT, side_effect=[self.response(302, sign_in),
                     requests.exceptions.SSLError('SSL: UNEXPECTED_EOF_WHILE_READING')]):
            with self.assertRaises(SourceLoginRequired) as caught:
                PublicHTTPClient().get('https://www.example.edu.cn/tzggcontent.jsp?wbnewsid=1')
        self.assertEqual(str(caught.exception), sign_in)

    def test_an_ordinary_hop_that_cannot_be_completed_stays_a_transport_error(self):
        # The same failure without a sign-in hand-off is the network, and must not
        # be reported to anyone as an access rule the site imposed.
        from backend.scraper.fetch_errors import SourceLoginRequired
        with patch('requests.utils.get_environ_proxies', return_value={}), \
             patch(TRANSPORT, side_effect=[self.response(302, 'https://www.example.edu.cn/tzgg/'),
                     requests.exceptions.SSLError('SSL: UNEXPECTED_EOF_WHILE_READING')]):
            with self.assertRaises(requests.exceptions.SSLError) as caught:
                PublicHTTPClient().get('https://www.example.edu.cn/tzggcontent.jsp?wbnewsid=1')
        self.assertNotIsInstance(caught.exception, SourceLoginRequired)

    def test_a_completed_sign_in_hop_reaches_the_classifier_as_a_page(self):
        # The hop succeeding is not an error here: the provider's own page is what
        # came back, and it is the classifier's judgement to make.
        sign_in = 'https://id.example.edu.cn/cas/login?service=https%3A%2F%2Fwww.example.edu.cn%2Ftzgg.htm'
        with patch(TRANSPORT, side_effect=[self.response(302, sign_in), self.response()]):
            result = PublicHTTPClient().get('https://www.example.edu.cn/tzggcontent.jsp?wbnewsid=1')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.url, 'https://www.example.edu.cn/')

    def test_same_host_duplicated_https_redirect_preserves_path_and_query(self):
        url = 'http://www.example.edu.cn/notices/?page=2'
        malformed = 'https://www.example.edu.cn' + url
        with patch(TRANSPORT, side_effect=[self.response(302, malformed), self.response()]) as transport:
            result = PublicHTTPClient().get(url)
        self.assertEqual(transport.call_args_list[1].args[1], 'https://www.example.edu.cn/notices/?page=2')
        self.assertIn('repaired_same_host_https_redirect:' + malformed, result._watcher_transport)

    def test_redirect_dns_is_validated_again(self):
        with patch('backend.scraper.pinned_transport.public_addresses', side_effect=[['8.8.8.8'],ValueError('private')]), \
             patch(TRANSPORT, return_value=self.response(302,'https://other.invalid/')) as transport:
            with self.assertRaises(ValueError):
                PublicHTTPClient().get('https://www.example.edu.cn/')
            self.assertEqual(transport.call_count, 1)

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
                with lock: active -= 1
        with patch(TRANSPORT, side_effect=transport), ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _:PublicHTTPClient().get('https://www.example.edu.cn/'),range(16)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.assertEqual(peak, 2)


class PinnedTransportSecurityTests(unittest.TestCase):
    def test_rebinding_cannot_change_actual_tcp_destination(self):
        from backend.scraper.pinned_transport import pinned_request
        answers = [(2,1,6,'',('93.184.216.34',80))]
        with patch('socket.getaddrinfo', side_effect=[answers,[(2,1,6,'',('127.0.0.1',80))]]) as dns, \
             patch('urllib3.util.connection.create_connection', side_effect=OSError('isolated connection probe')) as connect, \
             patch('backend.scraper.http_client._pace_host'):
            with self.assertRaises(requests.ConnectionError):
                pinned_request('GET','http://audit-rebind.invalid/',trust_env=False,timeout=1)
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ('93.184.216.34',80))

    def test_tls_uses_original_identity_but_numeric_connect_target(self):
        from backend.scraper.pinned_transport import PinnedAdapter
        adapter = PinnedAdapter('https://www.example.edu.cn/','93.184.216.34')
        request = requests.Request('GET','https://www.example.edu.cn/').prepare()
        try:
            with patch.object(adapter.poolmanager,'connection_from_host') as connect:
                adapter.get_connection_with_tls_context(request,True)
                self.assertEqual(connect.call_args.kwargs['host'],'93.184.216.34')
                options = connect.call_args.kwargs['pool_kwargs']
                self.assertEqual(options['server_hostname'],'www.example.edu.cn')
                self.assertEqual(options['assert_hostname'],'www.example.edu.cn')
            with self.assertRaises(ValueError):
                adapter.get_connection_with_tls_context(request,False)
            adapter.add_headers(request)
            self.assertEqual(request.headers['Host'],'www.example.edu.cn')
        finally:
            adapter.close()

    def test_host_header_omits_default_ports_but_keeps_custom_ports(self):
        from backend.scraper.pinned_transport import PinnedAdapter
        for url, expected in (
            ('https://www.example.edu.cn:443/', 'www.example.edu.cn'),
            ('http://www.example.edu.cn:80/', 'www.example.edu.cn'),
            ('https://www.example.edu.cn:8443/', 'www.example.edu.cn:8443'),
            ('http://www.example.edu.cn:8080/', 'www.example.edu.cn:8080'),
            ('https://[2606:4700:4700::1111]:443/', '[2606:4700:4700::1111]'),
        ):
            with self.subTest(url=url):
                adapter = PinnedAdapter(url, '93.184.216.34')
                try:
                    request = requests.Request('GET', url).prepare()
                    adapter.add_headers(request)
                    self.assertEqual(request.headers['Host'], expected)
                finally:
                    adapter.close()

    def test_http_proxy_uses_numeric_absolute_uri(self):
        from backend.scraper.pinned_transport import PinnedAdapter
        adapter = PinnedAdapter('http://www.example.edu.cn/','93.184.216.34')
        try:
            request = requests.Request('GET','http://www.example.edu.cn/a?b=1').prepare()
            self.assertEqual(adapter.request_url(request,{'http':'http://127.0.0.1:8080'}), 'http://93.184.216.34/a?b=1')
            adapter.add_headers(request)
            self.assertEqual(request.headers['Host'],'www.example.edu.cn')
        finally:
            adapter.close()

    def test_proxy_https_connect_target_is_numeric(self):
        from backend.scraper.pinned_transport import PinnedAdapter
        adapter = PinnedAdapter('https://www.example.edu.cn/','93.184.216.34')
        try:
            request = requests.Request('GET','https://www.example.edu.cn/a').prepare()
            with patch.object(adapter,'proxy_manager_for') as manager:
                adapter.get_connection_with_tls_context(request,True,{'https':'http://127.0.0.1:8080'})
                arguments = manager.return_value.connection_from_host.call_args.kwargs
                self.assertEqual(arguments['host'],'93.184.216.34')
                self.assertEqual(arguments['pool_kwargs']['server_hostname'],'www.example.edu.cn')
        finally:
            adapter.close()


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
