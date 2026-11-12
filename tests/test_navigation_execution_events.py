"""Actual navigation-result application on local material, no paid calls."""
from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import BackgroundTask,School
from backend.services import tasks
from backend.services.runtime_catalog import RuntimeCatalog
from backend.scraper.discovery.ai_navigation import process_navigation
from backend.scraper.discovery.layered import apply_route
from tests import test_ai_primary_navigation as navigation_fixture

ROOT=navigation_fixture.ROOT
HTML='<title>测试大学</title><nav><a href="/space/">成长空间</a></nav>'
READ_AT='2026-10-01T06:00:00+00:00'
REVIEW_AT='2026-10-26T06:00:00+00:00'


class NavigationExecutionEventTests(unittest.TestCase):
    def setUp(self):
        self.f=navigation_fixture.NavigationPersistenceTests();self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.app=self.f.fixture.app
        self.school=db.session.get(School,self.f.fixture.school_id)
        from backend.services.discovery_cache import DiscoveryCache
        from backend.scraper.discovery.inventory_crawler import inspect_page
        self.store=DiscoveryCache(self.f.fixture.app.config['DISCOVERY_CACHE_PATH'])
        self.key=self.store.ensure_site('测试大学',ROOT)
        self.parent=tasks.enqueue('discover',self.f.fixture.school_id,{'school_id':self.f.fixture.school_id})
        parent_handle=tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(parent_handle), \
                patch('backend.ai.configuration.get_model_binding',return_value={'id':1,'version':1}), \
                patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')):
            inspect_page(self.store,self.store.report(self.key)['site'],self.store.get_page(self.key,ROOT),
                fetcher=lambda _:dict(html=HTML,url=ROOT,status=200,checked_at=READ_AT),focus='valuable')
        tasks.finish(parent_handle,{'status':'finished'})
        self.job=BackgroundTask.query.filter_by(kind='navigation_review').one()
        self.handle=tasks.claim(capabilities=['directory'])
        self.assertEqual(self.handle['id'],self.job.id)

    def events(self):
        return self.store.report(self.key)['exploration_events']

    def answer(self,site,page,html,parsed,**kwargs):
        for link in parsed['links']:
            apply_route(parsed,link,dict(link,decision='follow',kind='gateway',ai_reviewed=True,ai_role='gateway'))
        return dict(status='processed',reviewed_routes=1,cached_routes=0,decision_id='routes:'+'a'*64)

    def run_application(self,answer=None):
        with tasks.execution_scope(self.handle), \
                patch('backend.scraper.discovery.layered.assist',side_effect=answer or self.answer), \
                patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')):
            return process_navigation(self.handle['payload'])

    def connect(self):
        """A source first found by navigation application keeps that ancestry."""
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.source_onboarding import queue_columns,onboard_page
        from backend.database.source_governance_models import SourceConfigVersion
        from tests import test_direct_onboarding as source_fixture
        self.run_application()
        url=ROOT+'space/'
        inspect_page(self.store,self.store.report(self.key)['site'],self.store.get_page(self.key,url),
            fetcher=lambda _:dict(html=source_fixture.HTML,url=url,status=200),ai_assist=False)
        RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH']).publish(self.store,self.key)
        queue_columns(self.school.id,self.store,self.key)
        handle=tasks.claim(capabilities=['http'])
        def fetch(address,purpose):
            return source_fixture.HTML if address==url else self.f.fixture.fetch(address,purpose)
        with tasks.execution_scope(handle),patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')):
            result=onboard_page(handle['payload'],fetcher=fetch)
        tasks.finish(handle,result)
        self.assertEqual(result['state'],'connected')
        return SourceConfigVersion.query.one()

    def test_new_page_is_linked_to_actual_application_and_exact_inspection(self):
        old=self.events()
        original=next(e for e in reversed(old) if e['event_type']=='page_inspection_finished')
        self.run_application()
        events=self.events()
        start=next(e for e in events if e['event_type']=='navigation_application_started')
        found=next(e for e in events if e['event_type']=='page_discovered')
        end=next(e for e in events if e['event_type']=='navigation_application_finished')
        self.assertEqual(start['parent_event_id'],original['id'])
        self.assertEqual(start['content_hash'],hashlib.sha256(HTML.encode()).hexdigest())
        self.assertEqual(found['parent_event_id'],start['id'])
        self.assertEqual(found['evidence'][0]['content_hash'],start['content_hash'])
        self.assertTrue(found['evidence'][0]['locator'])
        self.assertEqual(end['parent_event_id'],start['id'])
        self.assertEqual(end['new_page_count'],1)
        self.assertEqual(start['review_summary']['decision_reference'],'routes:'+'a'*64)
        self.assertIsNone(end['cost'])
        self.assertEqual(self.store.get_page(self.key,ROOT+'space/')['state'],'pending')
        published=RuntimeCatalog(self.f.fixture.app.config['SOURCE_CATALOG_PATH']).monitor_snapshot(self.key)
        self.assertIn(end['id'],{e['id'] for e in published['exploration_events']})

    def relation_times(self):
        with self.store.connect() as connection:
            edges=[r['observed_at'] for r in connection.execute(
                'SELECT observed_at FROM edges WHERE site_key=? AND parent_url=?',(self.key,ROOT))]
        self.assertTrue(edges)
        return edges+[r['observed_at'] for r in self.store.structure(self.key) if r['reference_url']==ROOT]

    def test_read_relations_use_the_response_observation_time(self):
        self.assertEqual(self.store.get_page(self.key,ROOT)['checked_at'],READ_AT)
        self.assertEqual(set(self.relation_times()),{READ_AT})

    def test_delayed_application_and_publication_do_not_reobserve_old_material(self):
        with patch('backend.services.source_inventory.now',return_value=REVIEW_AT):
            self.run_application()
        self.assertEqual(set(self.relation_times()),{READ_AT})
        catalog=RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'])
        page=next(p for p in catalog.report(self.key)['pages'] if p['url']==ROOT)
        self.assertTrue(page['navigation_edges'])
        self.assertEqual({r['observed_at'] for r in page['navigation_edges']},{READ_AT})
        application=next(e for e in self.events() if e['event_type']=='navigation_application_started')
        self.assertNotEqual(application['observed_at'],READ_AT,'Applying a result is a separate actual event')

    def test_identical_real_reread_keeps_its_newer_observation_during_old_review(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        later='2026-10-20T06:00:00+00:00'
        inspect_page(self.store,self.store.report(self.key)['site'],self.store.get_page(self.key,ROOT),
            fetcher=lambda _:dict(html=HTML,url=ROOT,status=200,checked_at=later),focus='valuable',ai_assist=False)
        with patch('backend.services.source_inventory.now',return_value=REVIEW_AT):
            self.run_application()
        self.assertEqual(set(self.relation_times()),{later})

    def test_legacy_material_without_observation_time_stays_unknown(self):
        page=dict(self.handle['payload']['page']);page.pop('observed_at',None)
        if page.get('snapshot_ref'):
            page['snapshot_ref']=dict(page['snapshot_ref']);page['snapshot_ref'].pop('observed_at',None)
        self.handle['payload']=dict(self.handle['payload'],page=page)
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET checked_at=NULL WHERE site_key=? AND url=?',(self.key,ROOT))
        with patch('backend.services.source_inventory.now',return_value=REVIEW_AT):
            self.run_application()
        self.assertEqual(set(self.relation_times()),{''})

    def test_failed_revisit_does_not_date_old_material_as_a_new_success(self):
        self.store.finish(self.key,ROOT,state='failed',fetched_at=REVIEW_AT,error='network failure')
        self.run_application()
        self.assertEqual(set(self.relation_times()),{READ_AT})

    def test_existing_page_reinterpretation_does_not_repeat_first_discovery(self):
        self.store.enqueue(self.key,ROOT+'space/','原有页面','gateway',1,[],'school_domain')
        self.run_application()
        self.assertFalse(any(e['event_type']=='page_discovered' for e in self.events()))
        ended=next(e for e in self.events() if e['event_type']=='navigation_application_finished')
        self.assertEqual(ended['new_page_count'],0)

    def test_old_material_without_recorded_inspection_keeps_upstream_unknown(self):
        with self.store.connect() as connection:
            connection.execute('DELETE FROM exploration_events WHERE site_key=?',(self.key,))
        self.run_application()
        started=self.events()[0]
        self.assertEqual(started['event_type'],'navigation_application_started')
        self.assertIsNone(started['parent_event_id'])
        self.assertEqual(started['upstream_status'],'material_inspection_not_recorded')

    def test_changed_material_before_application_creates_no_application_or_discovery(self):
        before=self.events()
        def changed(*args,**kwargs):
            result=self.answer(*args,**kwargs)
            self.store.finish(self.key,ROOT,state='fetched',html='<title>Different material</title>')
            return result
        result=self.run_application(changed)
        self.assertEqual(result['error_code'],'material_changed')
        self.assertEqual(self.events(),before)
        self.assertIsNone(self.store.get_page(self.key,ROOT+'space/'))

    def test_partial_application_failure_records_committed_discovery_and_interruption(self):
        with patch('backend.services.discovery_changes.remember_navigation',side_effect=RuntimeError('private failure detail')):
            with self.assertRaisesRegex(RuntimeError,'private failure detail'):
                self.run_application()
        events=self.events()
        self.assertTrue(any(e['event_type']=='page_discovered' for e in events))
        self.assertFalse(any(e['event_type']=='navigation_application_finished' for e in events))
        ended=events[-1]
        self.assertEqual(ended['event_type'],'navigation_application_interrupted')
        self.assertEqual(ended['error_type'],'RuntimeError')
        self.assertEqual(ended['new_page_count'],1)
        self.assertNotIn('private failure detail',str(events))

    def test_same_material_reread_cannot_replace_the_actual_queueing_inspection(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        original=next(e for e in reversed(self.events()) if e['event_type']=='page_inspection_finished')
        inspect_page(self.store,self.store.report(self.key)['site'],self.store.get_page(self.key,ROOT),
            fetcher=lambda _:dict(html=HTML,url=ROOT,status=200),focus='valuable',ai_assist=False)
        later=next(e for e in reversed(self.events()) if e['event_type']=='page_inspection_finished')
        self.assertNotEqual(original['id'],later['id'])
        self.run_application()
        start=next(e for e in self.events() if e['event_type']=='navigation_application_started')
        self.assertEqual(start['parent_event_id'],original['id'])

    def test_legacy_task_does_not_infer_origin_from_matching_existing_material(self):
        self.handle['payload']=dict(self.handle['payload'],inspection_origin=None)
        self.run_application()
        start=next(e for e in self.events() if e['event_type']=='navigation_application_started')
        self.assertIsNone(start['parent_event_id'])
        self.assertEqual(start['upstream_status'],'material_inspection_not_recorded')

    def test_lost_lease_before_application_writes_no_event_or_new_page(self):
        before=self.events()
        def lost(*args,**kwargs):
            result=self.answer(*args,**kwargs)
            with tasks.control_transaction():
                row=db.session.get(BackgroundTask,self.handle['id'])
                row.token='a later owner';db.session.commit()
            return result
        with self.assertRaises(tasks.LeaseLost):
            self.run_application(lost)
        self.assertEqual(self.events(),before)
        self.assertIsNone(self.store.get_page(self.key,ROOT+'space/'))

    def test_cached_decision_does_not_claim_a_new_model_dispatch(self):
        def cached(*args,**kwargs):
            result=self.answer(*args,**kwargs)
            result.update(cached_routes=1);result.pop('decision_id')
            return result
        self.run_application(cached)
        start=next(e for e in self.events() if e['event_type']=='navigation_application_started')
        self.assertEqual(start['review_summary']['cached_routes'],1)
        self.assertIsNone(start['review_summary']['decision_reference'])
        self.assertEqual(start['scope'],'navigation_result_application_not_model_dispatch')

    def test_application_retry_never_repeats_first_enqueue(self):
        self.run_application();self.run_application()
        events=self.events()
        self.assertEqual(sum(e['event_type']=='page_discovered' for e in events),1)
        self.assertEqual([e['new_page_count'] for e in events if e['event_type']=='navigation_application_finished'],[1,0])

    def test_result_summary_does_not_copy_arbitrary_model_or_provider_values(self):
        def unusual(*args,**kwargs):
            result=self.answer(*args,**kwargs)
            return dict(result,status='private status',reviewed_routes='private count',decision_id='private reference',
                error='private error',payload={'token':'private token'})
        self.run_application(unusual)
        self.assertNotIn('private',str(self.events()))

    def test_source_receipt_retains_navigation_application_ancestry(self):
        from backend.services.structure_snapshot import read_structure,graph_projection
        version=self.connect()
        self.assertIsNotNone(version.execution_json['parent_event_id'])
        receipt=version.execution_json['page_receipt']
        self.assertTrue(any(e['event_type']=='navigation_application_started' for e in receipt['events']))
        graph=graph_projection(read_structure(self.school),self.school)
        event=next(e for e in graph['exploration_events'] if e['node_id']==f'source:{version.department_id}')
        self.assertTrue(event['current_configuration'])
        self.assertIsNotNone(event['parent_event_id'])
        self.assertFalse(any(edge['type']=='source_ownership' for edge in graph['relations']))

    def test_original_failure_survives_interruption_record_failure_and_context_resets(self):
        from backend.services import exploration_events
        real=exploration_events._append
        def append(connection,key,url,event_type,*args,**kwargs):
            if event_type=='navigation_application_interrupted':raise OSError('private recording failure')
            return real(connection,key,url,event_type,*args,**kwargs)
        with patch.object(exploration_events,'_append',side_effect=append), \
                patch('backend.services.discovery_changes.remember_navigation',side_effect=RuntimeError('original failure')), \
                self.assertLogs('backend.services.exploration_events','ERROR') as logs:
            with self.assertRaisesRegex(RuntimeError,'original failure'):
                self.run_application()
        self.assertNotIn('private recording failure',str(logs.output))
        count=len(self.events())
        self.store.enqueue(self.key,ROOT+'separate/','另一入口','gateway',1,[],'school_domain')
        self.assertEqual(len(self.events()),count,'The recorder context must not leak into unrelated work')

    def test_finished_record_failure_blocks_publication_of_success(self):
        from backend.services import exploration_events
        real=exploration_events._append
        def append(connection,key,url,event_type,*args,**kwargs):
            if event_type=='navigation_application_finished':raise OSError('recording unavailable')
            return real(connection,key,url,event_type,*args,**kwargs)
        with patch.object(exploration_events,'_append',side_effect=append), \
                patch('backend.services.runtime_catalog.RuntimeCatalog.publish') as publish:
            with self.assertRaisesRegex(OSError,'recording unavailable'):
                self.run_application()
            publish.assert_not_called()
        self.assertFalse(any(e['event_type']=='navigation_application_finished' for e in self.events()))

    def test_stale_parser_is_rejected_before_application(self):
        from backend.scraper.discovery.parser_revision import ParserRevisionChanged
        before=self.events()
        with patch('backend.scraper.discovery.parser_revision.assert_current_parser',side_effect=ParserRevisionChanged('restart required')):
            with self.assertRaises(ParserRevisionChanged):
                self.run_application()
        self.assertEqual(self.events(),before)
        self.assertIsNone(self.store.get_page(self.key,ROOT+'space/'))

    def test_failed_publication_keeps_previous_public_snapshot_and_structure_version(self):
        from backend.services.structure_versions import current_version
        catalog=RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'])
        catalog.publish(self.store,self.key)
        before=catalog.monitor_snapshot(self.key)
        version=current_version(self.school.id)
        with patch.object(RuntimeCatalog,'publish',side_effect=OSError('publication unavailable')):
            with self.assertRaisesRegex(OSError,'publication unavailable'):
                self.run_application()
        self.assertEqual(catalog.monitor_snapshot(self.key),before)
        self.assertEqual(current_version(self.school.id),version)
        self.assertTrue(any(e['event_type']=='navigation_application_finished' for e in self.events()),
            'Durable local application and public publication are separate observed facts')


if __name__=='__main__':unittest.main()
