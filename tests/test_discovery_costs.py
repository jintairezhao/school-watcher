"""Real execution metadata is attributed once; reservations are not invoices."""
import json
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event

from backend.ai.models import AIExecution, AISchoolRound
from backend.ai.providers import ProviderError, ProviderResult
from backend.database.db import db
from backend.database.models import Department, School
from backend.database.source_governance_models import SourceProposal
from backend.services import tasks
from backend.services.discovery_costs import (capture_discovery_context, load_discovery_costs,
                                              preserve_discovery_context)
from tests import test_ai_runtime as runtime_fixture


URL = 'https://example.edu.cn/opaque/'
BRANCH = 'https://example.edu.cn/college/'
EVIDENCE = {'school_id': 1, 'candidates': [{'candidate_id': 'one', 'name': '通知',
    'url': URL, 'kind': 'notice_source', 'path': '学生工作处'}],
    'evidence': [{'candidate_id': 'one', 'evidence_id': 'p0', 'text': '本科生申请资助材料'}]}
OUTPUT = {'results': [{'candidate_id': 'one', 'role': 'publishing', 'value': 'relevant',
    'historical': 'high', 'reason': '准备材料', 'facts': [{'kind': 'history', 'text': '资助材料',
    'quote': '本科生申请资助材料', 'evidence_id': 'p0'}]}]}


class DiscoveryCostsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.AIRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.school = School(name='测试大学', url='https://example.edu.cn/')
        db.session.add(self.school)
        db.session.commit()
        self.site = {'name': self.school.name, 'root_url': self.school.url}

    def handle(self, kind='navigation_review', **payload):
        data = dict(school_id=self.school.id, url=URL, page={'url': URL, 'snapshot_url': URL}, **payload)
        row = tasks.enqueue(kind, str(self.school.id) + ':' + str(len(tasks.BackgroundTask.query.all())), data)
        row.checkpoint = {'navigation_branch_url': BRANCH}
        db.session.commit()
        claimed = tasks.claim(capabilities=[row.capability])
        self.assertEqual(claimed['id'], row.id)
        return claimed

    def invoke(self, ident='invoice', handle=None, provider=None):
        from backend.ai.runtime import run_skill
        response = ProviderResult(json.dumps(OUTPUT), 'stop',
            {'known': True, 'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20}, 'provider-receipt')
        with tasks.execution_scope(handle or self.handle()), patch('backend.ai.providers.complete',
            side_effect=provider, return_value=response) as transport:
            result = run_skill('student-information', 'assess', dict(EVIDENCE, school_id=self.school.id),
                               'directory', ident)
        return result, transport.call_count

    def context(self, **changes):
        key = 'school-discovery:' + str(self.school.id) + ':1:1'
        return dict({'task_id': 1, 'generation': 1, 'task_kind': 'onboard',
                     'school_id': self.school.id, 'school_budget_key': key,
                     'page_url': URL, 'branch_url': BRANCH}, **changes)

    def execution(self, name, *, sent=True, known=True, tokens=20, reserve=0,
                  context='default', status='succeeded', budget_key=None, usage=None):
        context = self.context() if context == 'default' else context
        key = budget_key or 'school-discovery:' + str(self.school.id) + ':1:1'
        row = AIExecution(execution_id=name, purpose='directory', config_version='1',
            provider='deepseek', model='not-a-real-request', skill_id='student-information',
            skill_version='1', skill_digest='digest', input_digest='material', mode='navigation',
            request_sent=sent, status=status, reserved_tokens=reserve, budget_keys=[key],
            usage=usage if usage is not None else {'known': known, 'total_tokens': tokens},
            diagnostics={'discovery_context': context} if context else {})
        db.session.add(row)
        db.session.commit()
        return row

    def test_actual_dispatch_context_survives_progress_and_final_provider_diagnostics(self):
        def provider(*args, **kwargs):
            kwargs['on_progress']({'received_bytes': 100, 'temporary_provider_phase': 'waiting',
                                   'discovery_context': {'school_id': 999}})
            during = db.session.get(AIExecution, 'invoice', populate_existing=True)
            self.assertEqual(during.diagnostics['discovery_context']['school_id'], self.school.id)
            return ProviderResult(json.dumps(OUTPUT), 'stop', {'known': True, 'total_tokens': 20},
                                  'receipt', {'received_bytes': 250})
        result, count = self.invoke(provider=provider)
        self.assertEqual((result['status'], count), ('succeeded', 1))
        row = db.session.get(AIExecution, 'invoice', populate_existing=True)
        context = row.diagnostics['discovery_context']
        self.assertEqual((context['page_url'], context['branch_url']), (URL, BRANCH))
        self.assertNotIn('temporary_provider_phase', row.diagnostics)
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['totals']['actual_requests'], 1)
        self.assertEqual(costs['totals']['confirmed_tokens'], 20)
        self.assertEqual(costs['by_page'][URL]['confirmed_tokens'], 20)
        self.assertEqual(costs['by_branch'][BRANCH]['confirmed_tokens'], 20)

    def test_cached_result_reuses_one_execution_and_one_provider_invoice(self):
        handle = self.handle()
        first, one = self.invoke(handle=handle)
        second, zero = self.invoke(handle=handle)
        self.assertEqual(first, second)
        self.assertEqual((one, zero), (1, 0))
        self.assertEqual(load_discovery_costs(self.site)['totals']['actual_requests'], 1)

    def test_provider_active_duration_does_not_become_school_elapsed_time(self):
        one = self.execution('timed-one');two = self.execution('timed-two')
        one.diagnostics = dict(one.diagnostics, elapsed_seconds=2.5)
        two.diagnostics = dict(two.diagnostics, elapsed_seconds=3.5)
        db.session.commit()
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['totals']['provider_active_seconds'], 6)
        self.assertEqual(costs['totals']['timed_requests'], 2)
        self.assertIsNone(costs['elapsed_seconds'])
        self.assertEqual(costs['by_page'][URL]['provider_active_seconds'], 6)

    def test_round_counter_without_execution_details_exposes_attribution_gap(self):
        from backend.ai.models import AIBudget
        self.invoke()
        costs = load_discovery_costs(self.site)
        self.assertTrue(costs['attribution_complete'])
        ledger = db.session.get(AIBudget, costs['round_key'])
        ledger.used_requests += 4;ledger.used_tokens += 10000;db.session.commit()
        costs = load_discovery_costs(self.site)
        self.assertFalse(costs['attribution_complete'])
        self.assertEqual(costs['ledger_gap']['actual_requests'], 4)
        self.assertEqual(costs['ledger_gap']['tokens'], 10000)
        self.assertEqual(costs['totals']['confirmed_tokens'], 20)

    def test_automatic_value_calls_are_attributed_to_the_existing_source(self):
        source = Department(school_id=self.school.id, name='真实公告栏目', list_url=URL,
                            list_selector='#notices li')
        db.session.add(source);db.session.commit()
        handle = self.handle(kind='student_assessment', subject_kind='source',
                             subject_id=source.id, automatic_source=True)
        result, count = self.invoke(handle=handle)
        self.assertEqual((result['status'], count), ('succeeded', 1))
        context = db.session.get(AIExecution, 'invoice').diagnostics['discovery_context']
        self.assertEqual(context['source_id'], source.id)
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['by_page'][URL]['confirmed_tokens'], 20)
        self.assertEqual(costs['unattributed']['actual_requests'], 0)

    def test_explicit_article_analysis_does_not_become_source_exploration(self):
        handle = self.handle(kind='student_assessment', subject_kind='article', subject_id=1, requested_by=1)
        self.assertEqual(capture_discovery_context(handle, self.school.id,
                         'school-discovery:' + str(self.school.id) + ':1:1'), {})

    def test_unknown_paid_request_keeps_reservation_and_is_not_resent(self):
        handle = self.handle()
        def interrupted(*args, **kwargs):
            kwargs['on_progress']({'received_bytes': 15})
            raise ProviderError('network_result_unknown', uncertain=True,
                                diagnostics={'received_bytes': 15})
        first, one = self.invoke(handle=handle, provider=interrupted)
        second, zero = self.invoke(handle=handle)
        self.assertEqual((first['status'], second['status'], one, zero), ('uncertain', 'uncertain', 1, 0))
        totals = load_discovery_costs(self.site)['totals']
        self.assertEqual((totals['actual_requests'], totals['unknown_requests'], totals['confirmed_tokens']), (1, 1, 0))
        self.assertGreater(totals['reserved_tokens'], 0)
        self.assertEqual(totals['reserved_requests'], 0)

    def test_unsent_reservation_is_separate_from_actual_provider_requests(self):
        self.execution('reserved', sent=False, known=False, reserve=100, status='reserved')
        self.execution('unknown', known=False, reserve=200, status='uncertain')
        self.execution('confirmed', tokens=50)
        self.execution('unsent-failure', sent=False, tokens=0, status='failed')
        totals = load_discovery_costs(self.site)['totals']
        self.assertEqual(totals['execution_count'], 4)
        self.assertEqual((totals['actual_requests'], totals['confirmed_tokens']), (2, 50))
        self.assertEqual((totals['reserved_requests'], totals['reserved_tokens'], totals['unknown_requests']), (1, 300, 1))

    def test_legacy_or_inexact_context_is_unattributed_not_guessed(self):
        for index, context in enumerate((None, {}, self.context(school_id=999),
                self.context(school_budget_key='another-round'), self.context(task_id='1'),
                self.context(page_url='', branch_url=''), self.context(task_kind='collect'))):
            self.execution('legacy-' + str(index), context=context)
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['unattributed']['actual_requests'], 7)
        self.assertEqual(costs['by_page'], {})
        self.assertEqual(costs['totals']['confirmed_tokens'], 140)

    def test_onboard_page_can_be_known_while_branch_remains_unknown(self):
        self.execution('page-only', context=self.context(branch_url=''))
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['by_page'][URL]['actual_requests'], 1)
        self.assertEqual(costs['by_branch'], {})
        self.assertEqual(costs['unattributed_branch']['actual_requests'], 1)

    def test_snapshot_identity_has_priority_over_final_redirect_url(self):
        original = 'https://example.edu.cn/original-entry/'
        self.execution('redirect', context=self.context(snapshot_url=original))
        costs = load_discovery_costs(self.site)
        self.assertIn(original, costs['by_page'])
        self.assertNotIn(URL, costs['by_page'])

    def test_current_school_round_does_not_mix_old_round_or_neighbor_school(self):
        current_key = self.context()['school_budget_key']
        db.session.add(AISchoolRound(school_id=self.school.id, budget_key=current_key))
        db.session.commit()
        self.execution('current')
        self.execution('older', budget_key=current_key + '-old', tokens=10000)
        self.execution('neighbor', budget_key='school-discovery:' + str(self.school.id * 10) + ':1:1')
        costs = load_discovery_costs(self.site)
        self.assertEqual(costs['scope'], 'current_school_round')
        self.assertEqual(costs['totals']['confirmed_tokens'], 20)

    def test_shared_school_website_does_not_borrow_campus_name_or_root_path(self):
        self.execution('one')
        other = School(name='另一校区', url=self.school.url)
        db.session.add(other)
        db.session.commit()
        self.assertEqual(load_discovery_costs(dict(self.site, name=other.name))['totals']['actual_requests'], 0)
        self.assertEqual(load_discovery_costs(dict(self.site, root_url=self.school.url + 'campus/'))['scope'], 'unavailable')
        db.session.add(School(name=self.school.name, url=self.school.url))
        db.session.commit()
        self.assertEqual(load_discovery_costs(self.site)['scope'], 'unavailable')

    def test_invalid_usage_never_becomes_a_confirmed_bill(self):
        for index, usage in enumerate(({'known': True, 'total_tokens': True},
                {'known': True, 'total_tokens': '50'}, {'known': True, 'total_tokens': -1},
                {'known': False, 'total_tokens': 5000}, None, [])):
            row = self.execution('invalid-' + str(index), usage=usage if usage is not None else {})
            if usage is None:
                row.usage = None
                db.session.commit()
        totals = load_discovery_costs(self.site)['totals']
        self.assertEqual((totals['confirmed_tokens'], totals['unknown_requests']), (0, 6))

    def test_administrator_reconciliation_is_not_provider_reported_usage(self):
        self.execution('reconciled', usage={'known': True, 'total_tokens': 70,
                                          'source': 'administrator_reconciliation'})
        totals = load_discovery_costs(self.site)['totals']
        self.assertEqual(totals['confirmed_tokens'], 0)
        self.assertEqual((totals['reconciled_requests'], totals['reconciled_tokens']), (1, 70))

    def test_cost_reader_is_select_only_and_never_loads_model_output_or_credentials(self):
        self.execution('one')
        pending = School(name='not written', url='https://notwritten.example.edu.cn/')
        db.session.add(pending)
        sql = []
        def executed(connection, cursor, statement, parameters, context, executemany):
            sql.append(statement)
        event.listen(db.engine, 'before_cursor_execute', executed)
        try:
            costs = load_discovery_costs(self.site)
        finally:
            event.remove(db.engine, 'before_cursor_execute', executed)
        self.assertIsNone(pending.id)
        self.assertEqual(costs['totals']['actual_requests'], 1)
        text = '\n'.join(sql).lower()
        self.assertNotIn('ai_executions.output', text)
        self.assertNotIn('encrypted_key', text)
        self.assertTrue(all(statement.lstrip().upper().startswith('SELECT') for statement in sql))

    def test_reader_without_initialized_database_is_explicitly_unavailable(self):
        self.fixture.ctx.pop()
        try:
            self.assertEqual(load_discovery_costs(self.site)['scope'], 'unavailable')
            with Flask(__name__).app_context():
                self.assertEqual(load_discovery_costs(self.site)['scope'], 'unavailable')
        finally:
            self.fixture.ctx.push()

    def test_program_or_database_failure_is_not_disguised_as_zero_cost(self):
        with patch.object(db.session, 'execute', side_effect=RuntimeError('broken database')):
            with self.assertRaisesRegex(RuntimeError, 'broken database'):
                load_discovery_costs(self.site)

    def test_only_exact_persisted_task_generation_and_discovery_kind_can_attribute(self):
        handle = self.handle()
        context = capture_discovery_context(handle, self.school.id, 'round')
        self.assertEqual(context['task_id'], handle['id'])
        self.assertEqual(capture_discovery_context(dict(handle, generation=99), self.school.id, 'round'), {})
        self.assertEqual(capture_discovery_context(self.handle(kind='collect'), self.school.id, 'round'), {})
        # Uncommitted/forged in-memory checkpoint values are never copied.
        handle['checkpoint']['navigation_branch_url'] = 'https://forged.example.edu.cn/'
        self.assertEqual(capture_discovery_context(handle, self.school.id, 'round')['branch_url'], BRANCH)

    def test_source_identity_is_recorded_only_when_it_belongs_to_exact_school(self):
        source = Department(school_id=self.school.id, name='通知', list_url=URL, list_selector='li')
        db.session.add(source)
        db.session.commit()
        handle = self.handle(kind='onboard', department_id=source.id)
        self.assertEqual(capture_discovery_context(handle, self.school.id, 'round')['source_id'], source.id)
        other = School(name='other', url='https://other.example.edu.cn/')
        db.session.add(other)
        db.session.commit()
        self.assertNotIn('source_id', capture_discovery_context(handle, other.id, 'round'))

    def test_uninstalled_source_review_uses_saved_candidate_url_without_inventing_source(self):
        config = {'name': '公开通知', 'list_url': URL, 'list_selector': 'li'}
        proposal = SourceProposal(school_id=self.school.id, identity_key='identity',
            proposal_key='new-source-config', origin='configuration', state='proposed',
            candidate_json=json.dumps(config), expected_config_hash='old', evidence_hash='evidence')
        db.session.add(proposal)
        db.session.commit()
        row = tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id})
        handle = tasks.claim(capabilities=['directory'])
        self.assertEqual(row.id, handle['id'])
        context = capture_discovery_context(handle, self.school.id, 'round')
        self.assertEqual(context['page_url'], URL)
        self.assertNotIn('source_id', context)
        self.assertNotIn('branch_url', context)

    def test_directory_purpose_outside_discovery_task_does_not_capture_context(self):
        handle = self.handle(kind='student_assessment')
        result, calls = self.invoke(handle=handle)
        self.assertEqual((result['status'], calls), ('succeeded', 1))
        self.assertNotIn('discovery_context', result['diagnostics'])
        self.assertEqual(load_discovery_costs(self.site)['unattributed']['actual_requests'], 1)

    def test_only_our_context_survives_progress_field_replacement(self):
        old = {'discovery_context': self.context(), 'temporary_phase': 'waiting', 'received_bytes': 10}
        new = {'received_bytes': 20, 'discovery_context': {'school_id': 999}}
        kept = preserve_discovery_context(old, new)
        self.assertEqual(kept['discovery_context'], self.context())
        self.assertNotIn('temporary_phase', kept)
        self.assertEqual(kept['received_bytes'], 20)
        self.assertEqual(preserve_discovery_context({}, new), {'received_bytes': 20})

    def test_non_discovery_summary_execution_keeps_existing_runtime_behavior(self):
        with tasks.execution_scope(self.handle(kind='collect')), patch('backend.ai.providers.complete',
                return_value=self.fixture.response()):
            result = self.fixture.invoke('summary-outside-scope')
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('discovery_context', result['diagnostics'])
        self.assertEqual(load_discovery_costs(self.site)['totals']['actual_requests'], 0)


if __name__ == '__main__':
    unittest.main()
