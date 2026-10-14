"""A school shares actual request limits, concurrency and tokens across workers."""
import unittest
from unittest.mock import patch

from backend.ai.models import AIBudget, AIExecution
from backend.ai.configuration import save_limits
from backend.ai.runtime import run_skill, AIBudgetError
from backend.database.db import db
from backend.services import tasks


class SchoolAIBudgetTests(unittest.TestCase):
    def setUp(self):
        from backend.database import source_governance_models  # Register the durable work ledger.
        from tests.test_ai_runtime import AIRuntimeTests
        self.fixture = AIRuntimeTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def invoke(self, ident):
        evidence = {'school_id': 3, 'candidates': [{'candidate_id': 'page', 'url': 'https://example.edu.cn/'}],
                    'evidence': [{'evidence_id': 'page', 'html': '<h1>学校介绍</h1>'}]}
        return run_skill('university-source-onboarding', 'column', evidence, 'directory', ident)

    def response(self):
        return self.fixture.response({'candidate_id': 'page', 'status': 'not_column', 'columns': [], 'reason': '学校介绍'})

    def test_request_limit_counts_dispatches_not_cache_and_survives_new_task_generation(self):
        from backend.ai.school_budget import school_usage
        save_limits({'school_requests': 2})
        parent = tasks.enqueue('discover', 3, {'school_id': 3})
        handle = tasks.claim(capabilities=['directory'])
        with patch('backend.ai.providers.complete', return_value=self.response()) as provider:
            with tasks.execution_scope(handle):
                self.invoke('first')
                self.invoke('first')
            tasks.finish(handle, {})
            tasks.enqueue('discover', 3, {'school_id': 3}, replace_finished=True)
            again = tasks.claim(capabilities=['directory'])
            self.assertGreater(again['generation'], handle['generation'])
            with tasks.execution_scope(again):
                self.invoke('second')
                with self.assertRaises(AIBudgetError) as blocked:
                    self.invoke('third')
            self.assertEqual(blocked.exception.code, 'budget_exhausted')
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(school_usage(3)['used_requests'], 2)
        self.assertEqual(school_usage(3)['reserved_requests'], 0)
        self.assertEqual(school_usage(3)['used_tokens'], 140)
        self.assertIsNone(db.session.get(AIExecution, 'third'))

    def test_one_school_cannot_reserve_a_second_call_from_another_parent(self):
        from backend.ai.runtime import _reserve, _settle
        def reserve(name, school):
            return _reserve(name, 'directory', self.fixture.binding, 'university-source-onboarding',
                '1', 'digest', name, 'column', 10, school_id=school)
        one, _ = reserve('one', 3)
        with self.assertRaises(AIBudgetError) as blocked:
            reserve('same-school', 3)
        self.assertEqual(blocked.exception.code, 'concurrency_limit')
        two, _ = reserve('another-school', 4)
        self.assertEqual(AIExecution.query.count(), 2)
        _settle(one.execution_id, status='failed', usage={'known': True, 'total_tokens': 0})
        self.assertTrue(reserve('free-school', 3)[1])
        self.assertTrue(all(row.used_requests == 0 for row in AIBudget.query.all()))

    def test_unknown_paid_result_keeps_reservation_without_retrying_request(self):
        from backend.ai.providers import ProviderError
        from backend.ai.school_budget import school_usage
        with patch('backend.ai.providers.complete', side_effect=ProviderError('read_timeout', uncertain=True)) as provider:
            first = self.invoke('unknown')
            second = self.invoke('unknown')
        self.assertEqual(first, second)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(school_usage(3)['used_requests'], 1)
        self.assertEqual(school_usage(3)['used_tokens'], 0)
        self.assertGreater(school_usage(3)['reserved_tokens'], 0)
        self.assertEqual(school_usage(3)['unconfirmed_requests'],1)
        from backend.services.onboarding_progress import _ai_message
        message=_ai_message(None,{'ai_usage':school_usage(3),'ai_error_code':'budget_exhausted'},True)
        self.assertIn('结果未确认',message)
        self.assertNotIn('额度已用完',message)

    def test_explicit_limit_change_resumes_saved_items_without_new_round(self):
        from datetime import datetime, timedelta
        from backend.database.models import School
        from backend.database.source_governance_models import DiscoveryWorkItem
        from backend.ai.school_budget import school_usage
        db.session.add(School(id=3, name='Budget recovery', url='https://example.edu.cn/'))
        db.session.commit()
        save_limits({'school_requests': 1})
        with patch('backend.ai.providers.complete', return_value=self.response()):
            self.invoke('spent')
        before = school_usage(3)
        job = tasks.enqueue('navigation_review', 'saved', {'school_id': 3})
        job.state, job.phase = 'waiting', 'ai_budget'
        job.deadline_at = datetime.utcnow() - timedelta(days=1)
        job.checkpoint = {'route_cursor': 16}
        item = DiscoveryWorkItem(identity='saved', school_id=3, generation=1, group_key='page',
            candidate_id='entry', kind='navigation', material_hash='hash', input_json={},
            state='paused', error_code='budget_exhausted')
        db.session.add(item); db.session.commit()
        save_limits({'school_requests': 1})
        self.assertEqual(job.state, 'waiting')
        save_limits({'school_requests': 2})
        self.assertEqual(job.state, 'pending')
        self.assertEqual(item.state, 'pending')
        self.assertEqual(job.checkpoint['route_cursor'], 16)
        self.assertGreater(job.deadline_at, datetime.utcnow())
        self.assertEqual(school_usage(3), before)


if __name__ == '__main__':
    unittest.main()
