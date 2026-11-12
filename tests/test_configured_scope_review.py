"""Correct a legacy source label from its complete observed list, not a URL/name guess."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department, Subscription
from backend.database.source_governance_models import SourceConfigVersion, SourceReviewEvent
from backend.services import source_governance as governance
from backend.services import source_review_recovery as recovery


class ConfiguredScopeReviewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture = SourceGovernanceTests()
        self.fixture.setUp()
        self.source = self.fixture.dept
        self.source.name = '示例学院'
        db.session.commit()
        self.original = governance.source_config(self.source)

    def tearDown(self):
        self.fixture.tearDown()

    def proposal(self, config=None):
        config = config or self.original
        snapshot = governance._snapshot(self.fixture.html(), config['list_url'])
        evidence = governance.CapturedEvidence(dict(schema=1, school_id=self.source.school_id,
            school_name=self.fixture.school.name, root_url=self.fixture.school.url,
            config=config, config_hash=governance._hash(config), expected_config=self.original,
            list=snapshot, articles=[], identity_paths=[], identity_snapshots=[]))
        return governance.propose_source(self.source.school_id, config, evidence,
                                        department_id=self.source.id, origin='manual_review')

    def test_complete_single_list_supplies_its_actual_heading(self):
        revision = recovery.observed_scope_revision(self.original, self.fixture.html())
        self.assertEqual(revision['config']['name'], '通知公告')
        self.assertEqual(revision['config']['list_selector'], self.original['list_selector'])
        self.assertEqual(revision['config']['group_name'], '示例学院')
        self.assertEqual(revision['region']['name'], '通知公告')

    def test_joint_selector_does_not_rename_multiple_columns_as_one(self):
        other = self.fixture.html().replace('id="notices"', 'id="news"').replace('通知公告', '校园新闻')
        config = {**self.original, 'list_selector': 'section li'}
        self.assertIsNone(recovery.observed_scope_revision(config, self.fixture.html() + other))

    def test_observed_redirect_cannot_be_relabelled_as_the_original_address(self):
        from backend.scraper.acquisition import FetchResult, FetchedHTML
        html = FetchedHTML(FetchResult('https://other.example.edu.cn/notices/',
                                       html=self.fixture.html(), outcome='usable', status=200))
        self.assertIsNone(recovery.observed_scope_revision(self.original, html))

    def test_partial_material_creates_a_draft_not_a_receipt_or_source_change(self):
        from backend.services.source_path_receipts import load_source_receipts
        from backend.services.structure_versions import current_version
        proposal = self.proposal()
        before = current_version(self.source.school_id)
        with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
                patch('backend.ai.providers.complete', side_effect=AssertionError('No model')):
            self.assertTrue(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertEqual(governance.source_config(self.source), self.original)
        self.assertEqual(json.loads(proposal.candidate_json)['name'], '通知公告')
        self.assertEqual(proposal.department_id, self.source.id)
        self.assertEqual(proposal.state, 'needs_review')
        result = json.loads(proposal.validation_json)
        self.assertFalse(result['passed'])
        self.assertIn('independent_sample_missing', result['errors'])
        self.assertNotIn('column_identity_or_scope_unconfirmed', result['errors'])
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(load_source_receipts({'name': self.fixture.school.name,
                                              'root_url': self.fixture.school.url}), [])
        self.assertGreater(current_version(self.source.school_id)['revision'], before['revision'])
        self.assert_source_readers(self.original['name'])
        self.assertFalse(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_observed').count(), 1)

    def test_explicitly_edited_config_is_not_silently_replaced(self):
        proposal = self.proposal({**self.original, 'name': '用户指定的栏目'})
        self.assertFalse(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertEqual(json.loads(proposal.candidate_json)['name'], '用户指定的栏目')

    def test_stale_expected_config_cannot_create_a_repair_revision(self):
        proposal = self.proposal()
        self.source.title_selector = 'a:first-child'
        db.session.commit()
        self.assertFalse(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_observed').count(), 0)

    def test_automatic_repair_stays_disabled(self):
        proposal = self.proposal()
        proposal.origin = 'repair'
        db.session.commit()
        self.assertFalse(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertEqual(governance.source_config(self.source), self.original)

    def assert_source_readers(self, name):
        from backend.services.structure_snapshot import read_structure, source_choices, graph_projection
        capture = read_structure(self.source.school)
        choices = [column for group in source_choices(capture).values() for entry in group
                   for column in entry['own_columns']]
        self.assertEqual([(column['department'].id, column['label']) for column in choices],
                         [(self.source.id, name)])
        graph = graph_projection(capture, self.source.school, scope='exploration')
        self.assertEqual([(node['source_id'], node['label']) for node in graph['nodes']
                          if node['entity_type'] == 'source'], [(self.source.id, name)])
        self.assertFalse(any(edge['type'] == 'source_ownership' for edge in graph['relations']))

    def test_existing_manual_review_uses_scope_then_normal_gate_and_preserves_id(self):
        proposal = self.proposal()
        self.complete_review_without_model(proposal)

    def test_resume_reuses_bound_scope_revision_without_model(self):
        proposal = self.proposal()
        self.assertTrue(recovery.prepare_configured_scope_review(proposal, self.fixture.html()))
        self.assertTrue(recovery.configured_scope_current(proposal, self.fixture.html()))
        self.assertFalse(recovery.configured_scope_current(proposal, self.fixture.html().replace('通知公告', '学院新闻')))
        self.complete_review_without_model(proposal)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_observed').count(), 1)

    def complete_review_without_model(self, proposal):
        ident = self.source.id
        db.session.add(Subscription(school_id=self.source.school_id, user_id=self.fixture.user.id,
                                    department_ids=[ident]))
        db.session.commit()
        capture = governance.capture_source_evidence
        def capture_retained(school_id, candidate, **kwargs):
            kwargs['inventory'] = self.fixture.identity
            kwargs['fetcher'] = self.fixture.fetcher(self.fixture.html())
            return capture(school_id, candidate, **kwargs)
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 'configured'}), \
                patch('backend.services.source_governance.capture_source_evidence', side_effect=capture_retained), \
                patch('backend.services.source_governance.run_source_skill_for_proposal', side_effect=AssertionError('No model needed')), \
                patch('socket.socket.connect', side_effect=AssertionError('No network')):
            result = governance._process_source_review({'proposal_id': proposal.id})
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(self.source.id, ident)
        self.assertEqual(self.source.name, '通知公告')
        self.assertEqual(Subscription.query.one().department_ids, [ident])
        self.assertEqual(SourceConfigVersion.query.one().department_id, ident)
        self.assert_source_readers('通知公告')


if __name__ == '__main__':
    unittest.main()
