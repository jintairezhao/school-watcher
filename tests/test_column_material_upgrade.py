"""Saved paid decisions survive new observation metadata and prompt resources.

The public PGS fixture is exact retained HTTP HTML. Its pre-upgrade material
shape differs only by heading_locations, confirmed independently against 2.8.
All execution rows and budgets below live in a temporary test database.
"""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.ai.models import AIBudget, AIExecution
from backend.ai.skill_loader import digest
from backend.database.db import db
from backend.scraper.discovery.column_regions import materials
from backend.services import tasks
from backend.services.source_agent import recognize_column

URL = 'https://pgs.ruc.edu.cn/'
HTML = (Path(__file__).parent / 'fixtures/acquisition/retained-pgs-tabs.html').read_bytes().decode('utf8')

class ColumnMaterialUpgradeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.current = materials(self.fixture.school_id, URL, HTML)
        self.original = copy.deepcopy(self.current)
        for part in self.original:
            for proof in part['evidence']:
                proof.pop('heading_locations', None)
        self.assertEqual(len(self.current), 2)
        self.assertNotEqual(digest(self.current), digest(self.original))
        heading = next(row for proof in self.current[0]['evidence']
                       for row in proof.get('heading_locations', []) if row['heading'] == '通知公告')
        self.column = {'name': '通知公告', 'container_selector': heading['containing_region_selector'],
            'heading_selector': heading['selector'], 'list_selector': 'div.tab-show li',
            'title_selector': 'a', 'link_selector': 'a', 'date_selector': 'span', 'content_selector': ''}
        self.output = {'candidate_id': 'page', 'status': 'ready', 'columns': [self.column], 'reason': ''}
        self.binding = {'id': None, 'version': 1}
        self.skill_digest = 'a' * 64
        self.ident = 'column-region:' + digest([self.skill_digest, self.original[0], None, 1])
        self.budget = AIBudget(key='school-discovery:upgrade-test', used_requests=40,
                              used_tokens=100000, reserved_tokens=10000, active_count=0)
        db.session.add(self.budget)
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def row(self, *, status='succeeded', known=True, output=None):
        value = AIExecution(execution_id=self.ident, purpose='directory', profile_id=None,
            config_version='1', provider='deepseek', model='fixture', mode='column',
            skill_id='university-source-onboarding', skill_version='1', skill_digest=self.skill_digest,
            input_digest=digest({'evidence': self.original[0], 'repair_feedback': None}),
            status=status, request_sent=True, active_released=True, usage={'known': known, 'total_tokens': 100},
            reserved_tokens=10000 if not known else 0, budget_keys=[self.budget.key],
            output=self.output if output is None else output)
        db.session.add(value)
        db.session.commit()
        return value

    def task(self, *, pending=True, cursor=0, columns=None):
        saved = {'hash': digest(self.original), 'cursor': cursor,
                 'columns': columns or [], 'unresolved': [], 'decision_ids': [self.ident] if cursor else []}
        checkpoint = {'column_regions': saved}
        if pending:
            checkpoint['column_pending_decision'] = {'execution_id': self.ident, 'evidence': self.original[0],
                'binding': [None, 1], 'skill_digest': self.skill_digest,
                'material_hash': digest(self.original), 'cursor': 0}
        job = tasks.enqueue('onboard', 'upgrade-test', {'school_id': self.fixture.school_id, 'url': URL})
        job.checkpoint = checkpoint
        db.session.commit()
        return job, tasks.claim(capabilities=['http'])

    def ledger(self):
        db.session.refresh(self.budget)
        return (self.budget.used_requests, self.budget.used_tokens, self.budget.reserved_tokens,
                self.budget.active_count, self.budget.reserved_requests)

    def recognize(self):
        return recognize_column(self.fixture.school_id, URL, HTML)

    def test_old_paid_complete_result_is_retrieved_before_new_material_or_binding(self):
        row = self.row()
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', side_effect=AssertionError('No new binding needed')), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Do not clone the paid request')):
            result = self.recognize()
        self.assertEqual(result['columns'], [self.column])
        self.assertEqual(result['decision_ids'], [self.ident])
        self.assertFalse(result['material_complete'])
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 1)
        self.assertNotIn('column_pending_decision', handle['checkpoint'])
        self.assertEqual(row.skill_digest, self.skill_digest)
        self.assertEqual(self.ledger(), before)

    def test_settled_cursor_columns_and_original_id_survive_metadata_only_upgrade(self):
        self.row()
        _, handle = self.task(pending=False, cursor=1, columns=[self.column])
        before = self.ledger()
        from backend.ai.configuration import AIConfigError
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')) as model:
            result = self.recognize()
        self.assertEqual(model.call_args.args[2], self.current[1])
        self.assertEqual(result['columns'], [self.column])
        self.assertEqual(result['decision_ids'], [self.ident])
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 1)
        self.assertEqual(result['error_code'], 'budget_exhausted')
        self.assertEqual(self.ledger(), before)

    def test_waiting_original_result_can_settle_between_claims_without_a_replacement(self):
        row = self.row(status='sending', known=False)
        job, handle = self.task()
        before = self.ledger()
        original_plan = copy.deepcopy(handle['checkpoint']['column_pending_decision'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No replacement')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        tasks.handoff(handle, waiting.exception)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(waiting.exception.delay, 10)
        self.assertEqual(job.checkpoint['column_pending_decision'], original_plan)
        row.status, row.usage = 'succeeded', {'known': True, 'total_tokens': 100}
        db.session.commit()
        from datetime import datetime, timedelta
        job.available_at = datetime.utcnow() - timedelta(seconds=1)  # Advance the local fixture clock.
        db.session.commit()
        resumed = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(resumed), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Reuse the now-complete original result')):
            result = self.recognize()
        self.assertEqual(result['columns'], [self.column])
        self.assertEqual(result['decision_ids'], [self.ident])
        self.assertEqual(self.ledger(), before)

    def test_unknown_paid_result_remains_waiting_with_old_input_and_budget(self):
        self.row(status='uncertain', known=False)
        _, handle = self.task()
        original = copy.deepcopy(handle['checkpoint'])
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Unknown request never authorizes a new ID')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'ai_result_unknown')
        self.assertEqual(handle['checkpoint']['column_pending_decision']['evidence'],
                         original['column_pending_decision']['evidence'])
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertEqual(self.ledger(), before)

    def test_success_with_mismatched_original_input_is_not_adopted(self):
        row = self.row()
        row.input_digest = 'b' * 64
        db.session.commit()
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Mismatch cannot authorize another fee')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'execution_conflict')
        self.assertEqual(handle['checkpoint']['column_regions']['columns'], [])
        self.assertIn('column_pending_decision', handle['checkpoint'])
        self.assertEqual(self.ledger(), before)

    def test_partial_output_cannot_be_adopted_as_a_complete_column_decision(self):
        self.row(status='partial', known=True)
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Do not replay partial output')):
            result = self.recognize()
        self.assertEqual(result['columns'], [])
        self.assertEqual(result['decision_ids'], [self.ident])
        self.assertEqual(handle['checkpoint']['column_regions']['unresolved'], [0])
        self.assertEqual(self.ledger(), before)

    def test_success_with_unknown_usage_is_consumed_once_then_waits_for_the_original_bill(self):
        self.row(known=False)
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No next fee')):
            first = self.recognize()
            self.assertEqual(first['columns'], [self.column])
            self.assertTrue(handle['checkpoint']['column_pending_decision']['consumed'])
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
            self.assertEqual(waiting.exception.error_code, 'ai_result_unknown')
        self.assertEqual(handle['checkpoint']['column_regions']['columns'], [self.column])
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 1)
        self.assertEqual(self.ledger(), before)

    def test_missing_original_material_keeps_the_success_and_budget_without_adopting_it(self):
        self.row()
        _, handle = self.task()
        handle['checkpoint']['column_pending_decision'].pop('evidence')
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Missing proof does not authorize payment')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'original_material_missing')
        self.assertEqual(handle['checkpoint']['column_regions']['columns'], [])
        self.assertIn('column_pending_decision', handle['checkpoint'])
        self.assertEqual(self.ledger(), before)

    def test_changed_partition_does_not_mark_unread_material_complete_from_one_old_fragment(self):
        self.row()
        _, handle = self.task()
        before = self.ledger()
        from backend.ai.configuration import AIConfigError
        enlarged = dict(self.current[0], evidence=self.current[0]['evidence'] + self.current[1]['evidence'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')) as model:
            self.assertEqual(self.recognize()['columns'], [self.column])
            with patch('backend.scraper.discovery.column_regions.materials', return_value=[enlarged]):
                second = self.recognize()
        self.assertEqual(model.call_args.args[2], enlarged)
        self.assertFalse(second['material_complete'])
        self.assertEqual(second['error_code'], 'budget_exhausted')
        self.assertEqual(second['columns'], [self.column])
        self.assertEqual(second['decision_ids'], [self.ident])
        self.assertEqual(self.ledger(), before)

    def test_confirmed_unsent_failure_preserves_cursor_for_the_existing_resume_gate(self):
        row = self.row(status='failed')
        row.request_sent = False
        row.usage = {'known': True, 'total_tokens': 0}
        row.error_code = 'interrupted_before_dispatch'
        row.output = None
        db.session.commit()
        _, handle = self.task()
        before = self.ledger()
        from backend.ai.configuration import AIConfigError
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')) as model:
            result = self.recognize()
        self.assertEqual(model.call_args.args[2], self.current[0])
        self.assertEqual(result['decision_ids'], [])
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertEqual(self.ledger(), before)
        self.assertFalse(row.request_sent)

    def test_normal_consumed_success_with_unknown_usage_is_not_appended_again(self):
        self.row(known=False)
        _, handle = self.task(cursor=1, columns=[self.column])
        handle['checkpoint']['column_pending_decision']['blocked'] = 'usage'
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Already consumed result has only its original unknown bill')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'ai_result_unknown')
        self.assertEqual(handle['checkpoint']['column_regions']['columns'], [self.column])
        self.assertEqual(handle['checkpoint']['column_regions']['decision_ids'], [self.ident])
        self.assertEqual(self.ledger(), before)

    def test_inconsistent_zero_cost_row_cannot_clear_pending_or_skip_the_region(self):
        row = self.row(status='failed')
        row.error_code = 'provider_connection_paused'
        row.request_sent = False
        row.usage = {'known': True, 'total_tokens': 0, 'input_tokens': 5}
        row.reserved_tokens = 10
        row.output = {'partial': 'retained'}
        db.session.commit()
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Incomplete free proof cannot authorize a replacement')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'execution_conflict')
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertIn('column_pending_decision', handle['checkpoint'])
        self.assertEqual(self.ledger(), before)

    def test_cached_preflight_pause_uses_connection_handoff_without_counting_a_decision(self):
        _, handle = self.task(pending=False)
        handle['checkpoint'] = {}
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'failed',
                'usage': {'known': True, 'total_tokens': 0}, 'error_code': 'provider_connection_paused'}):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.phase, 'ai_connection')
        self.assertEqual(waiting.exception.error_code, 'provider_connection_paused')
        self.assertEqual(waiting.exception.checkpoint['column_regions']['cursor'], 0)
        self.assertEqual(waiting.exception.checkpoint['column_regions']['decision_ids'], [])
        self.assertEqual(self.ledger(), before)

    def test_unknown_bill_keeps_actual_execution_id_reported_by_the_runtime(self):
        _, handle = self.task(pending=False)
        handle['checkpoint'] = {}
        actual_id = 'connection-resume:actual-fixture-id'
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'uncertain', 'usage': {'known': False},
                'provenance': {'execution_id': actual_id}}):
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
        self.assertEqual(handle['checkpoint']['column_pending_decision']['execution_id'], actual_id)
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertEqual(self.ledger(), before)

    def test_missing_usage_is_unknown_for_a_sent_failed_request(self):
        row = self.row(status='failed', known=False)
        row.usage = {}
        db.session.commit()
        _, handle = self.task()
        before = self.ledger()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Legacy missing usage cannot authorize another ID')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                self.recognize()
        self.assertEqual(waiting.exception.error_code, 'ai_result_unknown')
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertEqual(self.ledger(), before)


if __name__ == '__main__':
    unittest.main()
