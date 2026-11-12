"""Synthetic safety checks, never evidence of university discovery effectiveness."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import time
import unittest

from scripts.checks.run_native_branch_pilot import Bounds,SamplingBoundary,bounded_process,read,save


class NativePilotBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-pilot-bounds-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.scope=dict(expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            website=dict(allowed_hosts=['www.cupk.edu.cn'],maximum_documents=1,maximum_http_attempts=2,maximum_response_bytes=3*1048576),
            model=dict(maximum_dispatch_attempts=2))
        self.bounds=Bounds(self.scope,self.root,self.root/'observations',time.monotonic()+60)
        save(self.root/'control/model-attempts.json',dict(used=1,attempts=[dict(sequence=1,origin='synthetic_existing_configuration_test')]))

    def request(self):
        from backend.scraper.acquisition import FetchRequest
        return FetchRequest('https://www.cupk.edu.cn/')

    def test_document_budget_and_unknown_boundary_do_not_run_transport(self):
        self.bounds.new_document(self.request())
        with self.assertRaises(SamplingBoundary):self.bounds.new_document(self.request())
        ledger=read(self.bounds.web_file)
        self.assertEqual(len(ledger['documents']),1)
        self.assertEqual(ledger['boundary']['semantic_value'],'unknown')
        self.assertEqual(ledger['http_attempts'],[])

    def test_declared_school_family_accepts_native_child_and_redirect_hosts(self):
        self.scope['website']['allowed_school_domain']='cupk.edu.cn'
        from backend.scraper.acquisition import FetchRequest
        self.bounds.new_document(FetchRequest('https://bkzs.cupk.edu.cn/'))
        self.bounds.http_attempt('https://9.9.9.9/','GET',{'Host':'career.cupk.edu.cn'})
        self.assertTrue(self.bounds.allowed_host('cupk.edu.cn'))
        self.assertEqual(len(read(self.bounds.web_file)['http_attempts']),1)

    def test_school_family_cannot_match_lookalike_or_third_party_hosts(self):
        self.scope['website']['allowed_school_domain']='cupk.edu.cn'
        for host in ('evilcupk.edu.cn','cupk.edu.cn.evil.example','other.edu.cn'):
            with self.subTest(host=host),self.assertRaises(SamplingBoundary):
                self.bounds.http_attempt('https://9.9.9.9/','GET',{'Host':host})
        self.assertEqual(read(self.bounds.web_file)['http_attempts'],[])

    def test_native_redirects_share_http_quota_before_sending_an_extra_request(self):
        import aiohttp
        from backend.scraper.acquisition.http_transport import fetch_http
        sent=[]
        class Response:
            status=302
            def __init__(self,n):self.headers={'Location':'/hop'+str(n)}
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        async def request(session,method,url,**kwargs):
            sent.append(str(url));return Response(len(sent))
        with patch.object(aiohttp.ClientSession,'_request',request),\
                patch('backend.scraper.acquisition.http_transport.public_addresses',return_value=['9.9.9.9']),\
                patch('backend.scraper.acquisition.http_transport._shared_permit',return_value=None),\
                self.bounds.install():
            with self.assertRaises(SamplingBoundary):fetch_http(self.request())
        self.assertEqual(len(sent),2)
        ledger=read(self.bounds.web_file)
        self.assertEqual([r['status'] for r in ledger['http_attempts']],[302,302])
        self.assertEqual(ledger['boundary']['reason'],'http_budget')

    def test_model_quota_includes_prior_test_and_survives_controller_recreation(self):
        from backend.ai.providers import ProviderError
        profile=dict(provider='deepseek',model='deepseek-flash',region='default',id=1)
        self.bounds.reserve_model(profile)
        replay=Bounds(self.scope,self.root,self.root/'observations',time.monotonic()+60)
        with self.assertRaises(ProviderError):replay.reserve_model(profile)
        self.assertEqual(read(self.root/'control/model-attempts.json')['used'],2)

    def test_expiry_and_undeclared_host_stop_without_dispatch(self):
        self.bounds.deadline=time.monotonic()-1
        with self.assertRaises(SamplingBoundary):self.bounds.new_document(self.request())
        self.assertEqual(read(self.bounds.web_file)['documents'],[])
        self.bounds.deadline=time.monotonic()+60
        with self.assertRaises(SamplingBoundary):self.bounds.http_attempt('https://9.9.9.9/','GET',{'Host':'other.edu.cn'})
        self.assertEqual(read(self.bounds.web_file)['http_attempts'],[])

    def test_native_worker_hands_off_boundary_with_its_checkpoint(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        from backend.worker import execute
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            job=tasks.enqueue('discover',fixture.school_id,dict(school_id=fixture.school_id,ai_assist=True))
            job.checkpoint={'existing_material_reference':'kept'};db.session.commit()
            handle=tasks.claim(capabilities=('directory',),worker_id='synthetic-safety-check')
            self.assertIsNotNone(handle)
            self.bounds.new_document(self.request())
            with patch('backend.worker.dispatch',side_effect=lambda *args:self.bounds.new_document(self.request())):
                execute(fixture.app,handle)
            row=db.session.get(BackgroundTask,job.id,populate_existing=True)
            self.assertEqual(row.state,'pending')
            self.assertEqual(row.phase,'sampling_boundary')
            self.assertEqual(row.checkpoint['existing_material_reference'],'kept')
            self.assertEqual(read(self.bounds.web_file)['http_attempts'],[])
        finally:fixture.tearDown()

    def test_scope_exclusion_parks_only_its_task_and_keeps_other_native_work_eligible(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        from backend.worker import execute
        from backend.scraper.acquisition import FetchRequest
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            job=tasks.enqueue('discover',fixture.school_id,dict(school_id=fixture.school_id))
            job.checkpoint={'existing_cursor':8};db.session.commit()
            handle=tasks.claim(capabilities=['directory'])
            other=tasks.enqueue('navigation_review','synthetic-in-scope',dict(school_id=fixture.school_id))
            self.scope['park_out_of_scope_tasks']=True
            with patch('backend.worker.dispatch',side_effect=lambda *args:self.bounds.new_document(FetchRequest('https://outside.edu.cn/'))):
                execute(fixture.app,handle)
            row=db.session.get(BackgroundTask,job.id,populate_existing=True)
            self.assertEqual((row.state,row.phase),('waiting','sampling_scope'))
            self.assertEqual(row.checkpoint['existing_cursor'],8)
            self.assertIsNone(self.bounds.boundary)
            ledger=read(self.bounds.web_file)
            self.assertEqual(ledger['http_attempts'],[])
            self.assertEqual(ledger['scope_exclusions'][0]['url'],'https://outside.edu.cn/')
            self.assertEqual(ledger['scope_exclusions'][0]['semantic_value'],'unknown')
            self.assertEqual(tasks.claim(capabilities=['directory'])['id'],other.id)
        finally:fixture.tearDown()

    def test_owned_sampling_process_has_a_hard_wall_limit(self):
        result=bounded_process(time.sleep,(20,),.25)
        self.assertTrue(result['hard_stopped'])
        self.assertNotEqual(result['exit_code'],0)
        self.assertLess(result['elapsed_seconds'],5)


if __name__=='__main__':unittest.main()
