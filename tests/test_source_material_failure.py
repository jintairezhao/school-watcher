"""Acquisition gaps must not purchase selector repair or verify a source."""
import json
import unittest
from unittest.mock import patch

from backend.database.models import Announcement, Department
from backend.database.source_governance_models import SourceValidation
from backend.services.source_validation import sample_failure_kind
from tests import test_direct_onboarding as fixture_module

HTML, URL, ai_column = fixture_module.HTML, fixture_module.URL, fixture_module.ai_column


class MaterialFailureClassificationTests(unittest.TestCase):
    def test_rendered_existing_empty_body_is_material_missing(self):
        from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
        address = 'https://example.edu.cn/notice.htm'
        request = FetchRequest(address, purpose='article', readiness_selector='div.detail-content')
        for region in ('<div class="detail-content"></div>', ''):
            with self.subTest(region_present=bool(region)):
                result = classify_result(request, FetchResult(address, status=200,
                    html='<html><h1>离校手续办理通知</h1>'+region+'</html>', transport='browser'))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, 'article_body_missing_after_render' if region else 'readiness_missing')
                self.assertEqual(sample_failure_kind([{'reason': result.outcome, 'error_code': result.error_code}]),
                                 'material_incomplete' if region else 'rule_error')

    def test_render_pending_samples_are_material_gaps(self):
        for code in ('readiness_missing', 'article_body_not_loaded', 'javascript_shell',
                     'fragment_route', 'navigation_not_loaded'):
            with self.subTest(code=code):
                self.assertEqual(sample_failure_kind([
                    {'reason': 'needs_adapter', 'error_code': 'response_too_large'},
                    {'reason': 'requires_render', 'error_code': code},
                    {'matched': True},
                ]), 'material_incomplete')

    def test_static_rule_failure_and_real_identity_conflict_still_need_attention(self):
        for failure in (
            {'reason': 'needs_adapter', 'error_code': 'readiness_missing'},
            {'reason': 'article_identity_conflict'},
        ):
            with self.subTest(failure=failure):
                self.assertEqual(sample_failure_kind([
                    {'reason': 'requires_render', 'error_code': 'readiness_missing'},
                    failure,
                ]), 'rule_error')

    def test_access_challenge_keeps_its_access_classification(self):
        self.assertEqual(sample_failure_kind([
            {'reason': 'requires_render', 'error_code': 'access_challenge'},
        ]), 'access_restricted')


class MaterialFailureOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_mixed_acquisition_gaps_retain_rules_without_activation_or_ai_repair(self):
        from backend.scraper.acquisition import FetchFailure, FetchResult
        from backend.services.source_onboarding import onboard_page

        def reader(address, purpose):
            if address == URL:
                return HTML
            index = int(address.rsplit('/', 1)[-1].split('.')[0])
            if index < 2:
                raise FetchFailure(FetchResult(address, status=200,
                    outcome='needs_adapter' if index == 0 else 'requires_render',
                    error_code='response_too_large' if index == 0 else 'readiness_missing'))
            return self.fixture.fetch(address, purpose)

        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column()]), \
             patch('backend.services.source_onboarding.recognize_column',
                   side_effect=AssertionError('Material gaps cannot buy model repair')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=reader)
        self.assertEqual(result['state'], 'material_limited')
        self.assertEqual(result['issues'][0]['codes'], ['article_material_incomplete'])
        self.assertEqual(result['recovery_rules'][0]['content_selector'], 'article')
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(Announcement.query.count(), 0)
        validation = json.loads(SourceValidation.query.order_by(SourceValidation.id.desc()).first().validation_json)
        self.assertEqual(validation['status'], 'material_limited')
        self.assertEqual(validation['articles']['matched_count'], 1)
        self.assertEqual(validation['articles']['required_matches'], 2)
        self.assertFalse(validation['articles']['passed'])
