"""Classify actual client exceptions without sockets, models or website reads."""
from types import SimpleNamespace
import ssl
import unittest
from unittest.mock import Mock, patch

import aiohttp
from backend.scraper.acquisition import FetchRequest
from backend.scraper.acquisition import http_transport as transport
from backend.scraper.acquisition.coordinator import fetch, FetchFailure


class HTTPFailureClassificationTests(unittest.TestCase):
    def certificate_error(self):
        key = SimpleNamespace(host='faculty.example.edu', port=443, ssl=True, is_ssl=True)
        return aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError('certificate identity mismatch'))

    def observed(self, error, **trace):
        # Exercise fetch_http's exception dispatch and cleanup with the real
        # exception hierarchy; replacing the event loop prevents any socket.
        loop = Mock()
        loop.run_until_complete.side_effect = [error, None]
        def reader(request, permit, actual_trace, **kwargs):
            actual_trace.update(trace)
            return object()
        with patch.object(transport.asyncio, 'new_event_loop', return_value=loop), \
                patch.object(transport, '_shared_permit', return_value=None), \
                patch.object(transport, '_release_permit') as release, \
                patch.object(transport, 'read_http', new=Mock(side_effect=reader)), \
                patch('socket.socket.connect', side_effect=AssertionError('No network')):
            result = transport.fetch_http(FetchRequest('https://faculty.example.edu/', browser_allowed=False))
        release.assert_called_once_with(None)
        loop.close.assert_called_once()
        return result

    def test_certificate_error_stays_a_transport_failure_through_the_coordinator(self):
        error = self.certificate_error()
        self.assertIsInstance(error, ValueError)
        result = self.observed(error, notes=['verified_public_destination'])
        self.assertEqual((result.outcome, result.error_code),
                         ('network_error', 'http_clientconnectorcertificateerror'))
        self.assertIn('证书', result.message)
        self.assertIn('verified_public_destination', result.evidence)
        browser = Mock(side_effect=AssertionError('No browser bypass'))
        final = fetch(FetchRequest(result.final_url, browser_allowed=False),
                      http_transport=lambda _: result, browser_transport=browser)
        self.assertEqual((final.outcome, final.error_code), (result.outcome, result.error_code))
        self.assertFalse(final.ok)
        self.assertTrue(FetchFailure(final).retryable)
        browser.assert_not_called()

    def test_invalid_url_and_public_address_refusal_remain_denied(self):
        for error in (aiohttp.InvalidURL('bad address'), ValueError('Connection target must be public')):
            with self.subTest(error=type(error).__name__):
                result = self.observed(error)
                self.assertEqual((result.outcome, result.error_code), ('denied', 'invalid_public_url'))

    def test_dns_failure_retains_its_existing_network_identity(self):
        result = self.observed(ValueError('No public IPv4 address could be verified'))
        self.assertEqual((result.outcome, result.error_code), ('network_error', 'http_dns_failure'))

    def test_other_ssl_failures_keep_their_concrete_client_error_identity(self):
        key = SimpleNamespace(host='faculty.example.edu', port=443, ssl=True, is_ssl=True)
        result = self.observed(aiohttp.ClientConnectorSSLError(key, ssl.SSLError('handshake failed')))
        self.assertEqual((result.outcome, result.error_code),
                         ('network_error', 'http_clientconnectorsslerror'))

    def test_existing_connect_idle_and_total_timeouts_remain_distinct(self):
        for error, code in ((aiohttp.ConnectionTimeoutError('connect'), 'http_connect_timeout'),
                            (aiohttp.SocketTimeoutError('idle'), 'http_idle_timeout'),
                            (TimeoutError('total'), 'http_budget_exhausted')):
            with self.subTest(code=code):
                result = self.observed(error)
                self.assertEqual((result.outcome, result.error_code), ('network_error', code))

    def test_an_already_observed_login_redirect_keeps_its_access_rule(self):
        result = self.observed(self.certificate_error(),
            redirect_target='https://id.example.edu/cas/login?service=https%3A%2F%2Ffaculty.example.edu%2F')
        self.assertEqual((result.outcome, result.error_code), ('denied', 'source_login_required'))
