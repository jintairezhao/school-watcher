"""A list template match does not establish an article body's CSS contract."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department
from backend.database.source_governance_models import SourceValidation
from backend.scraper.acquisition import FetchFailure, FetchResult, classify_result
from backend.scraper.discovery.publication_lists import publication_lists
from backend.services import tasks
from backend.services.discovery_policy import progress_state
from backend.services.source_governance import _snapshot
from backend.services.source_onboarding import onboard_page
from backend.services.source_validation import VALIDATION_VERSION
from tests import test_direct_onboarding as fixture_module

TITLES, URL = fixture_module.TITLES, fixture_module.URL


DEFAULT_BODY = 'div.article-content, div.content, div.main, article'
PROFILE = {'list_selector': 'ul.news li', 'title_selector': 'a', 'link_selector': 'a',
           'date_selector': 'span', 'content_selector': DEFAULT_BODY}
HTML = ('<html><title>通知公告</title><body><section><h2>通知公告</h2><ul class="news">' +
        ''.join(f'<li><a href="/info/1001/{100000+i}.htm">{title}</a>'
                '<span>2026-10-01</span></li>' for i, title in enumerate(TITLES)) +
        '</ul></section></body></html>')


class UnverifiedProfileBodyTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.socket.connect',
                     'backend.ai.providers.complete', 'backend.ai.runtime.run_skill'):
            guard = patch(name, side_effect=AssertionError('Offline only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        guard = patch('backend.scraper.discovery.publication_lists.load_selector_profiles',
                      return_value=[deepcopy(PROFILE)])
        guard.start()
        self.addCleanup(guard.stop)
        self.requests = []

    def transport(self, request):
        self.requests.append(request)
        if request.url == URL:
            page = HTML
        else:
            index = int(request.url.rsplit('/', 1)[-1].split('.')[0]) - 100000
            page = ('<html><body><h1>' + TITLES[index] + '</h1>'
                    '<div>发布日期：2026-10-01</div><div class="v_news_content">'
                    '<p>请各学院按照本通知公布的要求组织开展教学工作，并按期提交有关材料。</p>'
                    '</div></body></html>')
        result = classify_result(request, FetchResult(request.url, status=200, html=page))
        if not result.ok:
            raise FetchFailure(result)
        return result

    def run_case(self, *, retained=False, origin='rules', verified=False, selector=DEFAULT_BODY):
        payload = {'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False}
        handle = None
        if retained:
            feeds = [f for f in publication_lists(HTML, URL) if f['method'] == 'profile_match']
            self.assertEqual(len(feeds), 1)
            legacy = dict(feeds[0], content_selector=selector, verified=verified)
            job = tasks.enqueue('onboard', 'retained-profile-rule', payload)
            handle = tasks.claim(capabilities=['http'], exclude_kinds=('collect',))
            saved = {'page_hash': hashlib.sha256((URL + '\\0' + HTML).encode()).hexdigest(),
                     'stage': 'trial', 'trial_origin_stage': origin, 'trial_columns': [legacy],
                     'trial_decision_ids': ['paid-plan'] if origin == 'ai' else [],
                     'trial_request_key': 'paid-key' if origin == 'ai' else '',
                     'validation_version': VALIDATION_VERSION, 'completed': [], 'failures': [],
                     'department_ids': [], 'new_count': 0, 'progress': progress_state(),
                     'progress_ids': [], 'empty_columns': [], 'empty_rules': []}
            with tasks.execution_scope(handle):
                tasks.checkpoint({'onboarding_page': _snapshot(HTML, URL, role='list'),
                                  'column_validation': saved, 'unrelated_cursor': 7})
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            if handle:
                with tasks.execution_scope(handle):
                    result = onboard_page(payload)
                tasks.finish(handle, result)
                db.session.refresh(job)
                self.assertEqual(job.checkpoint['unrelated_cursor'], 7)
                self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'],
                                 ['paid-plan'] if origin == 'ai' else [])
            else:
                result = onboard_page(payload)
        return result

    def test_fresh_template_body_default_is_not_installed_as_verified_css(self):
        result = self.run_case()
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(Department.query.one().content_selector, '')
        self.assertTrue(all(not r.readiness_selector for r in self.requests if r.purpose == 'article'))

    def test_retained_unpaid_template_is_corrected_from_the_same_list_material(self):
        result = self.run_case(retained=True)
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(Department.query.one().content_selector, '')
        self.assertEqual(SourceValidation.query.count(), 1)

    def assert_explicit_contract_kept(self, **changes):
        result = self.run_case(retained=True, **changes)
        self.assertFalse(result.get('department_ids'), result)
        self.assertEqual(Department.query.count(), 0)
        expected = changes.get('selector', DEFAULT_BODY)
        self.assertTrue(self.requests)
        self.assertTrue(all(r.readiness_selector == expected for r in self.requests if r.purpose == 'article'))

    def test_paid_plan_body_rule_keeps_its_explicit_contract(self):
        self.assert_explicit_contract_kept(origin='ai')

    def test_verified_rule_keeps_its_explicit_contract(self):
        self.assert_explicit_contract_kept(verified=True)

    def test_changed_rule_is_not_mistaken_for_the_template_default(self):
        self.assert_explicit_contract_kept(selector='#manually-picked-body')


if __name__ == '__main__':
    unittest.main()
