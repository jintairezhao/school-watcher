"""Model-slot handoff preserves fair turns without replaying paid work."""
from datetime import datetime, timedelta
import unittest

from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.ai.models import AIExecution
from backend.ai.runtime import _settle
from backend.services import tasks


class AIQueueFairnessTests(unittest.TestCase):
    def setUp(self):
        from tests.test_queue_fairness import QueueFairnessTests
        self.fixture = QueueFairnessTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def pending(self, key, *, state='pending', phase='ai_resource', code='concurrency_limit'):
        row = tasks.enqueue('navigation_review', key, {'school_id': self.fixture.school.id})
        row.state, row.phase, row.error_code = state, phase, code
        row.available_at = datetime.utcnow() + timedelta(seconds=10)
        row.checkpoint = {'route_cursor': 8, 'original_execution': 'retain-this-identity'}
        db.session.commit()
        return row

    def completion(self):
        row = AIExecution(execution_id='settled-slot', purpose='directory', config_version='1',
            provider='deepseek', model='test', skill_id='test', skill_version='1',
            skill_digest='a' * 64, input_digest='b' * 64, mode='navigation',
            status='sending', request_sent=True, reserved_tokens=0, budget_keys=[])
        db.session.add(row); db.session.commit()
        return row

    def test_slot_settlement_wakes_only_confirmed_unsent_resource_waiters(self):
        ready = self.pending('ready')
        paid = self.pending('paid', state='waiting', phase='ai_result', code='ai_result_unknown')
        paused = self.pending('paused', state='waiting', phase='user_paused', code='')
        budget = self.pending('budget', state='waiting', phase='ai_budget', code='budget_exhausted')
        pending = self.pending('original-response', code='')
        unchanged = {row.id: (row.available_at, dict(row.checkpoint)) for row in (paid, paused, budget, pending)}
        self.completion()
        _settle('settled-slot', status='succeeded', usage={'known': True, 'total_tokens': 70})
        db.session.expire_all()
        self.assertLessEqual(ready.available_at, datetime.utcnow())
        self.assertEqual(ready.claim_count, 0)
        for row in (paid, paused, budget, pending):
            self.assertEqual((row.available_at, row.checkpoint), unchanged[row.id])
        self.assertEqual(AIExecution.query.count(), 1)

    def test_a_pre_dispatch_resource_wait_does_not_lose_its_turn_or_material(self):
        row = tasks.enqueue('navigation_review', 'route', {'school_id': self.fixture.school.id})
        row.checkpoint = {'route_cursor': 8}; db.session.commit()
        handle = tasks.claim(capabilities=['directory'])
        tasks.handoff(handle, tasks.TaskDeferred(capability='directory', phase='ai_resource',
            error_code='concurrency_limit', keep_place=True, delay=10))
        db.session.refresh(row)
        self.assertEqual(row.claim_count, 0)
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.checkpoint, {'route_cursor': 8})
        self.assertEqual(AIExecution.query.count(), 0)

    def test_replaying_a_settled_execution_does_not_release_another_slot(self):
        self.completion()
        _settle('settled-slot', status='succeeded', usage={'known': True, 'total_tokens': 70})
        waiting = self.pending('later')
        stamp = waiting.available_at
        _settle('settled-slot', status='succeeded', usage={'known': True, 'total_tokens': 70})
        db.session.refresh(waiting)
        self.assertEqual(waiting.available_at, stamp)
        self.assertEqual(AIExecution.query.count(), 1)

    def test_new_evidence_format_cannot_bypass_an_unknown_original_page(self):
        from backend.services.source_agent import _column_evidence_layout
        row = self.completion()
        row.mode = 'column'
        row.diagnostics = {'discovery_context': {'school_id': self.fixture.school.id,
                           'page_url': 'https://example.edu.cn/notices'}}
        db.session.commit()
        self.assertEqual(_column_evidence_layout({}, self.fixture.school.id,
            'https://example.edu.cn/notices'), 'legacy')
        self.assertEqual(_column_evidence_layout({}, self.fixture.school.id,
            'https://example.edu.cn/other'), 'focused-repair-2')
        self.assertEqual(AIExecution.query.count(), 1)

    def test_unattributed_legacy_paid_input_and_existing_cursor_keep_the_old_format(self):
        from backend.services.source_agent import _column_evidence_layout
        self.assertEqual(_column_evidence_layout({'column_regions': {'cursor': 2}},
            self.fixture.school.id, 'https://example.edu.cn/notices'), 'legacy')
        row = self.completion(); row.mode = 'column'; db.session.commit()
        self.assertEqual(_column_evidence_layout({}, self.fixture.school.id,
            'https://example.edu.cn/notices'), 'legacy')
        row.usage = {'known': True, 'total_tokens': 70}; row.status = 'succeeded'
        db.session.commit()
        self.assertEqual(_column_evidence_layout({}, self.fixture.school.id,
            'https://example.edu.cn/new-observed-page'), 'focused-repair-2')

    def test_existing_compact_plan_is_not_rewritten_to_obtain_a_different_paid_key(self):
        from backend.services.source_agent import _column_evidence_layout
        self.assertEqual(_column_evidence_layout({'column_evidence_layout': 'compact-locations-1'},
            self.fixture.school.id, 'https://example.edu.cn/notices'), 'compact-locations-1')


if __name__ == '__main__':
    unittest.main()
