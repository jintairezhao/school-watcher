"""Replay redirect routes without sockets, website calls or production data."""
from contextlib import contextmanager
from types import SimpleNamespace
import ssl
import unittest
from unittest.mock import Mock, patch

import aiohttp

from backend.scraper.acquisition import FetchRequest
from backend.scraper.acquisition import http_transport as transport
from backend.scraper.http_client import retry_direct_for
from backend.services import tasks


ROOT = 'http://faculty.example.edu/'
HTTPS = 'https://faculty.example.edu/'
IP = 'https://93.184.216.34/'
OTHER = 'https://other.example.edu/'
PROXY = 'http://proxy.test:8080'


def complete(coroutine):
    """All fake I/O completes immediately, so no event-loop socket is needed."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected real asynchronous I/O')


async def immediately_wait(awaitable, **kwargs):
    return await awaitable


async def immediately_call(function, *args):
    return function(*args)


class Deadline:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Response:
    def __init__(self, status=200, location=None):
        self.status = status
        self.headers = {'Content-Type': 'text/html'}
        if location:
            self.headers['Location'] = location
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        yield b'<html><title>Fixture</title><body>Verified response</body></html>'


class RedirectRouteMemoryTests(unittest.TestCase):
    @contextmanager
    def replay(self, responses, *, proxies=None, checkpoint=None, attempts=0, persist_memory=False,
               execution_handle=None):
        calls = []
        handle = execution_handle if execution_handle is not None else {'attempts': attempts, 'checkpoint': checkpoint or {}}
        responses = iter(responses)

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                calls.append((url, kwargs))
                result = next(responses)
                if isinstance(result, Exception):
                    raise result
                return result

        loop = SimpleNamespace(run_until_complete=complete, shutdown_asyncgens=lambda: immediately_call(lambda: None),
                               close=Mock())
        def save(data):
            handle['checkpoint'] = data

        from backend.services.http_route_memory import remember_verified_direct
        save_checkpoint = tasks.checkpoint
        with patch.object(transport.asyncio, 'new_event_loop', return_value=loop), \
                patch.object(transport.asyncio, 'timeout', return_value=Deadline()), \
                patch.object(transport.asyncio, 'wait_for', new=immediately_wait), \
                patch.object(transport.asyncio, 'to_thread', new=immediately_call), \
                patch.object(transport.aiohttp, 'ClientSession', return_value=Session()), \
                patch.object(transport, 'public_addresses', return_value=['93.184.216.34']), \
                patch.object(transport, '_shared_permit', return_value=None), \
                patch.object(transport, '_release_permit'), \
                patch.object(transport, 'get_environ_proxies', side_effect=proxies or (lambda _: {'all': PROXY})), \
                patch('backend.services.http_route_memory.get_environ_proxies', return_value={'all': PROXY}), \
                patch('backend.services.http_route_memory.prefer_verified_direct', return_value=False), \
                patch('backend.services.http_route_memory.remember_verified_direct',
                      wraps=remember_verified_direct if persist_memory else None) as learn, \
                patch.object(tasks, 'checkpoint', wraps=save_checkpoint if persist_memory else None,
                             side_effect=None if persist_memory else save), \
                patch('socket.socket.connect', side_effect=AssertionError('No network')), \
                tasks.execution_scope(handle):
            yield handle, calls, learn

    def certificate_error(self):
        key = SimpleNamespace(host='93.184.216.34', port=443, ssl=True, is_ssl=True)
        return aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError('identity mismatch'))

    def retry_checkpoint(self):
        return {'http_retry_direct': {'faculty.example.edu': 0}}

    def test_failed_ip_redirect_records_original_entrance_for_a_later_attempt(self):
        with self.replay([Response(301, IP), self.certificate_error()]) as (handle, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.error_code, 'http_clientconnectorcertificateerror')
            self.assertEqual(len(calls), 2)  # No immediate direct resend after the sent failure.
            self.assertEqual([call[1]['headers']['Host'] for call in calls],
                             ['faculty.example.edu', '93.184.216.34'])
            self.assertFalse(retry_direct_for(ROOT))
            handle['attempts'] = 1
            self.assertTrue(retry_direct_for(ROOT))
            self.assertTrue(retry_direct_for(IP))
            learn.assert_not_called()
            self.assertTrue(all('ssl' not in options for _, options in calls))

    def test_successful_domain_upgrade_remembers_the_actual_direct_entrance(self):
        # Only HTTP has a proxy; the final HTTPS hop has no proxy to bypass.
        with self.replay([Response(301, HTTPS), Response()], checkpoint=self.retry_checkpoint(), attempts=1,
                         proxies=lambda url: {'http': PROXY} if url == ROOT else {}) as (_, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual((result.status, result.final_url), (200, HTTPS))
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(options.get('proxy') is None for _, options in calls))
            learn.assert_called_once_with(ROOT)

    def test_only_direct_hops_receive_success_memory_on_a_mixed_route(self):
        with self.replay([Response(301, OTHER), Response()], checkpoint=self.retry_checkpoint(), attempts=1) as (_, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.status, 200)
            self.assertIsNone(calls[0][1]['proxy'])
            self.assertEqual(calls[1][1]['proxy'], PROXY)
            learn.assert_called_once_with(ROOT)

    def test_gateway_failure_after_redirect_keeps_both_retry_origins(self):
        with self.replay([Response(301, OTHER), Response(503)]) as (handle, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.status, 503)
            self.assertIn('proxy_route_retry_available', result.evidence)
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(handle['checkpoint']['http_retry_direct']),
                             {'faculty.example.edu', 'other.example.edu'})
            learn.assert_not_called()

    def test_required_proxy_failure_never_schedules_direct_bypass(self):
        with self.replay([Response(301, IP), self.certificate_error()]) as (handle, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT, policy={'proxy_required': True}))
            self.assertEqual(result.outcome, 'network_error')
            self.assertEqual(len(calls), 2)
            self.assertNotIn('http_retry_direct', handle['checkpoint'])
            learn.assert_not_called()

    def test_final_direct_failure_does_not_blame_an_earlier_successful_proxy_hop(self):
        with self.replay([Response(301, OTHER), aiohttp.ServerDisconnectedError()],
                         proxies=lambda url: {'all': PROXY} if url == ROOT else {}) as (handle, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.outcome, 'network_error')
            self.assertEqual(len(calls), 2)
            self.assertNotIn('http_retry_direct', handle['checkpoint'])
            learn.assert_not_called()

    def test_failed_direct_retry_does_not_publish_success_memory(self):
        with self.replay([Response(301, HTTPS), self.certificate_error()],
                         checkpoint=self.retry_checkpoint(), attempts=1) as (_, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.outcome, 'network_error')
            self.assertTrue(all(options.get('proxy') is None for _, options in calls))
            learn.assert_not_called()

    def test_two_successful_direct_hops_keep_their_own_observations(self):
        with self.replay([Response(301, HTTPS), Response()], checkpoint=self.retry_checkpoint(), attempts=1) as (_, calls, learn):
            result = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(result.status, 200)
            self.assertEqual([call.args[0] for call in learn.call_args_list], [ROOT, HTTPS])
            self.assertEqual(len(calls), 2)


class RedirectRoutePersistenceTests(unittest.TestCase):
    def test_failed_redirect_then_durable_direct_success_is_reused_by_a_new_read(self):
        from datetime import datetime, timedelta
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        from backend.services import http_route_memory

        fixture = DirectOnboardingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        replay = RedirectRouteMemoryTests()
        tasks.enqueue('content', 1)
        first = tasks.claim()
        with patch('backend.services.http_route_memory.get_environ_proxies', return_value={'all': PROXY}):
            with replay.replay([Response(301, IP), replay.certificate_error()],
                               execution_handle=first, persist_memory=True):
                failed = transport.fetch_http(FetchRequest(ROOT))
            self.assertEqual(failed.outcome, 'network_error')
            self.assertFalse(http_route_memory.prefer_verified_direct(ROOT))
            self.assertTrue(tasks.finish(first, error='fixture certificate failure'))
            # Advance only this temporary task's due time. Claiming it again must
            # load the saved checkpoint with a fresh owner token and attempt.
            BackgroundTask.query.filter_by(id=first['id']).update(
                {'available_at': datetime.utcnow() - timedelta(seconds=1)})
            db.session.commit()
            second = tasks.claim()
            self.assertEqual(second['id'], first['id'])
            self.assertNotEqual(second['token'], first['token'])
            self.assertEqual(second['attempts'], 1)
            with replay.replay([Response(301, HTTPS), Response()], execution_handle=second,
                               persist_memory=True) as (_, calls, _):
                success = transport.fetch_http(FetchRequest(ROOT))
                self.assertEqual(success.status, 200, success.to_dict())
                self.assertTrue(all(options.get('proxy') is None for _, options in calls))
            self.assertTrue(tasks.finish(second))
            self.assertTrue(http_route_memory.prefer_verified_direct(ROOT))
            self.assertTrue(retry_direct_for('https://faculty.example.edu/another-page'))
            self.assertFalse(http_route_memory.prefer_verified_direct(IP))
            self.assertFalse(http_route_memory.prefer_verified_direct(OTHER))
            with patch('backend.services.http_route_memory.get_environ_proxies',
                       return_value={'all': 'http://changed-proxy.test:8080'}):
                self.assertFalse(retry_direct_for(ROOT))
