"""Actual runtime ledger + navigation on local materials; provider IO is stubbed."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from backend.ai.models import AIExecution,AIBudget
from backend.ai.providers import ProviderResult,ProviderError
from backend.database.db import db
from backend.services import tasks
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.structure_snapshot import read_structure,graph_projection,transport_update
from backend.scraper.discovery.ai_navigation import process_navigation
from tests import test_navigation_execution_events as fixture
from tests import test_ai_primary_navigation as answers

BINDING=dict(id=0,version='isolated-reference',provider='deepseek',model='fixture-model',max_output_tokens=4096)


class ExecutionObservationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.NavigationExecutionEventTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.school=self.f.school
        self.binding=patch('backend.ai.configuration.get_model_binding',return_value=BINDING)
        self.binding.start();self.addCleanup(self.binding.stop)
        self.credentials=patch('backend.ai.runtime.credential_for',return_value='synthetic-not-a-key')
        self.credentials.start();self.addCleanup(self.credentials.stop)

    def response(self,binding,key,messages,**kwargs):
        evidence=json.loads(messages[-1]['content'].split('\n',1)[1])
        output=answers.reply(evidence,role='gateway')['output']
        return ProviderResult(json.dumps(output,ensure_ascii=False),'stop',
            dict(known=True,total_tokens=70,input_tokens=50,output_tokens=20,cache_hit_tokens=30,cache_miss_tokens=20),
            'private-provider-request-id',dict(private_provider_field='never publish'))

    def run_review(self,response=None):
        with tasks.execution_scope(self.f.handle),patch('backend.ai.providers.complete',side_effect=response or self.response) as adapter:
            result=process_navigation(self.f.handle['payload'])
        return result,adapter.call_count

    def reviews(self):return [event for event in self.f.events() if event['event_type']=='navigation_model_observed']

    def reset_review(self,*,clear_cache=False):
        with tasks.execution_scope(self.f.handle):
            tasks.checkpoint({})
        if clear_cache:
            with self.f.store.connect() as connection:connection.execute('DELETE FROM navigation_role_cache')

    def graph(self):return graph_projection(read_structure(self.school),self.school,scope='exploration')

    def test_real_runtime_use_has_exact_ledger_reference_and_causal_parent(self):
        result,calls=self.run_review();self.assertEqual(calls,1)
        invoice=AIExecution.query.one();review=self.reviews()[0]
        use,=review['model_uses']
        self.assertEqual(use['execution_id'],invoice.execution_id)
        self.assertEqual(use['input_digest'],invoice.input_digest)
        self.assertEqual(use['use_kind'],'request_attempt')
        parent=next(e for e in self.f.events() if e['id']==review['parent_event_id'])
        self.assertEqual(parent['event_type'],'page_inspection_finished')
        applied=next(e for e in self.f.events() if e['event_type']=='navigation_application_started')
        self.assertEqual(applied['parent_event_id'],review['id'])
        graph=self.graph();public=next(e for e in graph['exploration_events'] if e['id']==review['id'])
        self.assertEqual(public['model_usage']['executions'][0]['total_tokens'],70)
        self.assertIsNone(public['cost'])
        self.assertEqual(graph['exploration_history']['model_usage']['confirmed_tokens'],70)
        self.assertNotIn('private-provider',json.dumps(graph))
        self.assertFalse(any(edge['type']=='source_ownership' for edge in graph['relations']))

    def test_shared_role_cache_keeps_original_reference_without_another_call_or_charge(self):
        self.run_review();self.reset_review()
        _,calls=self.run_review();self.assertEqual(calls,0)
        second=self.reviews()[-1]
        self.assertEqual(second['model_uses'],[])
        self.assertEqual(len(second['cache_reuses']),1)
        self.assertEqual(second['cache_reuses'][0]['execution_reference']['execution_id'],AIExecution.query.one().execution_id)
        graph=self.graph();summary=graph['exploration_history']['model_usage']
        self.assertEqual(summary['referenced_executions'],1)
        self.assertEqual(summary['confirmed_tokens'],70)
        self.assertEqual(db.session.get(AIBudget,AIExecution.query.one().budget_keys[0]).used_requests,1)
        self.assertEqual(next(e for e in graph['exploration_events'] if e['id']==second['id'])['model_usage']['executions'][0]['use_kind'],'shared_cache_reuse')

    def test_runtime_dedup_is_distinct_from_shared_role_cache(self):
        self.run_review();self.reset_review(clear_cache=True)
        _,calls=self.run_review();self.assertEqual(calls,0)
        use,=self.reviews()[-1]['model_uses']
        self.assertEqual(use['use_kind'],'ledger_reuse')
        self.assertEqual(AIExecution.query.count(),1)
        self.assertEqual(self.graph()['exploration_history']['model_usage']['confirmed_tokens'],70)

    def test_unknown_provider_usage_is_not_zero_or_successful_connection(self):
        def failed(*args,**kwargs):raise ProviderError('fixture_timeout',uncertain=True)
        with self.assertRaises(tasks.TaskDeferred):self.run_review(failed)
        public=next(e for e in self.graph()['exploration_events'] if e['event_type']=='navigation_model_observed')
        self.assertEqual(public['model_usage']['executions'][0]['usage_status'],'unknown')
        self.assertIsNone(public['model_usage']['executions'][0]['total_tokens'])
        self.assertEqual(AIExecution.query.one().status,'uncertain')
        self.assertGreater(AIExecution.query.one().reserved_tokens,0)

    def test_pending_execution_is_separate_from_unknown_finished_usage(self):
        self.run_review();row=AIExecution.query.one()
        row.status='sending';row.usage={'known':False};row.finished_at=None;db.session.commit()
        graph=self.graph();summary=graph['exploration_history']['model_usage']
        self.assertEqual(summary['pending_executions'],1)
        self.assertEqual(summary['unknown_usage'],0)
        public=next(e for e in graph['exploration_events'] if e['event_type']=='navigation_model_observed')
        self.assertEqual(public['model_usage']['executions'][0]['usage_status'],'pending')

    def test_proven_unsent_preflight_failure_is_not_billed_as_a_model_request(self):
        from backend.ai.configuration import AIConfigError
        with patch('backend.ai.runtime.credential_for',side_effect=['synthetic',AIConfigError('fixture gate','provider_connection_paused')]):
            with self.assertRaises(tasks.TaskDeferred):self.run_review()
        row=AIExecution.query.one();self.assertFalse(row.request_sent)
        use,=self.reviews()[0]['model_uses'];self.assertEqual(use['use_kind'],'no_dispatch')
        public=next(e for e in self.graph()['exploration_events'] if e['event_type']=='navigation_model_observed')
        self.assertEqual(public['model_usage']['executions'][0]['usage_status'],'not_dispatched')
        self.assertIsNone(public['model_usage']['executions'][0]['total_tokens'])

    def test_source_receipt_retains_references_after_the_published_page_window_is_gone(self):
        self.run_review();version=self.f.connect()
        before=self.graph();self.assertEqual(before['exploration_history']['model_usage']['confirmed_tokens'],70)
        with self.f.store.connect() as connection:connection.execute('DELETE FROM exploration_events')
        RuntimeCatalog(self.f.app.config['SOURCE_CATALOG_PATH']).publish(self.f.store,self.f.key)
        after=self.graph()
        self.assertTrue(any(e['event_type']=='navigation_model_observed' for e in after['exploration_events']))
        self.assertEqual(after['exploration_history']['model_usage']['confirmed_tokens'],70)
        self.assertEqual(after['source_count'],1)

    def test_material_change_after_call_keeps_old_observation_but_applies_no_result(self):
        def changed(*args,**kwargs):
            response=self.response(*args,**kwargs)
            self.f.store.finish(self.f.key,fixture.ROOT,state='fetched',html='<title>Changed document</title>')
            return response
        result,calls=self.run_review(changed)
        self.assertEqual(result['error_code'],'material_changed');self.assertEqual(calls,1)
        self.assertEqual(len(self.reviews()),1)
        self.assertFalse(any(e['event_type']=='navigation_application_started' for e in self.f.events()))
        self.assertEqual(AIExecution.query.count(),1)

    def test_legacy_cache_without_an_origin_stays_unknown(self):
        self.run_review()
        with self.f.store.connect() as connection:
            key,raw=connection.execute('SELECT cache_key,output_json FROM navigation_role_cache').fetchone()
            value=json.loads(raw);value.pop('execution_reference',None)
            connection.execute('UPDATE navigation_role_cache SET output_json=? WHERE cache_key=?',(json.dumps(value),key))
        self.reset_review();_,calls=self.run_review();self.assertEqual(calls,0)
        public=next(e for e in self.graph()['exploration_events'] if e['id']==self.reviews()[-1]['id'])
        self.assertEqual(public['model_usage']['cache_origin_unknown'],1)
        self.assertEqual(public['model_usage']['executions'],[])

    def test_wrong_input_reference_cannot_borrow_an_invoice(self):
        from backend.services.execution_observations import project_ledger,capture_ledger
        self.run_review();events=deepcopy(self.f.events())
        review=next(e for e in events if e['event_type']=='navigation_model_observed')
        review['model_uses'][0]['input_digest']='f'*64
        rows=capture_ledger(events,[],school_id=self.school.id)
        summary=project_ledger(events,rows)
        self.assertEqual(summary['confirmed_tokens'],0)
        self.assertEqual(summary['unverified_references'],1)
        self.assertFalse(review['model_usage']['executions'][0]['ledger_verified'])

    def test_reconciliation_refreshes_usage_without_rewriting_structure_or_events(self):
        from backend.ai.runtime import reconcile_usage
        def unknown(*args,**kwargs):raise ProviderError('fixture_timeout',uncertain=True)
        with self.assertRaises(tasks.TaskDeferred):self.run_review(unknown)
        before=self.graph();immutable=deepcopy(self.f.events())
        row=AIExecution.query.one()
        reconcile_usage(row.execution_id,80)
        after=self.graph()
        self.assertEqual(before['structure_version'],after['structure_version'])
        self.assertEqual(before['nodes'],after['nodes']);self.assertEqual(before['relations'],after['relations'])
        self.assertEqual(self.f.events(),immutable)
        self.assertEqual(after['exploration_history']['model_usage']['confirmed_tokens'],80)
        first=transport_update(before,school_id=self.school.id)
        second=transport_update(after,school_id=self.school.id,since=first['structure_token'])
        self.assertNotEqual(first['structure_token'],second['structure_token'])

    def test_foreign_school_or_unverifiable_legacy_context_is_not_disclosed(self):
        self.run_review();row=AIExecution.query.one()
        row.diagnostics={'discovery_context':{'school_id':self.school.id+99}};db.session.commit()
        graph=self.graph();summary=graph['exploration_history']['model_usage']
        self.assertEqual(summary['verified_executions'],0)
        self.assertEqual(summary['confirmed_tokens'],0)

    def test_malformed_receipts_and_untrusted_fields_do_not_escape_or_break_reading(self):
        from backend.services.execution_observations import capture_ledger,public_uses,reference
        self.assertIsNone(reference({'execution_id':'routes:'+'a'*64}))
        self.assertEqual(public_uses([{'execution_id':'private-token-url'}]),[])
        self.assertEqual(capture_ledger([],[{'execution_json':[]},{'execution_json':{'page_receipt':['bad']}}],school_id=self.school.id),[])

    def test_observation_does_not_reopen_the_runtime_callers_transaction(self):
        from backend.services.execution_observations import observe_model_uses
        from backend.ai.runtime import _execute
        self.run_review();row=AIExecution.query.one();school_id=self.school.id
        values={key:getattr(row,key) for key in ('execution_id','purpose','skill_id','skill_version',
            'skill_digest','input_digest','mode')}
        db.session.remove()
        with observe_model_uses() as observed:
            _execute(**values,binding=BINDING,messages=[],validator=lambda _:None,school_id=school_id)
            self.assertFalse(db.session().in_transaction())
        self.assertEqual(len(observed['uses']),1)
        self.assertEqual(observed['uses'][0]['use_kind'],'ledger_reuse')


if __name__=='__main__':unittest.main()
