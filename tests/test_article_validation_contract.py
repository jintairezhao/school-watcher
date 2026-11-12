"""Both source writers use the same observed article acceptance contract."""
import json
import unittest
from unittest.mock import patch

from backend.services import source_governance as governance
from backend.services.source_validation import article_samples
from backend.scraper.acquisition import FetchFailure, FetchResult
from tests import test_source_governance as fixture_module


class ArticleValidationContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.SourceGovernanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def compare(self, case, count=3):
        fixture = self.fixture
        html = fixture.html(count)
        config = governance.source_config(fixture.dept)
        base = fixture.fetcher(html)
        def reader(url, purpose):
            if purpose in ('article', 'body'):
                index = int(url.rsplit('/', 1)[-1].split('.')[0])
                if case == 'material_gap' and index == 1:
                    raise FetchFailure(FetchResult(url, status=200, outcome='needs_adapter',
                        error_code='article_body_missing_after_render', transport='browser',
                        html='<h1>关于研究生报名事项的通知1</h1><div class="article"></div>'))
                if case == 'gated' and index < 3:
                    raise FetchFailure(FetchResult(url,status=403,outcome='denied',error_code='http_403'))
                result = base(url, 'body')
                if case == 'wrong_identity' and index == 1:
                    result = result.replace('关于研究生报名事项的通知1', '另一个学院的其他通知')
                return result
            return base(url,purpose)
        evidence = governance.capture_source_evidence(fixture.school.id,config,department_id=fixture.dept.id,
            seed_html=html,inventory=fixture.identity,fetcher=reader)
        records,errors,_ = governance._column_scope(config,evidence.bundle['list'])
        self.assertFalse(errors)
        native = article_samples(config,records,reader)
        proposal = governance.propose_source(fixture.school.id,config,evidence,department_id=fixture.dept.id)
        formal = governance.validate_proposal(proposal.id)
        self.assertEqual(native['passed'],formal['passed'],(native,formal))
        self.assertEqual(native['matched_count'],formal['articles']['matched_count'])
        return native,formal,proposal

    def test_two_good_articles_cannot_erase_an_observed_material_gap(self):
        native,formal,proposal = self.compare('material_gap')
        self.assertFalse(native['passed'])
        self.assertEqual(native['matched_count'],2)
        self.assertEqual(native['failure_kind'],'material_incomplete')
        self.assertEqual(formal['articles']['failure_kind'],'material_incomplete')
        with patch('backend.ai.configuration.get_model_binding',side_effect=AssertionError('No model configuration needed')):
            result = governance.run_source_skill_for_proposal(proposal.id)
        self.assertEqual(result['status'],'material_limited')

    def test_actual_access_limits_keep_the_existing_limited_public_source_case(self):
        native,formal,_ = self.compare('gated',5)
        self.assertTrue(native['passed'])
        self.assertEqual(native['matched_count'],1)
        self.assertEqual(native['status'],'limited')
        self.assertEqual(len(formal['limitations']),3)

    def test_observed_identity_conflict_blocks_both_paths(self):
        from backend.ai.configuration import AIConfigError
        native,formal,proposal = self.compare('wrong_identity')
        self.assertFalse(native['passed'])
        self.assertTrue(native['identity_conflicts'])
        self.assertIn('article_identity_mismatch',formal['errors'])
        failed = next(row for row in formal['articles']['samples'] if not row['matched'])
        original = next(ref for ref in json.loads(proposal.evidence_json)['articles'] if ref['listed_url']==failed['url'])
        self.assertEqual(failed['snapshot'],original)
        with patch('backend.ai.configuration.get_model_binding',side_effect=AIConfigError('fixture not configured')) as binding:
            result = governance.run_source_skill_for_proposal(proposal.id)
        binding.assert_called_once()
        self.assertEqual(result['status'],'not_configured')

    def test_fully_readable_column_works_in_both_paths(self):
        native,formal,_ = self.compare('good')
        self.assertTrue(native['passed'])
        self.assertEqual(native['matched_count'],3)

    def test_genuinely_single_entry_column_works_in_both_paths(self):
        native,formal,_ = self.compare('good',1)
        self.assertTrue(native['passed'])
        self.assertEqual(native['required_matches'],1)

    def test_previous_validated_draft_cannot_activate_under_the_new_contract(self):
        from backend.database.db import db
        from backend.database.source_governance_models import SourceConfigVersion
        proposal = self.fixture.proposal()
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        proposal.validator_version = 'source-governance-19'
        proposal.validated_hash = governance._hash([
            proposal.candidate_json,proposal.evidence_hash,proposal.validator_version])
        db.session.commit()
        with self.assertRaisesRegex(ValueError,'来源尚未通过统一检查'):
            governance.activate_proposal(proposal.id)
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def test_all_gated_stays_access_limited_without_model_repair(self):
        native,formal,proposal = self.compare('gated')
        self.assertFalse(native['passed'])
        self.assertEqual(native['failure_kind'],'access_restricted')
        self.assertIn('source_access_restricted',formal['errors'])
        with patch('backend.ai.configuration.get_model_binding',side_effect=AssertionError('No model for an access wall')):
            result = governance.run_source_skill_for_proposal(proposal.id)
        self.assertEqual(result['status'],'access_limited')
