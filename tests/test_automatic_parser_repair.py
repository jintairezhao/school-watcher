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
