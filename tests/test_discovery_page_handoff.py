"""A source page handed to the browser must finish before choosing another URL."""
from pathlib import Path
from dataclasses import replace
from datetime import datetime
import unittest
from unittest.mock import Mock, patch
from tests import test_direct_onboarding as fixture_module
from backend.database.db import db
from backend.services import tasks
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.scraper.acquisition import FetchRequest, FetchResult, execution_context
from backend.services.task_fetch import request_key

ROOT = 'https://example.edu.cn/'
UNIT = ROOT + 'faculty/'
OTHER = ROOT + 'admissions/'


class DiscoveryPageHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests(); self.fixture.setUp()
        self.store = Inventory(Path(self.fixture.app.config['DISCOVERY_CACHE_PATH']))
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.store.finish(self.key, ROOT, state='fetched', html='<title>测试大学</title>')
        self.store.enqueue(self.key, UNIT, '工学院', 'unit', 1, [], 'school_domain')

    def tearDown(self):
        self.fixture.tearDown()

    def job(self):
        row = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id}, capability='directory')
        return row, tasks.claim(capabilities=['directory'])

    def ready_response(self, url):
        self.assertEqual(url, UNIT, 'Browser resume chose a different high-scoring frontier URL')
        return {'url': url, 'status': 200, 'html': '<title>工学院</title><main><h1>工学院</h1><p>学院公开介绍</p></main>', 'outcome': 'usable'}

    def test_browser_handoff_keeps_the_claimed_page_when_a_higher_rank_route_arrives(self):
        row, handle = self.job()
        request = FetchRequest(UNIT, purpose='directory', policy={'exploration': True})
        def handoff(url):
            self.assertEqual(url, UNIT)
            checkpoint = dict(tasks.current_execution().get('checkpoint') or {})
            checkpoint['browser'] = {'request': request.to_dict(), 'original_key': request_key(request)}
            tasks.defer(capability='browser', phase='render', checkpoint=checkpoint, delay=0)
        with tasks.execution_scope(handle):
            with self.assertRaises(tasks.TaskDeferred) as caught:
                crawl_site(self.store, self.key, max_pages=1, fetcher=handoff, focus='valuable')
            tasks.handoff(handle, caught.exception)
        self.store.enqueue(self.key, OTHER, '研究生招生与奖学金', 'channel', 1, [], 'school_domain')
        resumed = tasks.claim(capabilities=['browser'])
        self.assertEqual(resumed['id'], row.id)
        with tasks.execution_scope(resumed):
            crawl_site(self.store, self.key, max_pages=1, fetcher=self.ready_response, focus='valuable')
        self.assertEqual(self.store.get_page(self.key, UNIT)['state'], 'fetched')
        self.assertEqual(self.store.get_page(self.key, OTHER)['attempts'], 0)
        db.session.refresh(row)
        self.assertNotIn('browser', row.checkpoint)
        self.assertNotIn('discovery_active_page', row.checkpoint)

    def test_legacy_browser_checkpoint_recovers_original_page_without_new_marker(self):
        row, handle = self.job()
        request = FetchRequest(UNIT, purpose='directory', policy={'exploration': True})
        row.checkpoint = {'browser': {'request': request.to_dict(), 'original_key': request_key(request)}}
        db.session.commit()
        handle['checkpoint'] = row.checkpoint
        self.store.enqueue(self.key, OTHER, '研究生招生与奖学金', 'channel', 1, [], 'school_domain')
        with tasks.execution_scope(handle):
            crawl_site(self.store, self.key, max_pages=1, fetcher=self.ready_response, focus='valuable')
        self.assertEqual(self.store.get_page(self.key, OTHER)['attempts'], 0)

    def test_legacy_redirect_uses_original_digest_instead_of_guessing_from_final_url(self):
        row, handle = self.job()
        original = FetchRequest(UNIT, purpose='directory', policy={'exploration': True})
        redirected = replace(original, url=ROOT + 'rendered-faculty/', source_id='discover:1')
        row.checkpoint = {'browser': {'request': redirected.to_dict(), 'original_key': request_key(original)}}
        db.session.commit(); handle['checkpoint'] = row.checkpoint
        self.store.enqueue(self.key, OTHER, '研究生招生与奖学金', 'channel', 1, [], 'school_domain')
        with tasks.execution_scope(handle):
            crawl_site(self.store, self.key, max_pages=1, fetcher=self.ready_response, focus='valuable')
        self.assertEqual(self.store.get_page(self.key, OTHER)['attempts'], 0)

    def test_real_queue_bridge_submits_once_then_polls_original_page_without_repeating_http(self):
        from backend.services import task_fetch
        row, handle = self.job()
        response = self.ready_response(UNIT)
        rendered = FetchResult(UNIT, status=200, html=response['html'], transport='browser')
        http = Mock(return_value=FetchResult(UNIT, outcome='network_error', error_code='http_serverdisconnectederror'))
        with patch('backend.scraper.acquisition.coordinator.http_fetch', http), \
             patch.object(task_fetch.BrowserClient, 'submit', return_value=('running', None)) as submit, \
             patch.object(task_fetch.BrowserClient, 'poll', return_value=('done', rendered)) as poll:
            for turn in range(3):
                with tasks.execution_scope(handle), execution_context(browser_dispatch=task_fetch.browser_dispatch,
                        cache_lookup=task_fetch.cache_lookup, cache_store=task_fetch.cache_store):
                    try:
                        crawl_site(self.store, self.key, max_pages=1, focus='valuable')
                    except tasks.TaskDeferred as deferred:
                        self.assertLess(turn, 2)
                        tasks.handoff(handle, deferred)
                    else:
                        self.assertEqual(turn, 2)
                        break
                if turn == 0:
                    self.store.enqueue(self.key, OTHER, '研究生招生与奖学金', 'channel', 1, [], 'school_domain')
                db.session.refresh(row); row.available_at = datetime.utcnow(); db.session.commit()
                handle = tasks.claim(capabilities=['browser'])
                self.assertIsNotNone(handle)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(poll.call_count, 1)
        self.assertEqual(self.store.get_page(self.key, UNIT)['state'], 'fetched')
        self.assertEqual(self.store.get_page(self.key, OTHER)['attempts'], 0)
        db.session.refresh(row)
        self.assertNotIn('browser', row.checkpoint)
        self.assertNotIn('discovery_active_page', row.checkpoint)
