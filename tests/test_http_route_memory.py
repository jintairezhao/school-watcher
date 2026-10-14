import unittest
from unittest.mock import patch
from backend.services import tasks,http_route_memory
from backend.scraper.http_client import remember_proxy_failure,retry_direct_for

URL='https://fixture.example.edu/article/1'

class RouteMemoryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture=DirectOnboardingTests();self.fixture.setUp()
        self.proxy=patch('backend.services.http_route_memory.get_environ_proxies',return_value={'https':'http://proxy.test:8080'})
        self.proxy.start()
    def tearDown(self):self.proxy.stop();self.fixture.tearDown()
    def learn(self):
        tasks.enqueue('content',1)
        handle=tasks.claim()
        with tasks.execution_scope(handle):
            remember_proxy_failure(URL)
            http_route_memory.remember_verified_direct(URL)
        tasks.finish(handle)
    def test_verified_route_is_reused_for_other_notices_without_another_bad_proxy_probe(self):
        self.learn()
        self.assertTrue(retry_direct_for('https://fixture.example.edu/article/2'))
        self.assertFalse(retry_direct_for('https://other.example.edu/article/2'))
    def test_a_proxy_failure_alone_is_not_a_successful_route_configuration(self):
        tasks.enqueue('content',1);handle=tasks.claim()
        with tasks.execution_scope(handle):remember_proxy_failure(URL)
        tasks.finish(handle,error='timeout')
        self.assertFalse(http_route_memory.prefer_verified_direct(URL))
    def test_changed_proxy_expiry_and_explicit_proxy_requirement_are_respected(self):
        self.learn()
        with patch('backend.services.http_route_memory.get_environ_proxies',return_value={'https':'http://new-proxy.test:8080'}):
            self.assertFalse(retry_direct_for(URL))
        with patch('backend.scraper.http_client.proxy_required',return_value=True):
            self.assertFalse(retry_direct_for(URL))
        with patch('backend.services.http_route_memory.time.time',return_value=10**12):
            self.assertFalse(retry_direct_for(URL))

    def test_corrupt_route_hint_does_not_break_official_page_reading(self):
        from backend.database.models import AppConfig
        import json
        AppConfig.set(http_route_memory.KEY,json.dumps({http_route_memory.fingerprint(URL):{'expires_at':'invalid'}}))
        self.assertFalse(http_route_memory.prefer_verified_direct(URL))
