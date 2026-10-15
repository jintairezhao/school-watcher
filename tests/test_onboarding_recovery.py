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
             patch('backend.services.source_onboarding._read',
                   side_effect=lambda url, purpose, **kwargs: self.fixture.fetch(url, purpose)), \
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

    def test_access_recovery_respects_longer_site_wait_and_reuses_observed_rules(self):
        from backend.services.source_onboarding import onboard_page, retry_unconnected_columns
        from backend.scraper.acquisition import FetchFailure, FetchResult
        from tests.test_direct_onboarding import URL, HTML, ai_column
        job = tasks.enqueue('onboard', 'restricted', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        def restricted(address, purpose):
            if address == URL:
                return HTML
            raise FetchFailure(FetchResult(address, status=403, outcome='denied', error_code='http_403', retry_after=30000))
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.recognize_column',
                side_effect=AssertionError('An access failure must not buy rule repair')):
            result = onboard_page(handle['payload'], fetcher=restricted)
        tasks.finish(handle, result)
        db.session.refresh(job)
        job.finished_at = datetime.utcnow() - timedelta(hours=7)
        db.session.commit()
        self.assertEqual(retry_unconnected_columns(self.fixture.school_id, 'background:1', automatic=True), [])
        self.assertEqual(retry_unconnected_columns(self.fixture.school_id, 'manual:1', automatic=False), [])
        job.result = dict(job.result, recovery_not_before=(datetime.utcnow() - timedelta(seconds=1)).isoformat(),
                          website_not_before=(datetime.utcnow() - timedelta(seconds=1)).isoformat())
        db.session.commit()
        self.assertEqual(retry_unconnected_columns(self.fixture.school_id, 'background:2', automatic=True), [job.id])
        db.session.refresh(job)
        self.assertEqual(job.payload['observed_columns'], result['recovery_rules'])
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
                patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('Known rules recover with no new model request')):
            recovered = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        self.assertEqual(recovered['state'], 'connected')
        self.assertEqual(Department.query.count(), 1)

    def test_network_resume_executes_paid_columns_before_any_new_model_request(self):
        from backend.services.source_onboarding import onboard_page
        from backend.scraper.acquisition import FetchFailure, FetchResult
        from tests.test_direct_onboarding import URL, HTML, ai_column
        job = tasks.enqueue('onboard', 'paid-trial', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        def unavailable(address, purpose):
            if address == URL:
                return HTML
            raise FetchFailure(FetchResult(address, outcome='network_error', error_code='http_timeout'))
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
                patch('backend.services.source_onboarding.recognize_column', return_value={
                    'status': 'ready', 'columns': [ai_column(content_selector='')], 'decision_ids': ['actual-paid-decision']}) as model:
            with self.assertRaises(FetchFailure) as raised:
                onboard_page(handle['payload'], fetcher=unavailable)
        self.assertEqual(model.call_count, 1)
        tasks.finish(handle, error=raised.exception)
        db.session.refresh(job)
        self.assertEqual(job.checkpoint['column_validation']['stage'], 'trial')
        self.assertEqual(job.checkpoint['column_validation']['trial_columns'][0]['content_selector'], '')
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'], ['actual-paid-decision'])
        job.available_at = datetime.utcnow(); db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.publication_lists',
                side_effect=AssertionError('An existing trial must not be replaced by another profile')), \
                patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('Already paid for these rules')):
            result = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.count(), 1)

    def test_upgrade_executes_legacy_model_columns_instead_of_failing_rule_profile(self):
        import hashlib
        from backend.services.source_onboarding import onboard_page
        from backend.services.source_validation import VALIDATION_VERSION
        from tests.test_direct_onboarding import URL, HTML, ai_column
        job = tasks.enqueue('onboard', 'legacy-paid-trial', {'school_id': self.fixture.school_id, 'url': URL})
        job.checkpoint = {'column_regions': {'columns': [ai_column(content_selector='')],
            'decision_ids': ['old-paid-observation'], 'cursor': 1}, 'column_validation': {
            'page_hash': hashlib.sha256((URL + '\\0' + HTML).encode()).hexdigest(),
            'validation_version': 3, 'stage': 'done', 'completed': [],
            'department_ids': [], 'new_count': 0,
            'failures': [{'issue': {'codes': ['article_sample_mismatch']}}],
            'result': {'state': 'unsupported'}, 'progress': {'stopped': True}}}
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.publication_lists',
                side_effect=AssertionError('The old model observation is already available')), \
                patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('A program fix must not repeat a paid request')):
            result = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.one().content_selector, '')

    def test_legacy_partial_material_resumes_unread_region_on_second_claim(self):
        self._assert_partial_material_resumes()

    def test_v5_validation_replay_resumes_the_original_unread_region_without_repair_calls(self):
        self._assert_partial_material_resumes(version=5)

    def test_existing_rules_handoff_retains_partial_material_on_second_claim(self):
        self._assert_partial_material_resumes(already_handed_off=True)

    def _assert_partial_material_resumes(self, *, already_handed_off=False, version=3):
        import hashlib
        from bs4 import BeautifulSoup
        from backend.ai.skill_loader import digest, load_skill
        from backend.services.source_onboarding import onboard_page
        from backend.services.source_validation import VALIDATION_VERSION
        from backend.services.discovery_policy import progress_state
        from tests.test_direct_onboarding import URL, HTML, TITLES, ai_column
        other = '<section id="other"><h2>学术活动</h2><ul>' + ''.join(
            f'<li><a href="/other/{i}.htm">{title}</a><span>2026-10-01</span></li>'
            for i, title in enumerate(TITLES)) + '</ul></section>'
        html = HTML.replace('</body>', other + '</body>')
        document = BeautifulSoup(html, 'lxml')
        regions = [{'school_id': self.fixture.school_id,
            'candidates': [{'candidate_id': 'page', 'url': URL}],
            'evidence': [{'evidence_id': 'region-' + str(i), 'url': URL,
                'html': str(document.select_one(selector))}]}
            for i, selector in enumerate(('#notices', '#other'))]
        binding = {'id': 77, 'version': 1}
        skill = load_skill('university-source-onboarding', 'column')
        paid_id = 'column-region:' + digest([skill.resource_digest, regions[0], binding['id'], binding['version']])
        first = ai_column(content_selector='')
        second = ai_column(name='学术活动', content_selector='', list_selector='#other li',
                           container_selector='#other', heading_selector='#other h2')
        job = tasks.enqueue('onboard', 'legacy-partial-material', {'school_id': self.fixture.school_id, 'url': URL})
        job.checkpoint = {'column_regions': {'hash': digest(regions), 'columns': [first],
            'decision_ids': [paid_id], 'cursor': 1, 'unresolved': []}, 'column_validation': {
            'page_hash': hashlib.sha256((URL + '\\0' + html).encode()).hexdigest(),
            'validation_version': VALIDATION_VERSION if already_handed_off else version,
            'stage': 'rules' if already_handed_off else 'done', 'completed': [],
            'department_ids': [], 'new_count': 0, 'material_pending': True, 'material_feedback': [],
            'failures': [{'issue': {'codes': ['article_sample_mismatch']}}],
            'result': {'state': 'unsupported'}, 'progress': progress_state() if already_handed_off else {'stopped': True}}}
        if already_handed_off:
            checkpoint = job.checkpoint
            checkpoint['column_validation'].update(trial_columns=[first], trial_origin_stage='rules',
                trial_decision_ids=[paid_id], trial_request_key='', failures=[])
            job.checkpoint = checkpoint
        db.session.commit()
        def fetch(address, purpose):
            return html if address == URL else self.fixture.fetch(address, purpose)
        def decide(skill_name, mode, evidence, slot, execution_id, **kwargs):
            self.assertEqual(evidence, regions[1])
            self.assertNotEqual(execution_id, paid_id)
            self.assertEqual(tasks.current_execution()['checkpoint']['column_regions']['cursor'], 1)
            return {'status': 'succeeded', 'output': {'status': 'ready', 'columns': [second]}}
        with patch('backend.ai.configuration.get_model_binding', return_value=binding), \
                patch('backend.scraper.discovery.column_regions.materials', return_value=regions), \
                patch('backend.ai.runtime.run_skill', side_effect=decide) as model:
            handle = tasks.claim(capabilities=['http'])
            with tasks.execution_scope(handle), self.assertRaises(tasks.TaskDeferred) as deferred:
                onboard_page(handle['payload'], fetcher=fetch)
            tasks.handoff(handle, deferred.exception)
            self.assertEqual(Department.query.count(), 1)
            self.assertEqual(Department.query.one().name, '通知公告')
            self.assertEqual(model.call_count, 0)
            db.session.refresh(job)
            self.assertEqual(job.checkpoint['column_regions']['cursor'], 1)
            self.assertEqual(job.generation, 1)
            handle = tasks.claim(capabilities=['directory'])
            self.assertEqual(handle['id'], job.id)
            with tasks.execution_scope(handle):
                result = onboard_page(handle['payload'], fetcher=fetch)
            tasks.finish(handle, result)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual({source.name for source in Department.query.all()}, {'通知公告', '学术活动'})
        self.assertEqual(model.call_count, 1)
        db.session.refresh(job)
        self.assertEqual(job.generation, 1)
        self.assertEqual(job.checkpoint['column_regions']['cursor'], 2)
        self.assertIn(paid_id, job.checkpoint['column_regions']['decision_ids'])

    def test_single_page_repair_preserves_source_id_and_latest_ai_opt_out(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL, HTML
        initial = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False}, fetcher=self.fixture.fetch)
        source = db.session.get(Department, initial['department_ids'][0])
        source.name, source.list_selector = '旧栏目名', '#old li'
        db.session.commit()
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
