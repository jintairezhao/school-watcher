"""The school-to-inbox flow produces value without a department review step."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, School
from backend.services import tasks
import test_direct_onboarding as fixture_module

URL = fixture_module.URL


class StudentDiscoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.school_id = self.fixture.school_id

    def tearDown(self):
        self.fixture.tearDown()

    def test_verified_notices_automatically_queue_value_without_known_department(self):
        from backend.services.source_onboarding import onboard_page
        from backend.services.onboarding_progress import status
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)
        assessments = BackgroundTask.query.filter_by(kind='student_assessment').all()
        self.assertEqual(len(assessments), 2)
        self.assertEqual({t.payload['subject_kind'] for t in assessments}, {'source', 'listing_batch'})
        self.assertTrue(all(t.payload.get('requested_by') is None for t in assessments))
        progress = status(db.session.get(School, self.school_id))
        self.assertEqual(progress['article_count'], 3)
        self.assertEqual(progress['verified_source_count'], 1)
        self.assertEqual(progress['failed_count'], 0)

    def test_disabled_discovery_ai_is_inherited_by_first_collection(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            result = onboard_page({'school_id': self.school_id, 'url': URL, 'ai_assist': False},
                                  fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(BackgroundTask.query.filter_by(kind='student_assessment').count(), 0)
        self.assertFalse(BackgroundTask.query.filter_by(kind='collect').one().payload['ai_assist'])

    def test_pause_stops_automatic_value_but_keeps_explicit_article_request(self):
        from backend.services.discovery_control import control
        tasks.enqueue('discover', self.school_id, {'school_id': self.school_id})
        automatic = tasks.enqueue('student_assessment', 'auto', {'school_id': self.school_id,
            'subject_kind': 'listing_batch', 'automatic_listing': True}, capability='directory')
        explicit = tasks.enqueue('student_assessment', 'explicit', {'school_id': self.school_id,
            'subject_kind': 'article', 'requested_by': 1}, capability='directory')
        control(self.school_id, 'pause')
        db.session.refresh(automatic); db.session.refresh(explicit)
        self.assertEqual((automatic.state, automatic.phase), ('waiting', 'user_paused'))
        self.assertEqual(explicit.state, 'pending')
        control(self.school_id, 'resume')
        db.session.refresh(automatic)
        self.assertEqual(automatic.state, 'pending')

    def test_saved_leads_resume_on_schedule_without_requesting_user_review(self):
        from backend.worker import _schedule_due
        job = tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'ai_assist': False})
        job.state = 'done'
        job.result = {'exploration_limited': True, 'deferred_pages': 12}
        job.finished_at = datetime.utcnow() - timedelta(hours=7)
        db.session.commit()
        _schedule_due()
        job = BackgroundTask.query.filter_by(identity=f'discover:{self.school_id}').one()
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.payload['trigger'], 'background_discovery')
        self.assertFalse(job.payload['ai_assist'])
        self.assertFalse(job.payload.get('refresh'))

    def test_ai_opt_out_survives_future_collection_and_legacy_collection_payload(self):
        from backend.services.source_onboarding import onboard_page
        from backend.services.inbox_refresh import queue_sources
        from backend.services.source_collection import collect_source
        from backend.database.models import Department
        tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'ai_assist': False})
        result = onboard_page({'school_id': self.school_id, 'url': URL, 'ai_assist': False},
                              fetcher=self.fixture.fetch)
        source = db.session.get(Department, result['department_ids'][0])
        collection = BackgroundTask.query.filter_by(kind='collect').one()
        collection.state = 'done'; collection.finished_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        queue_sources([source], manual=True)
        db.session.refresh(collection)
        self.assertFalse(collection.payload['ai_assist'])
        with patch('backend.scraper.engine.scrape_department', return_value=(0, 3)), \
             patch('backend.services.student_information.queue_source_assessment') as assess_source, \
             patch('backend.services.student_information.queue_listing_assessment') as assess_listing:
            collect_source(source)
        assess_source.assert_called_once_with(source.id, ai_assist=False)
        assess_listing.assert_called_once_with(source.id, ai_assist=False)

    def test_late_navigation_result_resumes_only_the_current_generation(self):
        from backend.services.source_onboarding import onboard_page
        from backend.worker import _schedule_due
        onboard_page({'school_id': self.school_id, 'url': URL, 'ai_assist': False}, fetcher=self.fixture.fetch)
        parent = tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'ai_assist': False})
        parent.state = 'done'; parent.finished_at = datetime.utcnow() - timedelta(hours=7)
        child = tasks.enqueue('navigation_review', 'late', {'school_id': self.school_id,
            'parent_task_id': parent.id, 'parent_generation': parent.generation - 1})
        child.state = 'done'; child.result = {'deferred_pages': 12}
        db.session.commit()
        parent_id, child_id = parent.id, child.id
        _schedule_due()
        parent = db.session.get(BackgroundTask, parent_id)
        self.assertEqual(parent.state, 'done')
        child = db.session.get(BackgroundTask, child_id)
        child.payload = dict(child.payload, parent_generation=parent.generation)
        db.session.commit()
        _schedule_due()
        self.assertEqual(db.session.get(BackgroundTask, parent_id).state, 'pending')


if __name__ == '__main__':
    unittest.main()
