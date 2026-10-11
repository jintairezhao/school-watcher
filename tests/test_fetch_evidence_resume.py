import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from tests import test_direct_onboarding as fixture_module
from backend.services import tasks, task_fetch
from backend.scraper.acquisition import FetchRequest, FetchResult, execution_context, fetch


class FetchEvidenceResumeTests(unittest.TestCase):
    def test_completed_old_unrecognized_body_uses_current_classifier_without_rereading(self):
        tasks.enqueue('onboard', 'old-body', {'school_id': self.fixture.school_id,
            'url': 'https://example.edu.cn/'})
        handle = tasks.claim()
        request = FetchRequest('https://example.edu.cn/notice.htm', purpose='article')
        observation = FetchResult(request.url, status=200,
            html='<h1>公开通知</h1><div class="Article_content"><p>学校已经公布本学期安排，请各学院组织学生按照通知要求做好准备。</p></div>',
            outcome='needs_adapter', error_code='content_not_recognized')
        with tasks.execution_scope(handle):
            task_fetch.cache_store(request, observation)
        with tasks.execution_scope(dict(handle)), execution_context(cache_lookup=task_fetch.cache_lookup):
            result = fetch(request, http_transport=Mock(side_effect=AssertionError('duplicate website read')),
                           browser_transport=Mock(side_effect=AssertionError('duplicate browser read')))
        self.assertTrue(result.ok)
        self.assertEqual(result.html, observation.html)
        self.assertIn('article_region', result.evidence)

    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests(); self.fixture.setUp()
        self.fixture.app.config['FETCH_EVIDENCE_DIR'] = str(Path(self.fixture.app.config['SOURCE_CATALOG_PATH']).parent / 'fetch-evidence')

    def tearDown(self):
        self.fixture.tearDown()

    def test_unreadable_sample_replays_after_queue_handoff_without_another_read(self):
        tasks.enqueue('onboard', 'resume', {'school_id': self.fixture.school_id, 'url': 'https://example.edu.cn/'})
        handle = tasks.claim()
        request = FetchRequest('https://example.edu.cn/notice.htm', purpose='article')
        denied = FetchResult(request.url, status=403, html='<h1>访问被拒绝</h1>', outcome='denied', error_code='http_403')
        with tasks.execution_scope(handle):
            task_fetch.cache_store(request, denied)
        # A fresh execution scope reads persisted material, preserving its failure.
        with tasks.execution_scope(dict(handle)), execution_context(cache_lookup=task_fetch.cache_lookup):
            result = fetch(request, http_transport=Mock(side_effect=AssertionError('duplicate website read')))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'http_403')

    def test_restricted_sample_does_not_require_user_verification_of_whole_column(self):
        tasks.enqueue('onboard', 'restricted', {'school_id': self.fixture.school_id, 'url': 'https://example.edu.cn/'}, capability='browser')
        handle = tasks.claim()
        request = FetchRequest('https://example.edu.cn/notice.htm', purpose='article', policy={'exploration': True})
        blocked = FetchResult(request.url, status=200, html='<p>请完成安全验证</p>', outcome='needs_manual', error_code='human_verification')
        with tasks.execution_scope(handle), patch.object(task_fetch.BrowserClient, 'submit', return_value=('done', blocked)):
            result = task_fetch.browser_dispatch(request)
        self.assertEqual(result.outcome, 'needs_manual')
        self.assertFalse(result.ok)
