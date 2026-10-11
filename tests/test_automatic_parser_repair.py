"""Broken list rules recover through validated onboarding, without a review queue."""
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, Subscription
from backend.database.source_governance_models import SourceConfigVersion, SourceProposal
from backend.scraper.acquisition import FetchFailure, FetchResult, FetchedHTML
from backend.scraper.selector_monitor import evaluate_and_repair, queue_parser_repair
from backend.services.source_collection import collect_source
from backend.services.source_onboarding import onboard_page


class AutomaticParserRepairTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests, HTML, URL
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.url, self.html = URL, HTML
        self.changed = HTML.replace('id="notices"', 'id="new-notices"')
        result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False},
                              fetcher=self.fixture.fetch)
        self.source = db.session.get(Department, result['department_ids'][0])
        self.audit = patch('backend.scraper.selector_monitor.AUDIT_PATH', Path(self.fixture.temp.name) / 'audit.json')
        self.audit.start()

    def tearDown(self):
        self.audit.stop()
        self.fixture.tearDown()

    def test_real_collection_rule_break_repairs_same_source_without_proposals(self):
        original_id = self.source.id
        original_selector = self.source.list_selector
        announcement_ids = {a.id for a in Announcement.query.all()}
        with patch('backend.scraper.engine._fetch_html', return_value=self.changed), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No AI for a recognized list')):
            result = collect_source(self.source)
            self.assertEqual(result['state'], 'repairing')
            self.assertTrue(result['partial'])
            self.assertEqual(self.source.list_selector, original_selector)
            job = db.session.get(BackgroundTask, result['repair_task_id'])
            self.assertTrue(job.payload['revalidate'])
            self.assertEqual(job.payload['repair_department_id'], original_id)
            self.assertEqual(SourceProposal.query.count(), 0)
            self.assertEqual(BackgroundTask.query.filter_by(kind='source_review').count(), 0)
            reads_before = len(self.fixture.reads)
            repaired = onboard_page(job.payload, fetcher=self.fixture.fetch)
        self.assertEqual(repaired['state'], 'connected')
        self.assertEqual(repaired['department_ids'], [original_id])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual({a.id for a in Announcement.query.all()}, announcement_ids)
        self.assertEqual(SourceConfigVersion.query.filter_by(department_id=original_id).count(), 2)
        self.assertNotEqual(self.source.list_selector, original_selector)
        self.assertNotIn((self.url, 'directory'), self.fixture.reads[reads_before:])

    def test_readiness_failure_also_queues_repair_before_engine_parser_runs(self):
        failure = FetchFailure(FetchResult(self.url, status=200, html=self.changed,
            outcome='needs_adapter', error_code='readiness_missing', message='旧规则已变化'))
        with patch('backend.scraper.engine._fetch_html', side_effect=failure):
            result = collect_source(self.source)
        self.assertEqual(result['state'], 'repairing')
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 1)

    def pending_material(self):
        # The HTTP observation saw the loader; the rendered DOM removed it.
        html = ('<section id="notices"><li><a href="#">新学期课程安排通知</a>'
                '<time>2026-09-23</time></li><li><a href="#">奖学金材料提交通知</a>'
                '<time>2026-09-24</time></li></section>')
        return FetchResult(self.url, status=200, html=html, transport='browser',
            outcome='needs_adapter', error_code='dynamic_material_incomplete',
            message='动态内容尚未加载完整', evidence=('observed_dynamic_request', 'placeholder_publication_links:2'))

    def test_default_collection_does_not_convert_incomplete_material_to_ai_repair(self):
        failure = FetchFailure(self.pending_material())
        original = self.source.list_selector
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=failure) as reader, \
             patch('backend.services.source_onboarding.recognize_column') as recognize, \
             patch('backend.ai.runtime.run_skill') as model:
            with self.assertRaises(FetchFailure) as caught:
                collect_source(self.source)
        self.assertIs(caught.exception, failure)
        self.assertEqual(reader.call_args.args[0].purpose, 'list')
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
        self.assertEqual(self.source.list_selector, original)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        recognize.assert_not_called()
        model.assert_not_called()

    def test_shared_repair_entry_preserves_typed_network_material_and_access_failures(self):
        from dataclasses import replace
        from backend.scraper.selector_monitor import _classify_page
        partial = self.pending_material()
        samples = [partial,
            replace(partial, status=502, html=self.changed, outcome='network_error', error_code='http_502'),
            replace(partial, status=200, html=self.changed, outcome='network_error', error_code='browser_timeout'),
            replace(partial, status=403, html=self.changed, outcome='denied', error_code='http_403'),
            replace(partial, status=200, html=self.changed, outcome='needs_manual', error_code='human_verification'),
            replace(partial, status=200, html=self.changed, error_code='response_too_large')]
        with patch('backend.ai.runtime.run_skill') as model:
            for raw in samples:
                with self.subTest(error_code=raw.error_code):
                    html = FetchedHTML(raw)
                    classified = _classify_page(html, self.url)
                    self.assertEqual((classified.outcome, classified.error_code), (raw.outcome, raw.error_code))
                    self.assertIsNone(queue_parser_repair(self.source, html))
                    self.assertNotEqual(evaluate_and_repair(self.source, html)['action'], 'repairing')
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
        model.assert_not_called()

    def test_snapshot_retains_failed_acquisition_type_but_plain_html_stays_compatible(self):
        from backend.services.source_governance import _snapshot, read_snapshot
        raw = self.pending_material()
        reference = _snapshot(FetchedHTML(raw), self.url)
        self.assertEqual(reference['outcome'], raw.outcome)
        self.assertEqual(reference['error_code'], raw.error_code)
        self.assertEqual(reference['evidence'], list(raw.evidence))
        self.assertEqual(reference['status'], raw.status)
        self.assertEqual(reference['transport'], raw.transport)
        self.assertEqual(read_snapshot(reference), raw.html)
        self.assertEqual(_snapshot(self.html, self.url)['outcome'], 'usable')
        # An old caller's default must not upgrade explicitly failed material.
        self.assertEqual(_snapshot(FetchedHTML(raw), self.url, outcome='usable')['outcome'], raw.outcome)

    def test_default_onboarding_rereads_typed_partial_snapshot_before_any_ai(self):
        from backend.services.source_governance import _snapshot
        raw = self.pending_material()
        reference = _snapshot(FetchedHTML(raw), self.url)
        payload = {'school_id': self.source.school_id, 'url': self.url,
                   'snapshot': reference, 'ai_assist': True}
        failure = FetchFailure(raw)
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=failure) as reader, \
             patch('backend.services.source_onboarding.recognize_column',
                   return_value={'status': 'needs_review', 'columns': [], 'reason': 'fixture'}) as recognize, \
             patch('backend.ai.runtime.run_skill') as model:
            with self.assertRaises(FetchFailure) as caught:
                onboard_page(payload)
        self.assertIs(caught.exception, failure)
        reader.assert_called_once()
        self.assertEqual(reader.call_args.args[0].purpose, 'directory')
        recognize.assert_not_called()
        model.assert_not_called()

    def test_partial_collection_keeps_original_task_schedule_and_recovers_without_repair(self):
        from dataclasses import replace
        from backend import worker
        from backend.database.models import UserRead, UserAnnouncementState
        from backend.services import tasks
        from backend.services.discovery_policy import get_policy
        from backend.services.inbox_refresh import queue_sources
        source_id, original_selector = self.source.id, self.source.list_selector
        original_ids = {a.id for a in Announcement.query.all()}
        ann_id, user_id = min(original_ids), Subscription.query.one().user_id
        db.session.add(UserRead(user_id=user_id, announcement_id=ann_id))
        db.session.add(UserAnnouncementState(user_id=user_id, announcement_id=ann_id, starred=True))
        db.session.commit()
        task = BackgroundTask.query.filter_by(kind='collect').one()
        task_id, generation = task.id, task.generation
        # The final allowed attempt must also respect a longer site cooldown.
        task.attempts = get_policy()['network_attempts'] - 1
        db.session.commit()
        retry_after = get_policy()['recovery_seconds'] + 60
        failure = FetchFailure(replace(self.pending_material(), retry_after=retry_after))
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'], task_id)
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=failure), \
             patch('backend.services.source_onboarding.recognize_column') as recognize, \
             patch('backend.ai.runtime.run_skill') as model:
            worker.execute(self.fixture.app, handle)
            db.session.expire_all()
            task = db.session.get(BackgroundTask, task_id)
            self.assertEqual((task.state, task.error_code, task.attempts),
                             ('failed', 'dynamic_material_incomplete', get_policy()['network_attempts']))
            self.assertGreaterEqual((task.next_run_at - task.checked_at).total_seconds(), retry_after)
            self.assertEqual(queue_sources([self.source]), [])
            self.assertEqual(task.generation, generation)
            self.assertEqual({a.id for a in Announcement.query.all()}, original_ids)
            self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
            # Emulate elapsed scheduled recovery, without shortening production policy.
            elapsed = timedelta(seconds=retry_after + 1)
            for field in ('checked_at', 'finished_at', 'updated_at', 'available_at', 'next_run_at'):
                setattr(task, field, getattr(task, field) - elapsed)
            self.source.last_scraped_at = task.checked_at
            db.session.commit()
            self.assertEqual(queue_sources([self.source]), [task_id])
            db.session.expire_all()
            task = db.session.get(BackgroundTask, task_id)
            self.assertEqual((task.state, task.generation), ('pending', generation + 1))
            handle = tasks.claim(capabilities=['http'])
            self.assertEqual(handle['id'], task_id)
            ready = self.html.replace('</ul>', '<li><a href="/info/new.htm">关于开展学生创新项目申报工作的通知</a>'
                                      '<span>2026-10-02</span></li></ul>')
            with patch('backend.scraper.acquisition.fetch_or_raise',
                       return_value=FetchResult(self.url, status=200, html=ready, outcome='usable')) as reader:
                worker.execute(self.fixture.app, handle)
            reader.assert_called_once()
            db.session.expire_all()
            task = db.session.get(BackgroundTask, task_id)
            self.assertEqual(task.state, 'done')
            self.assertEqual(task.result['new_count'], 1)
            self.assertEqual(Announcement.query.count(), len(original_ids) + 1)
            self.assertTrue(original_ids <= {a.id for a in Announcement.query.all()})
            self.assertEqual(Department.query.one().id, source_id)
            self.assertEqual(Department.query.one().list_selector, original_selector)
            self.assertEqual(SourceConfigVersion.query.count(), 1)
            self.assertEqual(UserRead.query.one().announcement_id, ann_id)
            self.assertTrue(UserAnnouncementState.query.one().starred)
            self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
            recognize.assert_not_called()
            model.assert_not_called()

    def test_dynamic_material_uses_existing_bounded_read_retries_without_ai_repair(self):
        from dataclasses import replace
        from backend import worker
        from backend.services import tasks
        from backend.services.discovery_policy import get_policy
        task_id = BackgroundTask.query.filter_by(kind='collect').one().id
        original_ids = {a.id for a in Announcement.query.all()}
        original_selector = self.source.list_selector
        self.assertEqual(get_policy()['network_attempts'], 3)
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=FetchFailure(self.pending_material())) as reader, \
             patch('backend.services.source_onboarding.recognize_column') as recognize, \
             patch('backend.ai.runtime.run_skill') as model, patch('random.uniform', return_value=1):
            for attempt, seconds in enumerate((30, 120, 21600), 1):
                task = db.session.get(BackgroundTask, task_id)
                task.available_at = datetime.utcnow() - timedelta(seconds=1)
                db.session.commit()
                handle = tasks.claim(capabilities=['http'])
                self.assertEqual(handle['id'], task_id)
                worker.execute(self.fixture.app, handle)
                db.session.expire_all()
                task = db.session.get(BackgroundTask, task_id)
                self.assertEqual(task.attempts, attempt)
                self.assertEqual(task.state, 'pending' if attempt < 3 else 'failed')
                self.assertEqual(task.error_code, 'dynamic_material_incomplete')
                self.assertAlmostEqual((task.available_at - task.checked_at).total_seconds(), seconds, delta=.1)
            self.assertEqual(reader.call_count, 3)
            self.assertEqual({a.id for a in Announcement.query.all()}, original_ids)
            self.assertEqual(self.source.list_selector, original_selector)
            self.assertEqual(SourceConfigVersion.query.count(), 1)
            self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
            recognize.assert_not_called()
            model.assert_not_called()
        for outcome, code in (('requires_render', 'dynamic_material_pending'),
                              ('needs_adapter', 'dynamic_material_incomplete')):
            failure = FetchFailure(replace(self.pending_material(), outcome=outcome, error_code=code))
            self.assertTrue(failure.retryable)
            self.assertEqual(failure.outcome, outcome)
        for code in ('readiness_missing', 'content_not_recognized', 'response_too_large'):
            self.assertFalse(FetchFailure(replace(self.pending_material(), error_code=code)).retryable)
        self.assertFalse(FetchFailure(replace(self.pending_material(), outcome='denied')).retryable)

    def test_governance_material_entries_reject_failed_material_but_retain_rule_evidence(self):
        from dataclasses import replace
        from backend.services import source_governance as governance
        raw = self.pending_material()
        with patch('backend.ai.runtime.run_skill') as model:
            for purpose in ('list', 'independent_list'):
                with self.subTest(purpose=purpose):
                    failure = FetchFailure(raw)
                    def read_partial(*_):
                        raise failure
                    with self.assertRaises(FetchFailure) as caught:
                        governance._fetch_snapshot(self.url, purpose, read_partial)
                    self.assertIs(caught.exception, failure)
                    with self.assertRaises(FetchFailure) as returned:
                        governance._fetch_snapshot(self.url, purpose, lambda *_: FetchedHTML(raw))
                    self.assertEqual(returned.exception.result, raw)
            with patch.object(governance, '_fetch', side_effect=FetchFailure(raw)), self.assertRaises(FetchFailure):
                governance._exploration_html(self.url)
            rule = replace(raw, html=self.changed, error_code='readiness_missing', evidence=('readiness_not_found',))
            with patch.object(governance, '_fetch', side_effect=FetchFailure(rule)):
                self.assertEqual(governance._exploration_html(self.url).result, rule)
            def read_rule(*_):
                raise FetchFailure(rule)
            reference = governance._fetch_snapshot(self.url, 'list', read_rule)
            self.assertEqual(reference['error_code'], rule.error_code)
            self.assertEqual(reference['evidence'], list(rule.evidence))
            model.assert_not_called()

    def test_repeated_failures_share_job_and_reopen_only_after_six_hours(self):
        job = queue_parser_repair(self.source, self.changed)
        duplicate = queue_parser_repair(self.source, self.changed)
        self.assertEqual(job.id, duplicate.id)
        self.assertEqual(job.generation, 1)
        job.state, job.result, job.finished_at = 'done', {'state': 'unsupported'}, datetime.utcnow()
        db.session.commit()
        self.assertEqual(queue_parser_repair(self.source, self.changed).generation, 1)
        job.finished_at = datetime.utcnow() - timedelta(hours=7)
        db.session.commit()
        fresh = self.changed.replace('new-notices', 'newer-notices')
        reopened = queue_parser_repair(self.source, fresh)
        self.assertEqual(reopened.id, job.id)
        self.assertEqual(reopened.generation, 2)
        self.assertEqual(reopened.state, 'pending')
        from backend.services.source_governance import read_snapshot
        self.assertEqual(read_snapshot(reopened.payload['snapshot']), fresh)

    def test_waf_denials_and_confirmed_empty_do_not_spend_ai_on_parser_repair(self):
        shell = '<html><title>安全验证</title><script>$_ts={};</script></html>'
        with patch('backend.ai.runtime.run_skill') as ai:
            self.assertEqual(evaluate_and_repair(self.source, shell)['action'], 'browser_needed')
            self.assertIsNone(queue_parser_repair(self.source, shell))
            self.assertEqual(evaluate_and_repair(self.source, '<p>暂无通知</p>')['action'], 'healthy')
            denied = FetchFailure(FetchResult(self.url, status=403, html=shell,
                outcome='denied', error_code='http_403', message='访问受限'))
            with patch('backend.scraper.engine._fetch_html', side_effect=denied), self.assertRaises(FetchFailure):
                collect_source(self.source)
        ai.assert_not_called()
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)

    def test_rules_can_repair_with_ai_disabled_and_scope_stays_subscribed(self):
        from backend.services import tasks
        tasks.enqueue('discover', self.source.school_id,
            {'school_id': self.source.school_id, 'ai_assist': False})
        job = queue_parser_repair(self.source, self.changed)
        self.assertFalse(job.payload['ai_assist'])
        sub = Subscription.query.one()
        sub.department_ids = []
        db.session.commit()
        self.assertIsNone(queue_parser_repair(self.source, self.changed))
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 1)

    def test_health_monitor_queues_the_same_validated_repair(self):
        self.assertEqual(evaluate_and_repair(self.source, self.html)['action'], 'healthy')
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 0)
        result = evaluate_and_repair(self.source, self.changed)
        self.assertEqual(result['action'], 'repairing')
        self.assertEqual(result['repair_task_id'], queue_parser_repair(self.source, self.changed).id)
        self.assertEqual(SourceConfigVersion.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
