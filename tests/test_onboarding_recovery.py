"""Unusable pages recover autonomously without replaying stale parent work."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import BackgroundTask, Department, Subscription
from backend.services import tasks


class OnboardingRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def failed_page(self, key, *, hours=7, state='unsupported'):
        from tests.test_direct_onboarding import URL
        row = tasks.enqueue('onboard', key, {'school_id': self.fixture.school_id, 'url': URL,
            'snapshot': {'old': 'snapshot'}, 'ai_assist': True})
        row.state, row.result = 'done', {'state': state}
        row.finished_at = datetime.utcnow() - timedelta(hours=hours)
        row.checkpoint = {'onboarding_page': {'old': 'snapshot'}}
        db.session.commit()
        return row

    def test_automatic_retry_is_oldest_first_small_and_ignores_recent_or_noncolumns(self):
        from backend.services.source_onboarding import pending_onboarding_recovery, retry_unconnected_columns
        candidates = [self.failed_page(str(index), hours=index + 7) for index in range(6)]
        recent = self.failed_page('recent', hours=1)
        not_column = self.failed_page('not-column', hours=20, state='not_column')
        paused = self.failed_page('paused', hours=30)
        paused.payload = dict(paused.payload, discovery_pause_requested=True); db.session.commit()
        self.assertTrue(pending_onboarding_recovery(self.fixture.school_id))
        ids = retry_unconnected_columns(self.fixture.school_id, 'background:1', automatic=True)
        self.assertEqual(ids, [row.id for row in reversed(candidates[2:])])
        for row in (recent, not_column, paused):
            db.session.refresh(row)
            self.assertEqual((row.state, row.generation), ('done', 1))
        self.assertTrue(pending_onboarding_recovery(self.fixture.school_id))

    def test_successful_background_round_rebinds_child_and_reads_fresh_page_without_ai(self):
        from backend.worker import dispatch
        from tests.test_direct_onboarding import URL
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        child = self.failed_page('repair')
        child.payload = dict(child.payload, parent_task_id=parent.id, parent_generation=parent.generation)
        parent.state = 'done'; db.session.commit()
        tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id,
            'trigger': 'background_discovery', 'ai_assist': False})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), patch('backend.services.discovery_cache.adapt_site', return_value={
                'continuation_required': False, 'onboarding_ids': []}):
            result = dispatch('discover', handle['payload'])
            dispatch('discover', handle['payload'])
        tasks.finish(handle, result)
        db.session.refresh(child)
        self.assertEqual((child.state, child.generation), ('pending', 2))
        self.assertEqual(child.payload['parent_generation'], handle['generation'])
        self.assertFalse(child.payload['ai_assist'])
        self.assertTrue(child.payload['revalidate'])
        self.assertNotIn('snapshot', child.payload)
        self.assertEqual(child.checkpoint, {})
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding._read', side_effect=self.fixture.fetch), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('AI disabled')):
            connected = dispatch('onboard', handle['payload'])
        self.assertEqual(connected['state'], 'connected')
        self.assertIn((URL, 'directory'), self.fixture.reads)

    def test_blocked_homepage_does_not_restart_a_child(self):
        from backend.worker import dispatch
        child = self.failed_page('unprobed')
        tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id,
            'trigger': 'background_discovery', 'ai_assist': False})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), patch('backend.services.discovery_cache.adapt_site', return_value={
                'entry_failure': {'status_code': 403}, 'continuation_required': False}):
            dispatch('discover', handle['payload'])
        db.session.refresh(child)
        self.assertEqual((child.state, child.generation), ('done', 1))

    def test_recovery_signal_still_exists_when_other_sources_work(self):
        from backend.services.source_onboarding import pending_onboarding_recovery
        self.assertFalse(pending_onboarding_recovery(self.fixture.school_id))
        db.session.add(Department(school_id=self.fixture.school_id, name='可用通知',
            list_url='https://example.edu.cn/working', list_selector='li'))
        db.session.commit()
        self.failed_page('another-source')
        self.assertTrue(pending_onboarding_recovery(self.fixture.school_id))

    def test_single_page_repair_preserves_source_id_and_latest_ai_opt_out(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL, HTML
        source = Department(school_id=self.fixture.school_id, name='旧栏目名', list_url=URL,
            list_selector='#old li', title_selector='a', link_selector='a', date_selector='span')
        db.session.add(source); db.session.commit()
        tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id, 'ai_assist': False})
        with patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('AI disabled')), \
             patch('backend.services.student_information.queue_source_assessment') as source_ai, \
             patch('backend.services.student_information.queue_listing_assessment') as title_ai:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'automatic_repair': True,
                'repair_department_id': source.id, 'revalidate': True, 'ai_assist': True}, fetcher=self.fixture.fetch)
        self.assertEqual(result['department_ids'], [source.id])
        self.assertNotEqual(source.list_selector, '#old li')
        self.assertEqual(Department.query.count(), 1)
        self.assertFalse(source_ai.call_args.kwargs['ai_assist'])
        self.assertFalse(title_ai.call_args.kwargs['ai_assist'])

    def test_unsubscribed_repair_does_not_fetch_or_change_parser(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL
        source = Department(school_id=self.fixture.school_id, name='旧栏目', list_url=URL, list_selector='#old li')
        db.session.add(source)
        Subscription.query.one().department_ids = []; db.session.commit()
        with patch('backend.services.source_onboarding._read', side_effect=AssertionError('No longer subscribed')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'automatic_repair': True,
                'repair_department_id': source.id, 'revalidate': True})
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(source.list_selector, '#old li')


if __name__ == '__main__':
    unittest.main()
