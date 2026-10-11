"""Unknown paid column results must keep their original work and fee ledger."""
import copy
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.services import tasks


class ColumnUnknownCheckpointTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests, URL
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.url = URL
        self.binding = {'id': None, 'version': 1}
        self.pieces = [{'school_id': self.fixture.school_id,
                        'candidates': [{'candidate_id': 'page', 'url': URL}],
                        'evidence': [{'evidence_id': str(i), 'html': f'<section id="s{i}"></section>'}]}
                       for i in range(9)]

    def tearDown(self):
        self.fixture.tearDown()

    def execution(self, index, *, status='uncertain', known=False):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        skill = load_skill('university-source-onboarding', 'column')
        ident = 'column-region:' + digest([skill.resource_digest, self.pieces[index], None, 1])
        row = AIExecution(execution_id=ident, purpose='directory', profile_id=None, config_version='1',
            provider='deepseek', model='fixture', skill_id='university-source-onboarding', skill_version=skill.version,
            skill_digest=skill.resource_digest, input_digest=digest({'evidence': self.pieces[index], 'repair_feedback': None}),
            mode='column', status=status, request_sent=True, active_released=True,
            reserved_tokens=10000 if not known else 0, usage={'known': known, 'total_tokens': 200 if known else 0},
            output={'status': 'not_column', 'columns': []} if status == 'succeeded' else None,
            error_code='' if status == 'succeeded' else 'connection_lost')
        db.session.add(row)
        return row

    def task(self, checkpoint=None):
        job = tasks.enqueue('onboard', 'unknown-column', {'school_id': self.fixture.school_id, 'url': self.url})
        if checkpoint is not None:
            job.checkpoint = checkpoint
            db.session.commit()
        return job, tasks.claim(capabilities=['http'])

    def recognize(self):
        from backend.services.source_agent import recognize_column
        return recognize_column(self.fixture.school_id, self.url, 'retained-original-page')

    def test_unknown_result_neither_completes_a_decision_nor_dispatches_another_region(self):
        job, handle = self.task()
        unknown = {'status': 'uncertain', 'usage': {'known': False}, 'error_code': 'connection_lost'}
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value=unknown) as model:
            with self.assertRaises(tasks.TaskDeferred) as stopped:
                self.recognize()
            first = copy.deepcopy(handle['checkpoint'])
            self.assertEqual(first['column_regions']['cursor'], 0)
            self.assertEqual(first['column_regions']['decision_ids'], [])
            self.assertEqual(stopped.exception.state, 'waiting')
            self.assertEqual(stopped.exception.error_code, 'ai_result_unknown')
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
            self.assertEqual(model.call_count, 1)
            self.assertEqual(handle['checkpoint']['column_pending_decision'], first['column_pending_decision'])

    def test_unknown_original_plan_survives_changed_material_and_model_without_a_new_request(self):
        job, handle = self.task()
        unknown = {'status': 'uncertain', 'usage': {'known': False}, 'error_code': 'connection_lost'}
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value=unknown):
            with self.assertRaises(tasks.TaskDeferred) as stopped:
                self.recognize()
        tasks.handoff(handle, stopped.exception)
        job = db.session.get(type(job), job.id)
        original = copy.deepcopy(job.checkpoint['column_pending_decision'])
        job.state = 'pending'
        db.session.commit()
        resumed = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(resumed), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=list(reversed(self.pieces))), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 2}), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Unknown paid request must not be replaced')):
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
        self.assertEqual(resumed['checkpoint']['column_pending_decision'], original)

    def test_provider_gate_wait_keeps_cursor_and_does_not_count_a_free_rejection(self):
        from backend.ai.configuration import AIConfigError
        job, handle = self.task()
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('连接已暂停', 'provider_connection_paused')):
            with self.assertRaises(tasks.TaskDeferred) as stopped:
                self.recognize()
        self.assertEqual(stopped.exception.state, 'waiting')
        self.assertEqual(stopped.exception.error_code, 'provider_connection_paused')
        self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 0)
        self.assertEqual(handle['checkpoint']['column_regions']['decision_ids'], [])

    def test_valid_output_with_unknown_usage_can_be_verified_before_next_paid_action_waits(self):
        job, handle = self.task()
        column = {'name': '真实公告', 'list_selector': '#real li'}
        completed = {'status': 'succeeded', 'usage': {'known': False},
                     'output': {'status': 'ready', 'columns': [column]}}
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value=completed) as model:
            result = self.recognize()
            self.assertEqual(result['columns'], [column])
            self.assertEqual(handle['checkpoint']['column_regions']['cursor'], 1)
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
            self.assertEqual(model.call_count, 1)

    def test_valid_rules_install_before_an_unknown_usage_halts_further_model_work(self):
        from backend.services.source_onboarding import onboard_page
        from backend.database.models import Department, Announcement
        from tests.test_direct_onboarding import ai_column
        job, handle = self.task()
        completed = {'status': 'succeeded', 'usage': {'known': False},
                     'output': {'status': 'ready', 'columns': [ai_column()]}}
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value=completed) as model:
            with self.assertRaises(tasks.TaskDeferred) as first_slice:
                onboard_page(job.payload, fetcher=self.fixture.fetch)
            self.assertEqual(first_slice.exception.phase, 'source_material')
            self.assertEqual(Department.query.count(), 1)
            self.assertEqual(Announcement.query.count(), 3)
            with self.assertRaises(tasks.TaskDeferred) as blocked:
                onboard_page(job.payload, fetcher=self.fixture.fetch)
            self.assertEqual(blocked.exception.error_code, 'ai_result_unknown')
            self.assertEqual(model.call_count, 1)

    def test_legacy_unknown_decisions_reopen_only_the_polluted_stop_without_fee_reset(self):
        from backend.ai.models import AIBudget
        from backend.ai.skill_loader import digest
        from backend.services.discovery_policy import advance_progress, progress_state
        from backend.services.source_governance import _snapshot
        from backend.services.source_onboarding import resume_rule_discovery
        from tests.test_direct_onboarding import HTML
        rows = [self.execution(i, status='succeeded' if i < 3 else 'uncertain', known=i < 3)
                for i in range(8)]
        budget = AIBudget(key='school-discovery:fixture', used_requests=8, used_tokens=600, reserved_tokens=50000)
        db.session.add(budget)
        db.session.commit()
        progress = progress_state()
        for row in rows:
            progress = advance_progress(progress, row.execution_id)
        self.assertTrue(progress['stopped'])
        ref = _snapshot(HTML, self.url, role='list')
        saved = {'column_regions': {'hash': digest(self.pieces), 'cursor': 8, 'columns': [],
                    'unresolved': list(range(3, 8)), 'decision_ids': [r.execution_id for r in rows]},
                 'onboarding_page': ref,
                 'column_validation': {'stage': 'done', 'progress': progress, 'material_pending': True,
                    'material_feedback': [], 'department_ids': [41], 'completed': ['retained-success'],
                    'result': {'state': 'unsupported', 'error_code': 'no_progress'}}}
        job, handle = self.task(saved)
        tasks.finish(handle, saved['column_validation']['result'])
        generation = job.generation
        before = (budget.used_requests, budget.used_tokens, budget.reserved_tokens)
        resume_rule_discovery()
        db.session.refresh(job)
        repaired = job.checkpoint['column_validation']
        self.assertEqual(job.state, 'waiting')
        self.assertEqual(job.phase, 'ai_result')
        self.assertEqual(job.generation, generation)
        self.assertFalse(repaired['progress']['stopped'])
        self.assertEqual(repaired['progress']['decisions'], 3)
        self.assertEqual(repaired['department_ids'], [41])
        self.assertEqual(repaired['completed'], ['retained-success'])
        self.assertEqual(before, (budget.used_requests, budget.used_tokens, budget.reserved_tokens))
        blocked_plan = copy.deepcopy(job.checkpoint['column_pending_decision'])
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual(job.checkpoint['column_pending_decision'], blocked_plan)
        self.assertEqual(job.generation, generation)

    def test_confirmed_no_progress_stop_is_preserved_and_user_pause_is_respected(self):
        from backend.ai.skill_loader import digest
        from backend.services.discovery_policy import advance_progress, progress_state
        from backend.services.source_onboarding import resume_rule_discovery
        rows = [self.execution(i, status='succeeded', known=True) for i in range(8)]
        db.session.commit()
        progress = progress_state()
        for row in rows:
            progress = advance_progress(progress, row.execution_id)
        checkpoint = {'column_regions': {'hash': digest(self.pieces), 'cursor': 8,
                       'decision_ids': [row.execution_id for row in rows]},
                      'column_validation': {'stage': 'done', 'progress': progress}}
        job, handle = self.task(checkpoint)
        tasks.finish(handle, {'state': 'unsupported', 'error_code': 'no_progress'})
        before = copy.deepcopy(job.checkpoint)
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.checkpoint, before)
        rows[7].status, rows[7].usage = 'uncertain', {'known': False}
        job.payload = dict(job.payload, discovery_pause_requested=True)
        db.session.commit()
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.checkpoint, before)

    def test_one_settled_legacy_request_cannot_unblock_other_unknown_paid_results(self):
        from backend.services.source_agent import pause_unknown_column
        rows = [self.execution(i) for i in range(2)]
        db.session.commit()
        checkpoint = {'column_pending_decision': {'execution_id': rows[0].execution_id,
            'blocked': 'result', 'legacy_execution_ids': [row.execution_id for row in rows]}}
        job, handle = self.task(checkpoint)
        rows[0].status, rows[0].usage = 'succeeded', {'known': True, 'total_tokens': 200}
        db.session.commit()
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Pending outcomes never authorize a new request')):
            with self.assertRaises(tasks.TaskDeferred):
                pause_unknown_column(handle['checkpoint'])
        self.assertEqual(handle['checkpoint'], checkpoint)

    def test_confirmed_unsent_connection_resume_retains_the_new_unknown_execution(self):
        from backend.ai.connection_recovery import resumed_execution_id
        row = self.execution(0, status='failed', known=True)
        row.request_sent = False
        row.error_code = 'provider_connection_paused'
        row.usage = {'known': True, 'total_tokens': 0}
        db.session.commit()
        saved = {'ai_connection_resume': 1}
        actual_id = resumed_execution_id(row.execution_id, checkpoint=saved)
        job, handle = self.task(saved)
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=self.pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value=self.binding), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'uncertain', 'usage': {'known': False}}) as model:
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
            self.assertEqual(model.call_args.args[4], actual_id)
            self.assertEqual(handle['checkpoint']['column_pending_decision']['execution_id'], actual_id)
            with self.assertRaises(tasks.TaskDeferred):
                self.recognize()
            self.assertEqual(model.call_count, 1)
        self.assertFalse(row.request_sent)
        self.assertEqual(row.usage['total_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
