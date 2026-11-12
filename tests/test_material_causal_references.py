"""Causal recorder behavior only; local substitutes prove no website effectiveness."""
from copy import copy
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from backend.ai.material_capture import capture_materials,capture_runtime,capture_task
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.acquisition import FetchRequest,FetchResult,fetch

ROOT='https://example.edu.cn/'
HTML='<title>测试大学</title><main><p>这是学校官网公开的说明材料，用于本地记录行为检查。</p></main>'


@capture_runtime
def local_model(**kwargs):
    return dict(status='succeeded',output={},usage={'known':False},provenance={})


@capture_task
def local_navigation_task(app, handle):
    return local_model(execution_id='local-navigation', messages=[])


class MaterialCausalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.inventory=Inventory(self.root/'inventory.sqlite3')
        self.key=self.inventory.ensure_site('测试大学',ROOT)

    def records(self,session):
        return [json.loads(p.read_text('utf8')) for p in sorted(session.path.glob('event-*.json'))]

    def navigation_handle(self):
        from backend.ai.material_capture import capture_inspection
        from backend.services.exploration_events import inspection, current_inspection_origin
        site = self.inventory.report(self.key)['site']
        page = self.inventory.claim(self.key, focus='valuable')
        with inspection(self.inventory, site, page) as observed, capture_inspection(page, observed):
            self.inventory.finish(self.key, page['url'], html=HTML, state='fetched')
            material = self.inventory.get_page(self.key, page['url'])['content_hash']
            origin = current_inspection_origin(self.key, page['url'], material)
        return dict(id=91, generation=1, kind='navigation_review', payload=dict(
            parent_task_id=23, parent_generation=1, site=site,
            page=dict(page, snapshot_hash=material), inspection_origin=origin))

    def test_navigation_review_keeps_the_exact_origin_after_a_later_same_page_inspection(self):
        with capture_materials(self.root/'capture', label='navigation-origin') as session:
            handle = self.navigation_handle()
            self.inspect(self.inventory.get_page(self.key, ROOT))
            local_navigation_task(None, handle)
        records = self.records(session)
        original = next(r for r in records if r['event']=='action_started' and r['payload']['kind']=='page_inspection')
        task = next(r for r in records if r['event']=='action_started' and r['payload']['kind']=='task')
        model = next(r for r in records if r['event']=='execution_invoked')
        self.assertEqual(task['payload']['causes'], [original['action_reference']])
        self.assertEqual(task['payload']['input']['origin_resolution'], 'recorded_navigation_inspection_origin')
        self.assertEqual(task['payload']['input']['inspection_event_id'], handle['payload']['inspection_origin']['event_id'])
        self.assertEqual(model['action_reference']['decision_references'], original['action_reference']['decision_references'])
        self.assertEqual(len(model['action_reference']['decision_references']), 1)

    def test_navigation_origin_mismatch_does_not_attribute_or_cancel_execution(self):
        from copy import deepcopy
        with capture_materials(self.root/'capture', label='navigation-origin-mismatch') as session:
            original = self.navigation_handle()
            for section, field, wrong in (
                ('inspection_origin', 'content_hash', 'a'*64),
                ('inspection_origin', 'site_key', 'other-site'),
                ('inspection_origin', 'event_id', 'event:'+'a'*32),
                ('inspection_origin', 'url', ROOT+'other/'),
                ('page', 'snapshot_hash', 'b'*64),
            ):
                handle = deepcopy(original)
                handle['payload'][section][field] = wrong
                self.assertEqual(local_navigation_task(None, handle)['status'], 'succeeded')
        tasks = [r for r in self.records(session) if r['event']=='action_started' and r['payload']['kind']=='task']
        self.assertEqual(len(tasks), 5)
        for task in tasks:
            self.assertEqual(task['payload']['causes'], [])
            self.assertEqual(task['action_reference']['decision_references'], [])

    def test_navigation_origin_from_another_capture_remains_unattributed(self):
        with capture_materials(self.root/'first', label='navigation-old-origin'):
            handle = self.navigation_handle()
        with capture_materials(self.root/'second', label='navigation-current-origin') as session:
            local_navigation_task(None, handle)
        task = next(r for r in self.records(session) if r['event']=='action_started' and r['payload']['kind']=='task')
        self.assertEqual(task['payload']['causes'], [])
        self.assertEqual(task['action_reference']['decision_references'], [])
        self.assertEqual(task['payload']['input']['origin_resolution'], 'navigation_origin_not_recorded_in_current_capture')

    def inspect(self,page,model=False,http=None):
        def reader(url):
            if model:local_model(execution_id='local-model',messages=[{'role':'user','content':'公开测试材料'}])
            result=fetch(FetchRequest(url,browser_allowed=False),http_transport=http or
                (lambda request:FetchResult(request.url,html=HTML,status=200,transport='http')))
            return dict(url=result.final_url,status=result.status,html=result.html)
        inspect_page(self.inventory,self.inventory.report(self.key)['site'],page,fetcher=reader,ai_assist=False)

    def test_claim_inspection_acquisition_and_model_use_the_exact_decision_reference(self):
        with capture_materials(self.root/'capture',label='causal-local') as session:
            page=self.inventory.claim(self.key,focus='valuable')
            self.inspect(page,model=True)
            local_model(execution_id='unrelated-model',messages=[])
        records=self.records(session)
        choice=next(r for r in records if r['event']=='frontier_result')
        decision=dict(capture_id=session.path.name,decision_id=choice['payload']['decision_id'])
        acquisition=next(r for r in records if r['event']=='acquisition_result')
        model=next(r for r in records if r['event']=='execution_invoked')
        self.assertIn(decision,acquisition['action_reference']['decision_references'])
        self.assertIn(decision,model['action_reference']['decision_references'])
        self.assertIsNone([r for r in records if r['event']=='execution_invoked'][-1]['action_reference'])
        self.assertEqual(len([r for r in records if r['event']=='page_claimed']),1)
        self.assertTrue(session.complete)

    def test_same_url_copy_is_not_attached_to_a_claim_by_guessing(self):
        with capture_materials(self.root/'capture',label='identity-not-url') as session:
            page=self.inventory.claim(self.key,focus='valuable')
            self.inspect(copy(page))
            self.inspect(page)
        starts=[r for r in self.records(session) if r['event']=='action_started' and r['payload']['kind']=='page_inspection']
        self.assertEqual(starts[0]['action_reference']['decision_references'],[])
        self.assertEqual(len(starts[1]['action_reference']['decision_references']),1)

    def test_resume_action_does_not_invent_a_new_frontier_decision(self):
        with capture_materials(self.root/'capture',label='actual-resume') as session:
            page=self.inventory.claim(self.key,focus='valuable',resume_url=ROOT)
            self.inspect(page)
        records=self.records(session)
        self.assertFalse(any(r['event']=='frontier_inputs'for r in records))
        claimed=next(r for r in records if r['event']=='page_claimed')
        self.assertEqual(claimed['payload']['selection_mode'],'resume')
        result=next(r for r in records if r['event']=='acquisition_result')
        self.assertEqual(result['action_reference']['decision_references'],[])

    def test_new_capture_does_not_reuse_an_old_session_claim(self):
        with capture_materials(self.root/'first',label='first') as first:
            page=self.inventory.claim(self.key,focus='valuable')
        with capture_materials(self.root/'second',label='second') as second:
            self.inspect(page)
        result=next(r for r in self.records(second) if r['event']=='acquisition_result')
        self.assertEqual(result['action_reference']['decision_references'],[])
        self.assertNotEqual(result['action_reference']['capture_id'],first.path.name)

    def test_enabled_capture_keeps_actual_claim_output_and_unstarted_work_explicit(self):
        other=Inventory(self.root/'other.sqlite3');key=other.ensure_site('测试大学',ROOT)
        expected=other.claim(key,focus='valuable')
        with capture_materials(self.root/'capture',label='same-queue-output') as session:
            actual=self.inventory.claim(self.key,focus='valuable')
        self.assertEqual(actual,expected)
        completion=json.loads((session.path/'completion.json').read_text('utf8'))['payload']
        self.assertEqual(completion['unstarted_recorded_claims'],1)
        self.assertFalse(completion['cross_thread_context_automatically_inherited'])

    def test_task_recording_failure_does_not_cancel_original_execution(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.services import tasks
        from backend.worker import execute
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            tasks.enqueue('onboard','recording-failure',dict(school_id=fixture.school_id,url=ROOT))
            handle=tasks.claim(capabilities=['http'])
            with capture_materials(self.root/'capture',label='metadata-failure') as session,\
                    patch('backend.ai.material_capture._task_origin',side_effect=RuntimeError('never-a-real-key')),\
                    patch('backend.worker.dispatch',return_value={'state':'pending'}) as dispatch:
                execute(fixture.app,handle)
            self.assertEqual(dispatch.call_count,1)
            self.assertFalse(session.complete)
            self.assertNotIn('never-a-real-key','\n'.join(p.read_text('utf8')for p in session.path.glob('*.json')))
        finally:fixture.tearDown()

    def test_sampler_document_uses_the_active_acquisition_reference(self):
        from datetime import datetime,timedelta,timezone
        import time
        from scripts.checks.run_native_branch_pilot import Bounds,read
        scope=dict(expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
            website=dict(allowed_hosts=['example.edu.cn'],maximum_documents=2,maximum_http_attempts=2,maximum_response_bytes=1048576),
            model=dict(maximum_dispatch_attempts=1))
        bounds=Bounds(scope,self.root,self.root/'web',time.monotonic()+60)
        def http(request):
            bounds.new_document(request)
            return FetchResult(request.url,html=HTML,status=200,transport='http')
        with capture_materials(self.root/'capture',label='sampler-acquisition') as session:
            self.inspect(self.inventory.claim(self.key,focus='valuable'),http=http)
        doc=read(bounds.web_file)['documents'][0]
        actual=next(r for r in self.records(session) if r['event']=='acquisition_result')
        self.assertEqual(doc['action_reference']['action_id'],actual['action_reference']['action_id'])
        self.assertTrue(doc['action_reference']['decision_references'])

    def test_worker_validation_preserves_origin_after_the_action_closes(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.services import tasks
        from backend.worker import execute
        from backend.services.source_validation import record_validation
        from backend.database.db import db
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            tasks.enqueue('onboard','causal',dict(school_id=fixture.school_id,url=ROOT))
            handle=tasks.claim(capabilities=['http'])
            def dispatch(kind,payload):
                record_validation(fixture.school_id,dict(name='本地栏目',list_url=ROOT),dict(status='pending'))
                db.session.commit()
                return dict(state='pending')
            with capture_materials(self.root/'capture',label='worker-native') as session,patch('backend.worker.dispatch',side_effect=dispatch):
                execute(fixture.app,handle)
            records=self.records(session)
            action=next(r for r in records if r['event']=='action_started'and r['payload']['kind']=='task')
            native=next(r for r in records if r['event']=='native_validation')
            self.assertEqual(native['payload']['origin_action_reference']['action_id'],action['action_reference']['action_id'])
            self.assertEqual(native['payload']['persistence'],'committed')
            self.assertIsNone(native['action_reference'])
            saved='\n'.join(p.read_text('utf8') for p in session.path.glob('*.json'))
            self.assertNotIn(handle['token'],saved)
        finally:fixture.tearDown()

    def test_later_onboarding_uses_its_explicit_inspection_receipt(self):
        from tests.test_source_execution_events import SourceExecutionEventTests
        from tests.test_source_attention_flow import TEACHER
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page
        from backend.worker import execute
        fixture=SourceExecutionEventTests();fixture.setUp()
        try:
            inv,key=fixture.flow.inv,fixture.flow.key
            with capture_materials(self.root/'capture',label='inspection-to-onboard') as session:
                inspect_page(inv,inv.report(key)['site'],inv.get_page(key,TEACHER),
                    fetcher=lambda url:dict(url=url,status=200,html=fixture.flow.pages[url]),ai_assist=False)
                fixture.flow.queue();handle=tasks.claim(capabilities=['http'])
                with patch('backend.worker.dispatch',side_effect=lambda kind,payload:onboard_page(payload,fetcher=fixture.fetch)),\
                        patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model')):
                    execute(fixture.app,handle)
            records=self.records(session)
            original=next(r for r in records if r['event']=='action_started'and r['payload']['kind']=='page_inspection')
            action=next(r for r in records if r['event']=='action_started'and r['payload']['kind']=='task')
            self.assertIn(original['action_reference'],action['payload']['causes'])
            self.assertEqual(action['payload']['input']['origin_resolution'],'recorded_inspection_receipt')
            saved=next(r for r in records if r['event']=='source_configuration')
            self.assertEqual(saved['payload']['origin_action_reference']['action_id'],action['action_reference']['action_id'])
        finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
