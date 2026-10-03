"""Automatic value hints are bounded, evidence-based and never fetch article bodies."""
from datetime import datetime
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department
from backend.database.student_information_models import StudentAssessment
from backend.ai.configuration import AIConfigError
from backend.ai.skill_loader import canonical, load_skill, validate_output, SkillValidationError
from backend.services import student_information as value


class AutomaticStudentValueTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.source = Department(school_id=self.fixture.school_id, name='成长资源',
            list_url='https://example.edu.cn/resources/', list_selector='li')
        db.session.add(self.source)
        db.session.flush()
        self.articles = [Announcement(school_id=self.source.school_id, department_id=self.source.id,
            title=f'本科生科研申请材料说明 {i}', url=f'https://example.edu.cn/info/{i}',
            published_at=datetime(2026, 9, i + 1)) for i in range(12)]
        db.session.add_all(self.articles)
        db.session.commit()
        self.binding = {'id': 1, 'version': 1}
        self.ai_config = patch('backend.ai.configuration.get_model_binding', side_effect=lambda _: dict(self.binding))
        self.ai_config.start()

    def tearDown(self):
        self.ai_config.stop()
        self.fixture.tearDown()

    def response(self, *args, **kwargs):
        evidence = args[2]
        self.assertLessEqual(len(canonical(evidence).encode()), 24 * 1024)
        rows = []
        for candidate in evidence['candidates']:
            proof = next(p for p in evidence['evidence'] if p['candidate_id'] == candidate['candidate_id'])
            quote = proof['text'].splitlines()[0][-60:]
            rows.append({'candidate_id': candidate['candidate_id'], 'role': 'reference',
                'value': 'relevant', 'historical': 'possible', 'reason': '有助于准备科研申请',
                'facts': [{'kind': 'relevance', 'text': '科研申请材料',
                    'quote': quote, 'evidence_id': proof['evidence_id']}]})
        output = {'results': rows}
        validate_output(load_skill('student-information', 'assess'), output, evidence)
        return {'status': 'succeeded', 'output': output}

    def test_source_runs_automatically_with_six_samples_in_one_call(self):
        job = value.queue_source_assessment(self.source.id)
        self.assertIsNotNone(job)
        self.assertIsNone(job.payload.get('requested_by'))
        with patch('backend.ai.runtime.run_skill', side_effect=self.response) as ai:
            self.assertEqual(value.process(job.payload)['state'], 'ready')
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(len(ai.call_args.args[2]['evidence']), 6)
        self.assertIsNone(value.queue_source_assessment(self.source.id))

    def test_disabled_opt_out_and_legacy_jobs_do_not_call_ai(self):
        self.assertIsNone(value.queue_source_assessment(self.source.id, ai_assist=False))
        self.assertIsNone(value.queue_listing_assessment(self.source.id, ai_assist=False))
        with patch('backend.ai.configuration.get_model_binding', side_effect=AIConfigError('disabled')):
            self.assertIsNone(value.queue_source_assessment(self.source.id))
            self.assertIsNone(value.queue_listing_assessment(self.source.id))
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(value.process({'subject_kind': 'source', 'subject_id': self.source.id})['state'], 'skipped')
            self.assertEqual(value.process({'subject_kind': 'article', 'subject_id': self.articles[0].id})['state'], 'skipped')
        ai.assert_not_called()
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_listing_is_one_bounded_call_shared_by_repeated_queue_and_read(self):
        job = value.queue_listing_assessment(self.source.id)
        duplicate = value.queue_listing_assessment(self.source.id)
        self.assertEqual(job.id, duplicate.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response) as ai:
            self.assertEqual(value.process(job.payload)['state'], 'ready')
            views = value.listing_views(self.articles)
            self.assertEqual(value.listing_views(self.articles), views)
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(len(ai.call_args.args[2]['candidates']), 8)
        self.assertEqual(len(views), 8)
        self.assertNotIn(self.articles[0].id, views)
        self.assertTrue(all(v['value'] == 'relevant' for v in views.values()))
        self.assertTrue(all(v['eligibility'] == 'unknown' for v in views.values()))
        self.assertTrue(all(not a.content_text for a in self.articles))
        second = value.queue_listing_assessment(self.source.id)
        self.assertNotEqual(second.id, job.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response) as ai:
            self.assertEqual(value.process(second.payload)['state'], 'ready')
        self.assertEqual(len(ai.call_args.args[2]['candidates']), 4)
        self.assertIsNone(value.queue_listing_assessment(self.source.id))

    def test_listing_hints_invalidate_on_title_source_and_model_change(self):
        job = value.queue_listing_assessment(self.source.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response):
            value.process(job.payload)
        latest = self.articles[-1]
        self.assertIn(latest.id, value.listing_views(self.articles))
        latest.title = '标题已经更正'
        db.session.commit()
        self.assertNotIn(latest.id, value.listing_views(self.articles))
        self.source.name = '办事动态'
        db.session.commit()
        self.assertEqual(value.listing_views(self.articles), {})
        self.source.name = '成长资源'
        db.session.commit()
        self.binding['version'] = 2
        self.assertEqual(value.listing_views(self.articles), {})
        newer = value.queue_listing_assessment(self.source.id)
        self.assertNotEqual(newer.id, job.id)

    def test_changed_material_or_model_cannot_publish_from_a_queued_job(self):
        job = value.queue_listing_assessment(self.source.id)
        self.articles[-1].title = '更正后的标题'
        db.session.commit()
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(value.process(job.payload)['state'], 'stale')
        ai.assert_not_called()
        job = value.queue_source_assessment(self.source.id)
        self.binding['version'] = 2
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(value.process(job.payload)['state'], 'stale')
        ai.assert_not_called()

    def test_changed_material_during_network_call_is_not_published(self):
        job = value.queue_listing_assessment(self.source.id)
        def respond(*args, **kwargs):
            response = self.response(*args, **kwargs)
            self.articles[-1].title = '官网更新后的标题'
            db.session.commit()
            return response
        with patch('backend.ai.runtime.run_skill', side_effect=respond):
            self.assertEqual(value.process(job.payload)['state'], 'stale')
        self.assertEqual(StudentAssessment.query.count(), 0)

    def test_listing_cannot_quote_unseen_text_or_promote_publication_to_deadline(self):
        prepared = value.material('listing', self.articles[-1].id)
        output = self.response(None, None, prepared[1])['output']
        output['results'][0]['facts'][0]['quote'] = '本科生保证保研'
        with self.assertRaises(SkillValidationError):
            validate_output(load_skill('student-information', 'assess'), output, prepared[1])
        job = value.queue_listing_assessment(self.source.id)
        def unsafe(*args, **kwargs):
            response = self.response(*args, **kwargs)
            for row in response['output']['results']:
                row['facts'][0]['kind'] = 'deadline'
            return response
        with patch('backend.ai.runtime.run_skill', side_effect=unsafe):
            value.process(job.payload)
        self.assertTrue(all(v['facts'] == [] for v in value.listing_views(self.articles).values()))

    def test_automatic_backfill_stops_after_three_calls_and_twenty_four_notices(self):
        from backend.services import tasks
        for i in range(30):
            db.session.add(Announcement(school_id=self.source.school_id, department_id=self.source.id,
                title=f'本科生交流项目说明 {i}', url=f'https://example.edu.cn/exchange/{i}',
                published_at=datetime(2026, 10, i + 1)))
        db.session.commit()
        value.queue_listing_assessment(self.source.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response) as ai:
            for _ in range(5):
                handle = tasks.claim(capabilities=['directory'])
                if handle is None:
                    break
                with tasks.execution_scope(handle):
                    result = value.process(handle['payload'])
                tasks.finish(handle, result)
        self.assertEqual(ai.call_count, 3)
        self.assertEqual(StudentAssessment.query.count(), 24)
        self.assertEqual(BackgroundTask.query.filter_by(state='pending').count(), 0)
        self.assertIsNone(value.queue_listing_assessment(self.source.id))

    def test_unsubscribe_or_disable_after_queue_stops_automatic_work(self):
        from backend.database.models import School
        jobs = [value.queue_source_assessment(self.source.id), value.queue_listing_assessment(self.source.id)]
        school = db.session.get(School, self.source.school_id)
        school.subscriber_count = 0
        db.session.commit()
        with patch('backend.ai.runtime.run_skill') as ai:
            for job in jobs:
                self.assertEqual(value.process(job.payload)['reason'], 'school_inactive')
        ai.assert_not_called()
        self.assertIsNone(value.queue_source_assessment(self.source.id))
        self.assertIsNone(value.queue_listing_assessment(self.source.id))

    def test_source_hints_and_polling_reject_outdated_evidence(self):
        job = value.queue_source_assessment(self.source.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response):
            value.process(job.payload)
        row = db.session.get(StudentAssessment, 'source:' + str(self.source.id))
        row.result = dict(row.result, value='low', historical='low')
        db.session.commit()
        self.assertEqual(value.collection_policy([self.source])[self.source.id], (3, 4))
        self.assertEqual(value.source_view(self.source.id), value.VALUE_LABELS['low'])
        self.articles[-1].title = '本科生新的机会'
        db.session.commit()
        self.assertEqual(value.collection_policy([self.source])[self.source.id], (2, 1))
        self.assertEqual(value.source_view(self.source.id), '按官网栏目接收')

    def test_foreign_school_source_links_do_not_poison_a_title_batch(self):
        from backend.database.models import AnnouncementSource, School
        other = School(name='另一个学校', url='https://other.edu.cn/', subscriber_count=1)
        db.session.add(other)
        db.session.flush()
        source = Department(school_id=other.id, name='异校栏目', list_url=other.url, list_selector='li')
        db.session.add(source)
        db.session.flush()
        ann = Announcement(school_id=other.id, department_id=source.id, title='另一所学校的信息',
                           url=other.url + '1', published_at=datetime(2026, 10, 1))
        db.session.add(ann)
        db.session.flush()
        db.session.add(AnnouncementSource(announcement_id=ann.id, department_id=self.source.id))
        db.session.commit()
        job = value.queue_listing_assessment(self.source.id)
        self.assertNotIn(ann.id, job.payload['announcement_ids'])
        with patch('backend.ai.runtime.run_skill', side_effect=self.response):
            self.assertEqual(value.process(job.payload)['state'], 'ready')

    def test_explicit_source_scope_and_school_ai_opt_out_are_rechecked(self):
        from backend.database.models import DepartmentDirectoryEntry, Subscription
        from backend.services import tasks
        job = value.queue_listing_assessment(self.source.id)
        sub = Subscription.query.one()
        sub.department_ids = []
        db.session.commit()
        self.assertIsNone(value.queue_source_assessment(self.source.id))
        self.assertIsNone(value.queue_listing_assessment(self.source.id))
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(value.process(job.payload)['reason'], 'source_not_selected')
        ai.assert_not_called()
        parent = Department(school_id=self.source.school_id, name='学生服务', kind='unit')
        db.session.add(parent)
        db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=self.source.id, position=0))
        sub.department_ids = [parent.id]
        db.session.commit()
        self.assertIsNotNone(value.queue_source_assessment(self.source.id))
        discovery = tasks.enqueue('discover', self.source.school_id,
            {'school_id': self.source.school_id, 'ai_assist': False})
        self.assertIsNone(value.queue_listing_assessment(self.source.id))
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(value.process(job.payload)['reason'], 'source_not_selected')
        ai.assert_not_called()
        discovery.payload = dict(discovery.payload, ai_assist=True)
        db.session.commit()
        self.assertIsNotNone(value.queue_listing_assessment(self.source.id))

    def test_skipped_automatic_jobs_reopen_after_resubscription_but_uncertain_calls_do_not(self):
        from backend.database.models import School
        school = db.session.get(School, self.source.school_id)
        for queue in (value.queue_source_assessment, value.queue_listing_assessment):
            job = queue(self.source.id)
            school.subscriber_count = 0
            db.session.commit()
            result = value.process(job.payload)
            self.assertEqual(result['state'], 'skipped')
            job.state, job.result = 'done', result
            school.subscriber_count = 1
            db.session.commit()
            reopened = queue(self.source.id)
            self.assertEqual(reopened.id, job.id)
            self.assertEqual(reopened.generation, 2)
            reopened.state = 'done'
            reopened.result = {'state': 'unknown', 'error_code': 'network_result_unknown'}
            db.session.commit()
            self.assertEqual(queue(self.source.id).generation, 2)

    def test_source_label_uses_evidence_even_when_named_notice(self):
        self.source.name = '通知公告'
        db.session.commit()
        job = value.queue_source_assessment(self.source.id)
        with patch('backend.ai.runtime.run_skill', side_effect=self.response):
            value.process(job.payload)
        row = db.session.get(StudentAssessment, 'source:' + str(self.source.id))
        row.result = dict(row.result, value='mixed')
        db.session.commit()
        self.assertEqual(value.source_view(self.source.id), value.VALUE_LABELS['mixed'])


if __name__ == '__main__':
    unittest.main()
