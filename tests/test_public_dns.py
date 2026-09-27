import ipaddress
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper import public_dns


class DNSResponseSocket:
    address = '118.228.208.81'
    wrong_id = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, seconds):
        pass

    def connect(self, destination):
        pass

    def send(self, query):
        self.query = query

    def recv(self, maximum):
        ident = struct.unpack('!H', self.query[:2])[0]
        if self.wrong_id:
            ident = (ident + 1) % 65536
        return (struct.pack('!6H', ident, 0x8180, 1, 1, 0, 0) + self.query[12:]
                + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + ipaddress.ip_address(self.address).packed)


class PublicDNSTests(unittest.TestCase):
    def setUp(self):
        public_dns._cache.clear()
        DNSResponseSocket.address = '118.228.208.81'
        DNSResponseSocket.wrong_id = False
        self.http = patch.object(public_dns.requests, 'get', side_effect=public_dns.requests.ConnectionError('HTTPS unavailable'))
        self.http_mock = self.http.start()
        self.addCleanup(self.http.stop)

    def https_response(self, host='www.example.edu.cn', address='118.228.208.81'):
        response=public_dns.requests.Response()
        response.status_code=200
        response._content=json.dumps({'Status':0,'Question':{'name':host+'.','type':1},
            'Answer':[{'name':host+'.','type':1,'TTL':60,'data':address}]}).encode()
        response._content_consumed=True
        return response

    def test_udp_timeout_uses_verified_https_answer_and_cache(self):
        self.http_mock.side_effect=None
        self.http_mock.return_value=self.https_response()
        with patch.object(public_dns.socket,'socket',side_effect=TimeoutError('UDP unavailable')):
            self.assertEqual(public_dns.public_ipv4('www.example.edu.cn'),['118.228.208.81'])
            self.assertEqual(public_dns.public_ipv4('www.example.edu.cn'),['118.228.208.81'])
        self.assertEqual(self.http_mock.call_count,1)
        self.assertFalse(self.http_mock.call_args.kwargs['allow_redirects'])
        self.assertNotEqual(self.http_mock.call_args.kwargs.get('verify'),False)

    def test_https_answer_cannot_substitute_private_or_unrelated_destination(self):
        self.http_mock.side_effect=None
        for host,address in [('www.example.edu.cn','10.0.1.71'),('other.edu.cn','118.228.208.81')]:
            self.http_mock.return_value=self.https_response(host,address)
            with patch.object(public_dns.socket,'socket',side_effect=TimeoutError('UDP unavailable')):
                with self.assertRaises(ValueError):public_dns.public_ipv4('www.example.edu.cn')

    def test_public_response_is_validated_and_cached(self):
        with patch.object(public_dns.socket, 'socket', return_value=DNSResponseSocket()) as sock:
            self.assertEqual(public_dns.public_ipv4('www.example.edu.cn'), ['118.228.208.81'])
            self.assertEqual(public_dns.public_ipv4('www.example.edu.cn'), ['118.228.208.81'])
            self.assertEqual(sock.call_count, 1)

    def test_private_dns_answer_is_not_used(self):
        DNSResponseSocket.address = '10.0.1.71'
        with patch.object(public_dns.socket, 'socket', return_value=DNSResponseSocket()):
            with self.assertRaises(ValueError):
                public_dns.public_ipv4('www.example.edu.cn')

    def test_unrelated_response_is_rejected(self):
        DNSResponseSocket.wrong_id = True
        with patch.object(public_dns.socket, 'socket', return_value=DNSResponseSocket()):
            with self.assertRaises(ValueError):
                public_dns.public_ipv4('www.example.edu.cn')

    def test_pinned_https_preserves_certificate_and_host_checks(self):
        raw = MagicMock(status=200, headers={'Content-Type': 'text/html'})
        with patch.object(public_dns, 'public_ipv4', return_value=['118.228.208.81']), patch.object(
                public_dns.urllib3, 'HTTPSConnectionPool') as factory:
            factory.return_value.urlopen.return_value = raw
            response = public_dns.pinned_request('GET', 'https://www.example.edu.cn/units', stream=True)
            kwargs = factory.call_args.kwargs
            self.assertEqual(kwargs['server_hostname'], 'www.example.edu.cn')
            self.assertEqual(kwargs['assert_hostname'], 'www.example.edu.cn')
            self.assertEqual(kwargs['cert_reqs'], 'CERT_REQUIRED')
            self.assertEqual(factory.return_value.urlopen.call_args.kwargs['headers']['Host'], 'www.example.edu.cn')
            response.close()
            factory.return_value.close.assert_called_once()

    def test_private_literal_cannot_reach_fallback(self):
        from backend.scraper.http_client import requests
        with patch.object(public_dns, 'pinned_request') as pinned:
            with self.assertRaises(ValueError):
                requests.get('https://127.0.0.1/')
            pinned.assert_not_called()

    def test_static_fetch_also_rejects_private_literal(self):
        from backend.scraper.engine import _fetch_html
        with self.assertRaises(ValueError):
            _fetch_html('http://10.0.1.71/')


if __name__ == '__main__':
    unittest.main()
