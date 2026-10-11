"""Mixed directory publishing and unknown navigation preserve durable work."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.services import tasks
from tests import test_direct_onboarding as fixture_module
from tests import test_ai_primary_navigation as navigation_module

HTML, URL = fixture_module.HTML, fixture_module.URL


class SourceNavigationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def directory_inventory(self, html, *, publications=True):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.structure import publication_evidence
        store = Inventory(Path(self.fixture.temp.name) / 'mixed-directory.db')
        key = store.ensure_site('测试大学', 'https://example.edu.cn/')
        store.finish(key, 'https://example.edu.cn/', state='reference_only')
        store.enqueue(key, URL, '专业设置', 'directory', 1, [], 'school_domain')
        feed = publication_evidence(html, URL) if publications else None
        store.finish(key, URL, state='fetched', html=html, final_url=URL,
                     title='专业设置 - 测试大学', feed_json=json.dumps(feed) if feed else None)
        return store, key

    def test_mixed_directory_with_real_publications_enters_full_body_validation(self):
        from backend.services.source_onboarding import queue_columns, onboard_page
        from backend.database.models import Announcement, Department, BackgroundTask
        store, key = self.directory_inventory(HTML)
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Valid observed rules need no AI')):
            queued = queue_columns(self.fixture.school_id, store, key)
            self.assertEqual(len(queued['onboarding_ids']), 1)
            job = db.session.get(BackgroundTask, queued['onboarding_ids'][0])
            result = onboard_page(job.payload, fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(len([read for read in self.fixture.reads if read[1] == 'article']), 3)

    def test_plain_institution_roster_does_not_queue_a_paid_column_decision(self):
        from backend.services.source_onboarding import queue_columns
        store, key = self.directory_inventory('<h1>教学机构</h1><a href="/school/">理学院</a>', publications=False)
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('A plain roster is not publication work')):
            self.assertEqual(queue_columns(self.fixture.school_id, store, key)['onboarding_ids'], [])

    def test_mixed_directory_can_repair_an_existing_rule_without_erasing_source(self):
        from backend.services.source_onboarding import queue_columns
        from backend.database.models import Department
        source = Department(school_id=self.fixture.school_id, name='通知公告', kind='column',
                            list_url=URL, list_selector='#notices li')
        db.session.add(source)
        db.session.commit()
        original_id = source.id
        store, key = self.directory_inventory(HTML.replace('id="notices"', 'id="changed"'), publications=False)
        queued = queue_columns(self.fixture.school_id, store, key)
        self.assertEqual(len(queued['onboarding_ids']), 1)
        self.assertEqual(Department.query.one().id, original_id)
        self.assertEqual(Department.query.one().list_selector, '#notices li')

    def navigation_job(self):
        html = '<nav>' + ''.join(f'<a href="/space/{i}">成长空间{i}</a>' for i in range(9)) + '</nav>'
        return navigation_module.NavigationPersistenceTests.navigation_job(self, html, 'unknown-paid')

    def unknown_navigation(self, *args, **kwargs):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        skill = load_skill('student-information', 'navigation')
        db.session.add(AIExecution(execution_id=args[4], purpose='directory', profile_id=None,
            config_version='1', provider='deepseek', model='fixture', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': args[2], 'repair_feedback': None}), mode='navigation',
            status='uncertain', request_sent=True, active_released=True, reserved_tokens=10000,
            usage={'known': False}, error_code='connection_lost'))
        db.session.commit()
        return {'status': 'uncertain', 'error_code': 'connection_lost', 'usage': {'known': False}}

    def test_unknown_navigation_waits_and_reentry_keeps_original_request_without_model_call(self):
        from backend.scraper.discovery.ai_navigation import process_navigation
        _, _, job = self.navigation_job()
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=self.unknown_navigation) as model:
            with self.assertRaises(tasks.TaskDeferred) as paused:
                process_navigation(handle['payload'])
            self.assertEqual((paused.exception.state, paused.exception.phase), ('waiting', 'ai_result'))
            before = copy.deepcopy(handle['checkpoint'])
            with self.assertRaises(tasks.TaskDeferred):
                process_navigation(handle['payload'])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(handle['checkpoint'], before)
            self.assertEqual(handle['checkpoint']['route_cursor'], 0)
            self.assertFalse((handle['checkpoint'].get('navigation_progress') or {}).get('decision_ids'))

    def test_old_done_unknown_navigation_recovers_same_generation_and_preserves_paid_plan(self):
        from backend.scraper.discovery.ai_navigation import process_navigation
        from backend.services.source_onboarding import resume_rule_discovery
        _, _, job = self.navigation_job()
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=self.unknown_navigation):
            try:
                process_navigation(handle['payload'])
            except tasks.TaskDeferred:
                pass
        # Simulate the old caller returning needs_recovery and marking it done.
        tasks.finish(handle, {'status': 'needs_recovery', 'error_code': 'connection_lost'})
        before, generation = copy.deepcopy(job.checkpoint), job.generation
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Upgrade must not retry an unknown paid call')):
            resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase, job.error_code), ('waiting', 'ai_result', 'ai_result_unknown'))
        self.assertEqual(job.generation, generation)
        self.assertEqual(job.checkpoint, before)
        self.assertIsNone(tasks.claim(capabilities=['directory']))

    def test_provider_connection_pause_preserves_navigation_and_exact_binding(self):
        from backend.ai.configuration import AIConfigError
        from backend.scraper.discovery.ai_navigation import process_navigation
        _, _, job = self.navigation_job()
        handle = tasks.claim(capabilities=['directory'])
        binding = {'id': None, 'version': 1, 'provider': 'deepseek', 'model': 'fixture'}
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value=binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('连接暂停', 'provider_connection_paused')):
            with self.assertRaises(tasks.TaskDeferred) as paused:
                process_navigation(handle['payload'])
        self.assertEqual((paused.exception.state, paused.exception.phase), ('waiting', 'ai_connection'))
        self.assertEqual(paused.exception.error_code, 'provider_connection_paused')
        self.assertEqual(paused.exception.checkpoint['ai_connection_binding'], binding)
        self.assertEqual(paused.exception.checkpoint['route_cursor'], 0)

    def test_connection_resume_unknown_actual_id_stays_blocked_including_legacy_recovery(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        from backend.ai.connection_recovery import resumed_execution_id
        from backend.scraper.discovery.ai_navigation import process_navigation
        from backend.services.source_onboarding import resume_rule_discovery
        _, _, job = self.navigation_job()
        job.checkpoint = {'ai_connection_resume': 1}
        db.session.commit()
        handle = tasks.claim(capabilities=['directory'])
        skill = load_skill('student-information', 'navigation')
        def resumed_unknown(*args, **kwargs):
            db.session.add(AIExecution(execution_id=args[4], purpose='directory', profile_id=None,
                config_version='1', provider='deepseek', model='fixture', skill_id=skill.id,
                skill_version=skill.version, skill_digest=skill.resource_digest,
                input_digest=digest({'evidence': args[2], 'repair_feedback': None}), mode='navigation',
                status='failed', request_sent=False, active_released=True, usage={'known': True, 'total_tokens': 0},
                error_code='provider_connection_paused'))
            db.session.commit()
            actual_id = resumed_execution_id(args[4])
            self.assertNotEqual(actual_id, args[4])
            return self.unknown_navigation(*args[:4], actual_id, **kwargs)
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=resumed_unknown) as model:
            with self.assertRaises(tasks.TaskDeferred):
                process_navigation(handle['payload'])
            before = copy.deepcopy(handle['checkpoint'])
            with self.assertRaises(tasks.TaskDeferred):
                process_navigation(handle['payload'])
            self.assertEqual(model.call_count, 1)
        tasks.finish(handle, {'status': 'needs_recovery', 'error_code': 'connection_lost'})
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase), ('waiting', 'ai_result'))
        self.assertEqual(job.checkpoint, before)
        rows = AIExecution.query.all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row.request_sent for row in rows), 1)

    def test_old_usable_dynamic_snapshot_is_rendered_before_ai_or_done_checkpoint(self):
        import hashlib
        from backend.services.source_governance import _snapshot
        from backend.services.source_onboarding import onboard_page
        from backend.database.models import Announcement
        shell = ('<title>招生信息 - 测试大学</title><script>$.ajax({url:"/public/list"});</script><ul>'
                 '<li><a href="/">关于开展本科生招生工作的通知</a><span>2026-10-01</span></li>'
                 '<li><a href="#">关于开展招生咨询工作的通知</a><span>2026-10-02</span></li></ul>')
        reference = _snapshot(shell, URL, role='list')
        job = tasks.enqueue('onboard', 'old-dynamic', {'school_id': self.fixture.school_id, 'url': URL})
        job.checkpoint = {'onboarding_page': reference, 'column_validation': {
            'page_hash': hashlib.sha256((URL + '\\0' + shell).encode()).hexdigest(),
            'stage': 'done', 'result': {'state': 'unsupported', 'department_ids': []}}}
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('An incomplete shell must not reach AI')):
            result = onboard_page(job.payload, fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(self.fixture.reads.count((URL, 'directory')), 1)
        self.assertNotEqual(handle['checkpoint']['onboarding_page']['hash'], reference['hash'])


if __name__ == '__main__':
    unittest.main()
