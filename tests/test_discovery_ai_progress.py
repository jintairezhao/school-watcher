"""Show executed discovery AI work, including independent navigation workers."""
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import School
from backend.services import tasks


class DiscoveryAIProgressTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.school = db.session.get(School, self.fixture.school_id)
        self.parent = tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
        self.parent.state = 'done'
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def child(self, key, result, *, current=True):
        job = tasks.enqueue('navigation_review', key, {'school_id': self.school.id,
            'parent_task_id': self.parent.id, 'parent_generation': self.parent.generation,
            'school_generation': self.parent.generation if current else self.parent.generation - 1})
        job.state, job.result = 'done', result
        db.session.commit()
        return job

    def progress(self):
        from backend.services.onboarding_progress import status
        with patch('backend.services.onboarding_progress.ai_available', return_value=True):
            return status(self.school)

    def test_completed_navigation_child_is_reported_and_old_round_is_excluded(self):
        self.child('current', {'status': 'processed', 'reviewed_routes': 12, 'cached_routes': 3})
        self.child('stale', {'status': 'processed', 'reviewed_routes': 80}, current=False)
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'succeeded')
        self.assertEqual(progress['ai_reviewed_routes'], 12)
        self.assertEqual(progress['ai_cached_routes'], 3)
        self.assertIn('12', progress['ai_message'])

    def test_budget_failure_remains_visible_alongside_completed_identification(self):
        self.child('good', {'status': 'processed', 'reviewed_routes': 8})
        self.child('limited', {'status': 'needs_recovery', 'error_code': 'budget_exhausted'})
        progress = self.progress()
        self.assertEqual(progress['ai_reviewed_routes'], 8)
        self.assertIn('额度已用完，进度已保存', progress['ai_message'])

    def test_school_opt_out_does_not_claim_ai_is_active(self):
        self.parent.payload = dict(self.parent.payload, ai_assist=False)
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'disabled')
        self.assertIn('关闭', progress['ai_message'])

    def test_waiting_budget_is_saved_progress_instead_of_failed_school_or_missing_worker(self):
        from datetime import datetime, timedelta
        job = self.child('limited', {})
        job.state, job.phase = 'waiting', 'ai_budget'
        job.queued_at = self.parent.queued_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'budget_paused')
        self.assertIn('额度已用完，进度已保存', progress['message'])
        self.assertFalse(progress['can_retry'])
        self.assertFalse(progress['active'])

    def test_access_limited_source_reports_saved_background_recovery(self):
        from datetime import datetime, timedelta
        job = tasks.enqueue('onboard', 'restricted', {'school_id': self.school.id,
            'url': 'https://example.edu.cn/notices/'})
        due = (datetime.utcnow() + timedelta(hours=6)).isoformat()
        job.state, job.result = 'done', {'state': 'access_limited',
            'reason': '官网限制正文访问', 'recovery_not_before': due,
            'recovery_rules': [{'name': '通知公告'}],
            'issues': [{'name': '通知公告', 'reason': '官网限制正文访问',
                        'codes': ['article_access_restricted']}]}
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'retry_wait')
        self.assertIn('进度已保存', progress['message'])
        self.assertIn('后台', progress['message'])
        self.assertEqual(progress['waiting_recovery_count'], 1)
        self.assertEqual(progress['failed_count'], 0)
        self.assertEqual(progress['retry_at'], due + 'Z')
        self.assertFalse(progress['needs_verification'])


if __name__ == '__main__':
    unittest.main()
