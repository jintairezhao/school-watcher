"""Known pager corrections replay saved rules without buying another decision."""
import copy
from datetime import datetime, timedelta
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.ai.models import AIBudget, AISchoolRound, AIExecution
from backend.database.db import db
from backend.database.models import (Announcement, BackgroundTask, Department,
                                    Subscription, User, UserRead, UserAnnouncementState)
from backend.services import tasks


class PaginationCheckpointUpgradeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.version = patch('backend.services.source_validation.VALIDATION_VERSION', 5)
        self.version.start()
        self.no_provider = patch('backend.ai.providers.complete',
                                 side_effect=AssertionError('Saved validation cannot pay for another model request'))
        self.no_provider.start()

    def tearDown(self):
        self.no_provider.stop()
        self.version.stop()
        self.fixture.tearDown()

    def retained_task(self, key='saved-pager', *, version=4, codes=None, legacy=False):
        from tests.test_direct_onboarding import URL, HTML, ai_column
        from backend.services.source_governance import _snapshot
        column = ai_column()
        failure = {'issue': {'name': column['name'], 'reason': '旧后续分页未通过',
                            'codes': codes or ['pagination_repeats_first_page']},
                   'config': dict(column, list_url=URL, group_name=''),
                   'observation': dict(column, codes=codes or ['pagination_repeats_first_page']),
                   'repairable': True, 'failure_kind': 'rule_error'}
        reference = _snapshot(HTML, URL, role='list')
        job = tasks.enqueue('onboard', key, {'school_id': self.fixture.school_id, 'url': URL,
                                           'ai_assist': True, 'revalidate': True})
        validation = {'page_hash': hashlib.sha256((URL + '\\0' + HTML).encode()).hexdigest(),
                      'validation_version': version, 'stage': 'done', 'completed': [],
                      'department_ids': [], 'new_count': 0, 'failures': [failure],
                      'result': {'state': 'unsupported', 'department_ids': [], 'issues': [failure['issue']]},
                      'progress': {'decisions': 4, 'stopped': True}, 'progress_ids': ['old-evidence'],
                      'last_request_key': 'original-model-input'}
        if not legacy:
            validation.update(trial_columns=[column], trial_origin_stage='ai',
                              trial_decision_ids=['original-paid-decision'],
                              trial_request_key='original-model-input')
        job.checkpoint = {'onboarding_page': reference, 'school_round': 'original-round',
                          'column_regions': {'columns': [column], 'decision_ids': ['original-paid-decision'],
                                             'cursor': 1},
                          'column_validation': validation}
        job.state, job.result = 'done', validation['result']
        job.finished_at = datetime.utcnow()
        db.session.commit()
        return job

    def paid_ledger(self):
        key = 'school-discovery:retained-round'
        budget = AIBudget(key=key, used_requests=37, used_tokens=189729,
                          reserved_tokens=8300, reserved_requests=0, active_count=0)
        round_row = AISchoolRound(school_id=self.fixture.school_id, budget_key=key)
        execution = AIExecution(execution_id='original-paid-decision', purpose='directory',
                                config_version='1', provider='deepseek', model='deepseek-flash',
                                skill_id='university-source-onboarding', skill_version='1',
                                skill_digest='original', input_digest='original-model-input', mode='column',
                                status='succeeded', usage={'known': True, 'total_tokens': 5000},
                                request_sent=True, active_released=True, budget_keys=[key],
                                output={'status': 'ready'})
        db.session.add_all([budget, round_row, execution])
        db.session.commit()
        return self.ledger()

    @staticmethod
    def ledger():
        return {'budgets': [(r.key, r.used_requests, r.used_tokens, r.reserved_tokens,
                             r.reserved_requests, r.active_count) for r in AIBudget.query.all()],
                'rounds': [(r.school_id, r.budget_key, r.created_at, r.completed_at)
                           for r in AISchoolRound.query.all()],
                'executions': [(r.execution_id, r.status, copy.deepcopy(r.usage), r.request_sent,
                                r.reserved_tokens, copy.deepcopy(r.output)) for r in AIExecution.query.all()]}

    def execute(self, job, *, validation_failure=False):
        from backend.services.source_onboarding import onboard_page, resume_rule_discovery
        resume_rule_discovery()
        handle = tasks.claim(capabilities=['http'])
        self.assertIsNotNone(handle)
        self.assertEqual(handle['id'], job.id)
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists',
                   side_effect=AssertionError('A paid rule is already saved')), \
             patch('backend.services.source_onboarding.recognize_column',
                   side_effect=AssertionError('Replay must never enter AI repair')):
            if validation_failure:
                with patch('backend.services.source_validation.pagination_sample',
                           return_value={'errors': ['pagination_repeats_first_page']}):
                    result = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
            else:
                result = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        tasks.finish(handle, result)
        db.session.refresh(job)
        return result

    def test_upgrade_targets_fixed_versions_and_only_known_failures(self):
        from backend.services.source_onboarding import _validation_upgrade
        for version, code, expected in ((3, 'article_sample_mismatch', True),
                                       (4, 'article_sample_mismatch', False),
                                       (4, 'pagination_repeats_first_page', True),
                                       (5, 'pagination_repeats_first_page', False),
                                       (4, 'pagination_scope_mismatch', False),
                                       (4, 'pagination_fetch_unavailable', False),
                                       (4, 'pagination_access_restricted', False),
                                       (4, 'column_identity_changed', False),
                                       (6, 'pagination_repeats_first_page', False)):
            with self.subTest(version=version, code=code):
                checkpoint = {'column_validation': {'validation_version': version,
                               'failures': [{'issue': {'codes': [code]}}]}}
                self.assertEqual(_validation_upgrade(checkpoint), expected)

    def test_paid_trial_replay_preserves_task_decision_and_shared_budget(self):
        job = self.retained_task()
        before_ledger = self.paid_ledger()
        original = copy.deepcopy(job.checkpoint)
        identity, generation = job.identity, job.generation
        result = self.execute(job)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['new_count'], 3)
        self.assertEqual((job.identity, job.generation), (identity, generation))
        self.assertEqual(self.ledger(), before_ledger)
        self.assertEqual(job.checkpoint['school_round'], original['school_round'])
        self.assertEqual(job.checkpoint['onboarding_page'], original['onboarding_page'])
        self.assertEqual(job.checkpoint['column_regions'], original['column_regions'])
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'], ['original-paid-decision'])
        self.assertEqual(job.checkpoint['column_validation']['trial_request_key'], 'original-model-input')
        self.assertEqual(job.checkpoint['column_validation']['validation_version'], 5)
        reads = list(self.fixture.reads)
        from backend.services.source_onboarding import resume_rule_discovery
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.fixture.reads, reads)

    def test_legacy_region_rule_replays_without_replacing_paid_cursor(self):
        job = self.retained_task(legacy=True)
        regions = copy.deepcopy(job.checkpoint['column_regions'])
        result = self.execute(job)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(job.checkpoint['column_regions'], regions)
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'], regions['decision_ids'])

    def test_unrelated_failed_columns_stay_visible_and_are_not_retried(self):
        from tests.test_direct_onboarding import ai_column
        job = self.retained_task()
        checkpoint = copy.deepcopy(job.checkpoint)
        other = ai_column(name='另一个栏目', list_selector='#other li',
                          container_selector='#other', heading_selector='#other h2')
        failure = {'issue': {'name': other['name'], 'reason': '真实标题矛盾', 'codes': ['column_name_mismatch']},
                   'config': other, 'observation': other, 'repairable': True, 'failure_kind': 'rule_error'}
        checkpoint['column_validation']['trial_columns'].append(other)
        checkpoint['column_validation']['failures'].append(failure)
        job.checkpoint = checkpoint
        db.session.commit()
        result = self.execute(job)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['issues'], [failure['issue']])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(job.checkpoint['column_validation']['failures'], [failure])

    def test_replay_failure_finishes_once_without_new_ai_or_round(self):
        job = self.retained_task()
        before_ledger = self.paid_ledger()
        result = self.execute(job, validation_failure=True)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['issues'][0]['codes'], ['pagination_repeats_first_page'])
        self.assertEqual(self.ledger(), before_ledger)
        self.assertEqual(job.generation, 1)
        from backend.services.source_onboarding import resume_rule_discovery
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(Department.query.count(), 0)

    def test_pause_website_wait_and_unrelated_errors_are_not_reopened(self):
        from backend.services.source_onboarding import resume_rule_discovery
        direct_pause = self.retained_task('direct-pause')
        direct_pause.payload = dict(direct_pause.payload, discovery_pause_requested=True)
        waiting = self.retained_task('website-wait')
        waiting.result = dict(waiting.result, website_not_before=(datetime.utcnow() + timedelta(hours=2)).isoformat())
        ordinary = self.retained_task('ordinary-rule', codes=['column_name_mismatch'])
        body_v4 = self.retained_task('v4-body', codes=['article_sample_mismatch'])
        current = self.retained_task('already-v5', version=5)
        db.session.commit()
        before = {r.id: (r.state, r.generation, copy.deepcopy(r.checkpoint))
                  for r in (direct_pause, waiting, ordinary, body_v4, current)}
        resume_rule_discovery()
        for row in (direct_pause, waiting, ordinary, body_v4, current):
            db.session.refresh(row)
            self.assertEqual((row.state, row.generation, row.checkpoint), before[row.id])

    def test_paused_school_prevents_old_child_reopening(self):
        from backend.services.source_onboarding import resume_rule_discovery
        parent = tasks.enqueue('discover', self.fixture.school_id,
                               {'school_id': self.fixture.school_id, 'discovery_pause_requested': True})
        job = self.retained_task()
        before = copy.deepcopy(job.checkpoint)
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual((job.state, job.generation, job.checkpoint), ('done', 1, before))
        self.assertTrue(parent.payload['discovery_pause_requested'])

    def test_discovery_slice_reuses_generation_and_honors_website_wait(self):
        from tests.test_direct_onboarding import URL, HTML
        from backend.services.source_onboarding import queue_columns
        key = str(self.fixture.school_id) + ':' + hashlib.sha256(URL.encode()).hexdigest()[:24]
        job = self.retained_task(key)
        original = copy.deepcopy(job.checkpoint)
        inventory = SimpleNamespace(
            report=lambda _: {'site': {'root_url': 'https://example.edu.cn/'},
                              'pages': [{'url': URL, 'kind': 'channel', 'state': 'fetched', 'label': '通知公告'}]},
            structure=lambda _: {}, snapshot=lambda *_: HTML)
        job.result = dict(job.result, website_not_before=(datetime.utcnow() + timedelta(hours=1)).isoformat())
        db.session.commit()
        with patch('backend.services.source_catalog.publication_candidates', return_value=[]):
            self.assertEqual(queue_columns(self.fixture.school_id, inventory, 'site')['onboarding_ids'], [])
            db.session.refresh(job)
            self.assertEqual((job.state, job.generation, job.checkpoint), ('done', 1, original))
            job.result = dict(job.result, website_not_before=(datetime.utcnow() - timedelta(seconds=1)).isoformat())
            db.session.commit()
            self.assertEqual(queue_columns(self.fixture.school_id, inventory, 'site')['onboarding_ids'], [job.id])
            db.session.refresh(job)
            self.assertEqual((job.state, job.generation, job.checkpoint), ('pending', 1, original))
            self.assertEqual(queue_columns(self.fixture.school_id, inventory, 'site')['onboarding_ids'], [])
        db.session.refresh(job)
        self.assertEqual((job.generation, job.checkpoint), (1, original))

    def test_unknown_paid_result_stays_waiting_without_replay_or_fee_changes(self):
        from backend.services.source_onboarding import resume_rule_discovery
        job = self.retained_task()
        self.paid_ledger()
        execution = db.session.get(AIExecution, 'original-paid-decision')
        execution.status = 'uncertain'
        execution.usage = {'known': False}
        db.session.commit()
        before = self.ledger()
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase, job.generation), ('waiting', 'ai_result', 1))
        self.assertEqual(job.checkpoint['column_pending_decision']['execution_id'], execution.execution_id)
        self.assertEqual(self.ledger(), before)
        self.assertIsNone(tasks.claim(capabilities=['http']))

    def test_stale_parent_and_unfinished_network_trial_are_not_reopened(self):
        from backend.services.source_onboarding import resume_rule_discovery
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        stale = self.retained_task('stale-parent')
        stale.payload = dict(stale.payload, parent_task_id=parent.id, parent_generation=parent.generation + 1)
        network = self.retained_task('unfinished-network')
        checkpoint = copy.deepcopy(network.checkpoint)
        checkpoint['column_validation']['stage'] = 'trial'
        network.state, network.error_code, network.checkpoint = 'failed', 'http_timeout', checkpoint
        db.session.commit()
        before = {r.id: (r.state, r.generation, copy.deepcopy(r.checkpoint)) for r in (stale, network)}
        resume_rule_discovery()
        for row in (stale, network):
            db.session.refresh(row)
            self.assertEqual((row.state, row.generation, row.checkpoint), before[row.id])

    def test_network_interruption_resumes_the_same_bounded_replay(self):
        from tests.test_direct_onboarding import URL
        from backend.scraper.acquisition import FetchFailure, FetchResult
        from backend.services.source_onboarding import onboard_page, resume_rule_discovery
        job = self.retained_task()
        before = self.paid_ledger()
        original_regions = copy.deepcopy(job.checkpoint['column_regions'])
        resume_rule_discovery()
        handle = tasks.claim(capabilities=['http'])
        def failed_read(address, purpose):
            raise FetchFailure(FetchResult(address, outcome='network_error', error_code='http_timeout'))
        with patch('backend.services.source_onboarding.recognize_column',
                   side_effect=AssertionError('An interrupted replay has already-paid rules')), \
             patch('backend.services.source_onboarding.publication_lists',
                   side_effect=AssertionError('An interrupted replay retains its plan')):
            with tasks.execution_scope(handle), self.assertRaises(FetchFailure) as raised:
                onboard_page(handle['payload'], fetcher=failed_read)
            tasks.finish(handle, error=raised.exception)
            db.session.refresh(job)
            self.assertEqual(job.checkpoint['column_validation']['stage'], 'trial')
            self.assertEqual(job.checkpoint['column_validation']['validation_version'], 5)
            job.available_at = datetime.utcnow()
            db.session.commit()
            resumed = tasks.claim(capabilities=['http'])
            self.assertIsNotNone(resumed)
            self.assertEqual((resumed['id'], resumed['generation']), (handle['id'], handle['generation']))
            with tasks.execution_scope(resumed):
                result = onboard_page(resumed['payload'], fetcher=self.fixture.fetch)
            tasks.finish(resumed, result)
        db.session.refresh(job)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(self.ledger(), before)
        self.assertEqual(job.checkpoint['column_regions'], original_regions)
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'], ['original-paid-decision'])
        self.assertEqual(job.checkpoint['onboarding_page']['url'], URL)

    def test_expired_material_reads_fresh_page_then_replays_paid_rule(self):
        from tests.test_direct_onboarding import URL, HTML
        from backend.services.source_governance import read_snapshot as original_read
        from backend.services.source_onboarding import onboard_page, resume_rule_discovery
        job = self.retained_task()
        before = self.paid_ledger()
        old_reference = copy.deepcopy(job.checkpoint['onboarding_page'])
        fresh_html = HTML.replace('</body>', '<footer>实际网站导航已调整</footer></body>')
        def snapshots(reference):
            if reference == old_reference:
                raise OSError('Isolated retained snapshot was removed')
            return original_read(reference)
        def fetch(address, purpose):
            return fresh_html if address == URL else self.fixture.fetch(address, purpose)
        resume_rule_discovery()
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), \
             patch('backend.services.source_governance.read_snapshot', side_effect=snapshots), \
             patch('backend.services.source_onboarding.publication_lists',
                   side_effect=AssertionError('Changed page must execute the saved candidate first')), \
             patch('backend.services.source_onboarding.recognize_column',
                   side_effect=AssertionError('An expired snapshot is not another model fee')):
            result = onboard_page(handle['payload'], fetcher=fetch)
        tasks.finish(handle, result)
        db.session.refresh(job)
        self.assertEqual(result['state'], 'connected')
        self.assertNotEqual(job.checkpoint['onboarding_page']['hash'], old_reference['hash'])
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'], ['original-paid-decision'])
        self.assertEqual(job.generation, 1)
        self.assertEqual(self.ledger(), before)

    def test_conditional_wake_does_not_overwrite_other_workers_claim(self):
        from backend.services.source_onboarding import _resume_pagination_validation
        job = self.retained_task()
        original = copy.deepcopy(job.checkpoint)
        def worker_claim(*_):
            db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == job.id).values(
                state='running', token='other-worker-token', worker_id='other-worker',
                lease_until=datetime.utcnow() + timedelta(minutes=2)).execution_options(synchronize_session=False))
            return False
        with patch('backend.services.discovery_control.should_pause', side_effect=worker_claim):
            self.assertFalse(_resume_pagination_validation(job, datetime.utcnow()))
        self.assertEqual((job.state, job.token, job.worker_id), ('running', 'other-worker-token', 'other-worker'))
        self.assertEqual((job.generation, job.checkpoint), (1, original))

    def test_network_failure_is_not_woken_as_a_validator_correction(self):
        from backend.services.source_onboarding import resume_rule_discovery
        job = self.retained_task()
        job.state, job.error_code, job.attempts = 'failed', 'http_timeout', 3
        db.session.commit()
        original = copy.deepcopy(job.checkpoint)
        resume_rule_discovery()
        db.session.refresh(job)
        self.assertEqual((job.state, job.error_code, job.attempts, job.generation), ('failed', 'http_timeout', 3, 1))
        self.assertEqual(job.checkpoint, original)

    def test_existing_source_subscriptions_read_star_and_history_survive_replay(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL
        initial = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False},
                               fetcher=self.fixture.fetch)
        source_id = initial['department_ids'][0]
        for row in BackgroundTask.query.all():
            row.state = 'done'
        user = User.query.one()
        article = Announcement.query.order_by(Announcement.id).first()
        subscription = Subscription.query.one()
        subscription.department_ids = [source_id]
        read = UserRead(user_id=user.id, announcement_id=article.id)
        star = UserAnnouncementState(user_id=user.id, announcement_id=article.id, starred=True)
        db.session.add_all([read, star])
        db.session.commit()
        history = [(a.id, a.title, a.url) for a in Announcement.query.order_by(Announcement.id)]
        job = self.retained_task()
        checkpoint = copy.deepcopy(job.checkpoint)
        checkpoint['column_validation']['department_ids'] = [source_id]
        job.checkpoint = checkpoint
        db.session.commit()
        result = self.execute(job)
        self.assertEqual(result['department_ids'], [source_id])
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual([(a.id, a.title, a.url) for a in Announcement.query.order_by(Announcement.id)], history)
        self.assertEqual(Subscription.query.one().department_ids, [source_id])
        self.assertEqual(UserRead.query.one().announcement_id, article.id)
        self.assertTrue(UserAnnouncementState.query.one().starred)


if __name__ == '__main__':
    unittest.main()
