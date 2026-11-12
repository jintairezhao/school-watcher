"""Transient reads yield durably without losing article or column progress."""
from copy import deepcopy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.database.source_governance_models import SourceValidation
from backend.scraper.acquisition import FetchFailure, FetchResult
from backend.services import tasks
from tests import test_direct_onboarding as fixture_module

URL, HTML, TITLES = fixture_module.URL, fixture_module.HTML, fixture_module.TITLES


class OnboardingNetworkCheckpointTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.socket.connect',
                       'backend.ai.providers.complete', 'backend.ai.runtime.run_skill'):
            guard = patch(target, side_effect=AssertionError('Offline program check only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def failure(self, url):
        return FetchFailure(FetchResult(url, status=403, outcome='network_error',
            error_code='http_proxy_access_denied', message='Network route comparison pending'))

    def records(self):
        return [{'url': 'https://example.edu.cn/info/' + str(i) + '.htm', 'title': title}
                for i, title in enumerate(TITLES)]

    def test_first_retryable_failure_yields_before_another_sample_can_defer(self):
        from backend.services.source_validation import article_samples
        reads = []
        def fetch(url, purpose):
            reads.append(url)
            if len(reads) == 1:
                raise self.failure(url)
            raise tasks.TaskDeferred(capability='http', phase='fetch', delay=1, reason='Synthetic origin wait')
        with self.assertRaises(FetchFailure) as caught:
            article_samples(dict(fixture_module.ai_column(), list_url=URL), self.records(), fetch)
        self.assertEqual(reads, [self.records()[0]['url']])
        samples = caught.exception.article_sample_summary
        self.assertFalse(samples['passed'])
        self.assertEqual(samples['samples'][0]['error_code'], 'http_proxy_access_denied')
        self.assertEqual(samples['available_count'], 3)

    def test_successful_sample_proof_is_retained_when_next_sample_has_network_failure(self):
        from backend.services.source_validation import article_samples
        reads = []
        def fetch(url, purpose):
            reads.append(url)
            if len(reads) > 1:
                raise self.failure(url)
            return self.fixture.fetch(url, purpose)
        with self.assertRaises(FetchFailure) as caught:
            article_samples(dict(fixture_module.ai_column(), list_url=URL), self.records(), fetch)
        result = caught.exception.article_sample_summary
        self.assertEqual(reads, [x['url'] for x in self.records()[:2]])
        self.assertEqual(result['matched_count'], 1)
        self.assertTrue(result['samples'][0]['content_hash'])
        self.assertFalse(result['passed'])

    def test_network_failure_is_checkpointed_and_next_durable_attempt_changes_route(self):
        from backend.scraper.http_client import remember_proxy_failure, retry_direct_for
        from backend.services.source_onboarding import onboard_page
        job = tasks.enqueue('onboard', 'network-checkpoint',
            {'school_id': self.fixture.school_id, 'url': URL,
             'ai_assist': False, 'observed_columns': [fixture_module.ai_column()]})
        original_identity = (job.id, job.identity, job.generation)
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle):
            tasks.checkpoint({'school_round': 'unchanged-round',
                'column_regions': {'cursor': 7, 'decision_ids': ['existing-paid-decision']}})
        original_regions = deepcopy(handle['checkpoint']['column_regions'])
        reads = []
        def unavailable(url, purpose):
            reads.append((url, purpose))
            if purpose == 'article':
                remember_proxy_failure(url)
                raise self.failure(url)
            return HTML
        with tasks.execution_scope(handle), self.assertRaises(FetchFailure) as caught:
            onboard_page(handle['payload'], fetcher=unavailable)
        self.assertEqual(sum(purpose == 'article' for _, purpose in reads), 1)
        db.session.refresh(job)
        state = job.checkpoint['column_validation']
        self.assertEqual(state['stage'], 'trial')
        self.assertEqual(len(state['failures']), 1)
        self.assertEqual(state['failures'][0]['failure_kind'], 'network_error')
        self.assertFalse(state['failures'][0]['repairable'])
        validation = json.loads(SourceValidation.query.one().validation_json)
        self.assertEqual(validation['status'], 'material_limited')
        self.assertEqual(validation['articles']['sample_count'], 1)
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(state['progress']['decisions'], 0)
        tasks.finish(handle, error=caught.exception, retryable=True)
        db.session.refresh(job)
        self.assertEqual(job.attempts, 1)
        job.available_at = datetime.utcnow()
        db.session.commit()
        resumed = tasks.claim(capabilities=['http'], exclude_kinds=('collect',))
        with tasks.execution_scope(resumed):
            self.assertTrue(retry_direct_for(self.records()[0]['url']))
            result = onboard_page(resumed['payload'], fetcher=self.fixture.fetch)
        tasks.finish(resumed, result)
        db.session.refresh(job)
        self.assertEqual(result['state'], 'connected')
        self.assertFalse(result['issues'])
        self.assertEqual((job.id, job.identity, job.generation), original_identity)
        self.assertEqual(job.checkpoint['school_round'], 'unchanged-round')
        self.assertEqual(job.checkpoint['column_regions'], original_regions)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 3)

    def test_completed_column_is_not_reread_when_later_column_retries(self):
        from backend.services.source_onboarding import onboard_page
        other = HTML[HTML.index('<section'):HTML.index('</section>')+len('</section>')]
        other = other.replace('notices', 'other').replace('通知公告', '资助通知').replace('/info/', '/other/')
        html = HTML.replace('</body>', other + '</body>')
        columns = [fixture_module.ai_column(), fixture_module.ai_column(name='资助通知',
            list_selector='#other li', container_selector='#other', heading_selector='#other h2')]
        job = tasks.enqueue('onboard', 'network-after-success',
            {'school_id': self.fixture.school_id, 'url': URL,
             'ai_assist': False, 'observed_columns': columns})
        handle = tasks.claim(capabilities=['http'])
        first_column_reads = []
        unavailable = True
        def fetch(url, purpose):
            if url == URL:
                return html
            if '/other/' in url and unavailable:
                raise self.failure(url)
            if '/info/' in url:
                first_column_reads.append(url)
            return self.fixture.fetch(url, purpose)
        with tasks.execution_scope(handle), self.assertRaises(FetchFailure) as caught:
            onboard_page(handle['payload'], fetcher=fetch)
        db.session.refresh(job)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(len(job.checkpoint['column_validation']['completed']), 1)
        prior_reads = list(first_column_reads)
        tasks.finish(handle, error=caught.exception, retryable=True)
        db.session.refresh(job)
        job.available_at = datetime.utcnow()
        db.session.commit()
        unavailable = False
        resumed = tasks.claim(capabilities=['http'], exclude_kinds=('collect',))
        with tasks.execution_scope(resumed):
            result = onboard_page(resumed['payload'], fetcher=fetch)
        tasks.finish(resumed, result)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(first_column_reads, prior_reads)
        self.assertEqual(len(prior_reads), 3)
        self.assertEqual(Department.query.count(), 2)
        self.assertEqual(Announcement.query.count(), 6)


if __name__ == '__main__':
    unittest.main()
