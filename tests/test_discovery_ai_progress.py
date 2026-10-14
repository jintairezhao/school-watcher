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

    def waiting_ai(self, phase, code, *, kind='navigation_review'):
        from datetime import datetime, timedelta
        job = self.child('waiting-ai', {})
        job.kind = kind
        if kind == 'student_assessment':
            job.payload = dict(job.payload, automatic_source=True)
        job.state, job.phase, job.error_code = 'waiting', phase, code
        job.queued_at = self.parent.queued_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        return job

    def test_connection_wait_preserves_progress_even_without_worker_or_enabled_ai(self):
        self.waiting_ai('ai_connection', 'provider_connection_paused')
        from backend.services.onboarding_progress import status
        with patch('backend.services.onboarding_progress.ai_available', return_value=False):
            progress = status(self.school)
        self.assertEqual(progress['state'], 'ai_connection')
        self.assertEqual(progress['ai_state'], 'connection_paused')
        self.assertIn('连续连接失败', progress['ai_message'])
        self.assertIn('进度已保存', progress['message'])
        self.assertIn('等待 AI 连接恢复', progress['stage_message'])
        self.assertFalse(progress['active'])
        self.assertFalse(progress['busy'])
        self.assertFalse(progress['can_retry'])
        self.assertFalse(progress['needs_verification'])

    def test_unknown_result_is_waiting_not_failed_or_automatic_retry(self):
        job = self.waiting_ai('ai_result', 'ai_result_unknown')
        job.result = {'status': 'needs_recovery', 'error_code': 'connection_lost'}
        job.checkpoint = {'discovery_progress': {'ai_state': 'uncertain'}}
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'ai_result')
        self.assertEqual(progress['ai_state'], 'result_unknown')
        self.assertIn('AI 返回结果尚未确认', progress['message'])
        self.assertIn('进度已保存', progress['ai_message'])
        self.assertIn('未重复调用', progress['stage_message'])
        self.assertFalse(progress['active'])
        self.assertFalse(progress['can_retry'])

    def test_ai_wait_does_not_hide_actual_website_work(self):
        self.waiting_ai('ai_connection', 'provider_connection_paused')
        job = tasks.enqueue('onboard', 'reading-site', {'school_id': self.school.id})
        job.state = 'running'
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'running')
        self.assertIn('正在读取官网通知', progress['message'])
        self.assertIn('连续连接失败', progress['ai_message'])
        self.assertIn('其他信息仍在处理', progress['stage_message'])
        self.assertTrue(progress['active'])
        self.assertTrue(progress['busy'])

    def test_automatic_assessment_connection_wait_is_visible(self):
        self.waiting_ai('ai_connection', 'provider_connection_paused', kind='student_assessment')
        progress = self.progress()
        self.assertEqual(progress['state'], 'ai_connection')
        self.assertIn('进度已保存', progress['message'])
        self.assertFalse(progress['can_retry'])

    def resumed_navigation(self, *, running=False):
        job = self.waiting_ai('ai_connection', 'provider_connection_paused')
        job.checkpoint = {'route_cursor': 7, 'ai_connection_resume': 1,
            'discovery_progress': {'ai_state': 'failed', 'ai_error_code': 'provider_connection_paused'}}
        job.state, job.phase, job.error_code = ('running' if running else 'pending'), 'ai_resume', ''
        db.session.commit()
        return job

    def test_verified_resume_does_not_show_old_connection_failure_while_queued(self):
        job = self.resumed_navigation()
        saved = dict(job.checkpoint)
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'queued')
        self.assertNotIn('不可用', progress['ai_message'])
        self.assertNotIn('连续连接失败', progress['ai_message'])
        self.assertIn('安排', progress['ai_message'])
        db.session.refresh(job)
        self.assertEqual(job.checkpoint, saved)

    def test_verified_resume_does_not_show_old_failure_while_running(self):
        self.resumed_navigation(running=True)
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'running')
        self.assertNotIn('不可用', progress['ai_message'])

    def test_parent_resume_also_projects_current_state_without_erasing_cursor(self):
        self.parent.state, self.parent.phase, self.parent.error_code = 'pending', 'ai_resume', ''
        self.parent.checkpoint = {'page_cursor': 9, 'ai_connection_resume': 1,
            'discovery_progress': {'ai_state': 'failed', 'ai_error_code': 'provider_connection_paused'}}
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'queued')
        self.assertNotIn('不可用', progress['ai_message'])
        self.assertEqual(self.parent.checkpoint['page_cursor'], 9)

    def test_a_new_connection_pause_after_resume_remains_visible(self):
        job = self.resumed_navigation()
        job.state, job.phase, job.error_code = 'waiting', 'ai_connection', 'provider_connection_paused'
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'ai_connection')
        self.assertIn('连续连接失败', progress['ai_message'])
        self.assertIn('测试', progress['ai_message'])

    def test_old_navigation_wait_cannot_report_new_round_as_disconnected(self):
        job = self.child('previous-round-wait', {}, current=False)
        job.state, job.phase, job.error_code = 'waiting', 'ai_connection', 'provider_connection_paused'
        self.child('current-good', {'status': 'processed', 'reviewed_routes': 4})
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'succeeded')
        self.assertNotEqual(progress['state'], 'ai_connection')
        self.assertNotIn('连续连接失败', progress['ai_message'])

    def test_cumulative_usage_is_separate_from_current_connection_state(self):
        from backend.ai.models import AIBudget, AISchoolRound
        key = f'school-discovery:{self.school.id}:fixture'
        db.session.add_all([AISchoolRound(school_id=self.school.id, budget_key=key),
            AIBudget(key=key, used_requests=5, used_tokens=8239, reserved_tokens=11068, active_count=1)])
        self.resumed_navigation()
        progress = self.progress()
        self.assertEqual(progress['ai_state'], 'queued')
        self.assertIn('本轮累计调用 5 次', progress['ai_usage_message'])
        self.assertIn('已确认用量 8,239 Token', progress['ai_usage_message'])
        self.assertIn('预留 11,068 Token', progress['ai_usage_message'])
        self.assertIn('待确认', progress['ai_usage_message'])

    def test_html_and_polling_share_usage_copy_without_spending_or_mutation(self):
        from bs4 import BeautifulSoup
        from backend.ai.models import AIBudget, AISchoolRound
        from backend.database.models import User
        key = f'school-discovery:{self.school.id}:fixture'
        ledger = AIBudget(key=key, used_requests=5, used_tokens=8239, reserved_tokens=11068)
        db.session.add_all([AISchoolRound(school_id=self.school.id, budget_key=key), ledger])
        job = self.resumed_navigation()
        saved = dict(job.checkpoint)
        client = self.fixture.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = User.query.filter_by(username='reader').one().id
        with patch('backend.services.onboarding_progress.ai_available', return_value=True), \
             patch('backend.ai.providers.complete', side_effect=AssertionError('Progress must not call the provider')):
            page = client.get(f'/subscriptions/{self.school.id}')
            poll = client.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual((page.status_code, poll.status_code), (200, 200))
        html = BeautifulSoup(page.text, 'html.parser')
        self.assertEqual(html.select_one('#discoveryAI').text, poll.json['ai_message'])
        self.assertEqual(html.select_one('#discoveryAIUsage').text, poll.json['ai_usage_message'])
        self.assertNotIn('hidden', html.select_one('#discoveryAIUsage').attrs)
        db.session.refresh(job); db.session.refresh(ledger)
        self.assertEqual(job.checkpoint, saved)
        self.assertEqual((ledger.used_requests, ledger.used_tokens, ledger.reserved_tokens), (5, 8239, 11068))

    def test_parent_connection_wait_without_children_is_saved(self):
        self.parent.state, self.parent.phase = 'waiting', 'ai_connection'
        self.parent.error_code = 'provider_connection_paused'
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'ai_connection')
        self.assertFalse(progress['active'])
        self.assertFalse(progress['can_retry'])

    def test_waiting_page_and_poll_show_same_saved_state_without_mutating_progress(self):
        from bs4 import BeautifulSoup
        from backend.database.models import User
        job = self.waiting_ai('ai_result', 'ai_result_unknown')
        job.checkpoint = {'route_cursor': 7, 'route_pending_decision': {'fingerprint': 'paid-original'}}
        db.session.commit()
        checkpoint, generation = job.checkpoint, job.generation
        client = self.fixture.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = User.query.filter_by(username='reader').one().id
        with patch('backend.services.onboarding_progress.ai_available', return_value=True), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Progress reads must not call AI')):
            page = client.get(f'/subscriptions/{self.school.id}')
            poll = client.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual((page.status_code, poll.status_code), (200, 200))
        html = BeautifulSoup(page.text, 'html.parser')
        self.assertEqual(html.select_one('#discoveryMessage').text, poll.json['message'])
        self.assertEqual(html.select_one('#discoveryStages').text, poll.json['stage_message'])
        self.assertIn('未重复调用', html.select_one('#discoveryStages').text)
        self.assertIn('hidden', html.select_one('#discoveryRetry').attrs)
        self.assertIn('hidden', html.select_one('#discoveryProgress').attrs)
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase, job.generation, job.checkpoint),
                         ('waiting', 'ai_result', generation, checkpoint))

    def test_budget_wait_keeps_running_website_work_visible(self):
        self.waiting_ai('ai_budget', 'budget_exhausted')
        job = tasks.enqueue('onboard', 'reading-site', {'school_id': self.school.id})
        job.state = 'running'
        db.session.commit()
        progress = self.progress()
        self.assertEqual(progress['state'], 'running')
        self.assertIn('额度已用完，进度已保存', progress['message'])
        self.assertIn('已接入的栏目继续更新', progress['message'])
        self.assertTrue(progress['active'])
        self.assertTrue(progress['busy'])

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
