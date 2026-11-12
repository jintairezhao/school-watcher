"""A sampling exclusion is unknown material, not a stopped healthy frontier."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import time
import unittest
from unittest.mock import patch

from backend.scraper.acquisition import FetchRequest, FetchResult
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.services import tasks
from backend.services.source_inventory import Inventory
from scripts.checks import run_native_branch_pilot as pilot
from tests import test_direct_onboarding as fixtures

ROOT = 'https://example.edu.cn/'
EXCLUDED = 'http://coie.example.edu.cn/'
HEALTHY = 'https://notices.example.edu.cn/'
IP = 'https://93.184.216.34/'


class NativePageScopeExclusionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DirectOnboardingTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.root = Path(self.f.temp.name)
        self.store = Inventory(self.root/'scope-inventory.db')
        self.key = self.store.ensure_site('测试大学',ROOT)
        self.store.finish(self.key,ROOT,state='fetched',html='<title>测试大学</title>')
        self.store.enqueue(self.key,EXCLUDED,'甲学院','unit',1,[],'school_domain')
        self.store.enqueue(self.key,HEALTHY,'乙学院','unit',1,[],'school_domain')
        with self.store.connect() as c:
            c.execute('UPDATE pages SET network_failures=2,notes_json=? WHERE site_key=? AND url=?',
                      (json.dumps(['existing_note']),self.key,EXCLUDED))
        self.scope = dict(expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            park_out_of_scope_tasks=True,
            website=dict(allowed_hosts=['example.edu.cn'],allowed_school_domain='example.edu.cn',
                maximum_documents=6,maximum_http_attempts=8,maximum_response_bytes=3*1048576),
            model=dict(maximum_dispatch_attempts=0))
        self.bounds = pilot.Bounds(self.scope,self.root,self.root/'materials',time.monotonic()+60)
        pilot.save(self.root/'control/model-attempts.json',dict(used=0,attempts=[]))
        self.sent = []
        self.blocked_behavior = None

    def handle(self,kind='discover'):
        payload = {'school_id':self.f.school_id}
        if kind=='onboard':
            payload['url'] = EXCLUDED
        row = tasks.enqueue(kind,self.f.school_id,payload,capability='directory')
        handle = tasks.claim(capabilities=['directory'])
        self.assertEqual(handle['id'],row.id)
        handle['checkpoint']['discovery_active_page'] = {'site_key':self.key,'url':EXCLUDED}
        return handle

    def response(self,url):
        if self.blocked_behavior:
            self.blocked_behavior()
        ident = self.bounds.new_document(FetchRequest(url,purpose='directory'))
        marker = self.bounds.document.set(ident)
        try:
            if url==EXCLUDED:
                hop = self.bounds.http_attempt('http://93.184.216.34/','GET',{'Host':'coie.example.edu.cn'})
                self.sent.append('coie.example.edu.cn')
                self.bounds.finish_attempt(hop,status=301,finished_at=pilot.now(),location=IP)
                self.bounds.http_attempt(IP,'GET',{'Host':'93.184.216.34'})
                self.sent.append('93.184.216.34')
            else:
                hop = self.bounds.http_attempt(IP,'GET',{'Host':'notices.example.edu.cn'})
                self.sent.append('notices.example.edu.cn')
                self.bounds.finish_attempt(hop,status=200,finished_at=pilot.now())
            html = '<title>学院</title><main><h1>学院</h1><p>学院公开介绍</p></main>'
            result = FetchResult(url,status=200,html=html,outcome='usable')
            self.bounds.finish_document(ident,result)
            return dict(url=url,status=200,html=html,outcome='usable',result=result)
        finally:
            self.bounds.document.reset(marker)

    def run_slice(self,handle):
        with tasks.execution_scope(handle), patch('socket.socket.connect',side_effect=AssertionError('No network')), \
                patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model')), self.bounds.install():
            return crawl_site(self.store,self.key,max_pages=2,fetcher=self.response,focus='all')

    def test_excluded_redirect_yields_only_its_page_and_the_next_healthy_page_finishes(self):
        handle = self.handle()
        report = self.run_slice(handle)
        self.assertEqual(report['processed_this_run'],2)
        excluded = self.store.get_page(self.key,EXCLUDED)
        self.assertEqual((excluded['state'],excluded['health'],excluded['network_failures']),
                         ('scope_excluded','unknown',2))
        self.assertIsNone(excluded['content_hash'])
        self.assertIn('existing_note',json.loads(excluded['notes_json']))
        self.assertEqual(self.store.get_page(self.key,HEALTHY)['state'],'fetched')
        self.assertEqual(self.sent,['coie.example.edu.cn','notices.example.edu.cn'])
        self.assertNotIn('discovery_active_page',handle['checkpoint'])
        self.assertIsNone(self.bounds.boundary)
        ledger = pilot.read(self.bounds.web_file)
        self.assertEqual(len(ledger['documents']),2)
        self.assertEqual(len(ledger['http_attempts']),2)
        self.assertEqual(len(ledger['scope_exclusions']),1)
        self.assertEqual(ledger['scope_exclusions'][0]['semantic_value'],'unknown')
        self.assertTrue(ledger['scope_exclusions'][0]['page_only'])
        self.assertIsNone(ledger['boundary'])
        self.assertIsNone(ledger['documents'][0]['result'])
        self.assertEqual(ledger['documents'][1]['result']['status'],200)
        with self.store.connect() as c:
            rows = c.execute('SELECT payload_json FROM exploration_events WHERE site_key=? AND url=? AND event_type=?',
                (self.key,EXCLUDED,'page_inspection_finished')).fetchall()
        self.assertEqual(json.loads(rows[-1][0])['outcome'],'not_finished')

    def test_document_limit_still_stops_the_whole_slice_with_the_active_page_preserved(self):
        handle = self.handle()
        for reason in ('document_budget','http_budget','wall_budget','scope_expired'):
            with self.subTest(reason=reason):
                self.blocked_behavior = lambda:self.bounds.stop(reason)
                with self.assertRaises(tasks.TaskDeferred) as error:
                    self.run_slice(handle)
                self.assertEqual(error.exception.error_code,'sampling_'+reason)
                self.assertEqual(self.bounds.boundary,reason)
                self.assertEqual(self.store.get_page(self.key,HEALTHY)['attempts'],0)
                self.assertEqual(handle['checkpoint']['discovery_active_page']['url'],EXCLUDED)

    def test_browser_handoff_is_not_converted_to_a_scope_exclusion(self):
        handle = self.handle()
        self.blocked_behavior = lambda:tasks.defer(capability='browser',phase='render')
        with self.assertRaises(tasks.TaskDeferred) as error:
            self.run_slice(handle)
        self.assertEqual(error.exception.capability,'browser')
        self.assertEqual(self.store.get_page(self.key,EXCLUDED)['state'],'running')
        self.assertEqual(self.store.get_page(self.key,HEALTHY)['attempts'],0)

    def test_an_onboarding_task_keeps_its_scope_wait(self):
        handle = self.handle(kind='onboard')
        with self.assertRaises(tasks.TaskDeferred) as error:
            self.run_slice(handle)
        self.assertEqual(error.exception.error_code,'sampling_outside_declared_host')
        self.assertEqual(self.store.get_page(self.key,HEALTHY)['attempts'],0)

    def test_an_unmatched_scope_handoff_cannot_be_swallowed(self):
        handle = self.handle()
        self.blocked_behavior = lambda:tasks.defer(phase='sampling_scope',state='waiting',
            error_code='sampling_outside_declared_host')
        with self.assertRaises(tasks.TaskDeferred):
            self.run_slice(handle)
        self.assertEqual(self.store.get_page(self.key,EXCLUDED)['state'],'running')

    def test_a_new_explicit_scope_can_retry_the_retained_page_without_erasing_history(self):
        handle = self.handle()
        self.run_slice(handle)
        before = deepcopy(pilot.read(self.bounds.web_file)['scope_exclusions'])
        # A future approved retry requeues the original entry. The HTTP guard
        # still decides whether every actual hop is covered; no URL is rewritten.
        self.scope['website']['allowed_hosts'].append('93.184.216.34')
        self.store.finish(self.key,EXCLUDED,state='pending')
        self.bounds = pilot.Bounds(self.scope,self.root,self.root/'materials',time.monotonic()+60)
        self.run_slice(handle)
        self.assertEqual(self.store.get_page(self.key,EXCLUDED)['state'],'fetched')
        ledger = pilot.read(self.bounds.web_file)
        self.assertEqual(ledger['scope_exclusions'],before)
        self.assertEqual(len(ledger['documents']),3)
