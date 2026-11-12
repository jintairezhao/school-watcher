"""Existing multi-list subscriptions must not become one selected child list."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Subscription
from backend.database.source_governance_models import SourceConfigVersion
from backend.services import source_governance as governance


class ConfiguredJointScopeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture = SourceGovernanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source = self.fixture.dept
        self.source.list_selector = '#notices li, #news li'
        db.session.add(Subscription(school_id=self.source.school_id,
                                    user_id=self.fixture.user.id, department_ids=[self.source.id]))
        db.session.commit()
        self.original = governance.source_config(self.source)
        news = self.fixture.html().replace('id="notices"', 'id="news"').replace(
            '通知公告', '学院新闻').replace('article/', 'news/')
        self.html = self.fixture.html() + news

    def proposal(self, candidate=None):
        config = candidate or self.original
        captured = governance.capture_source_evidence(self.source.school_id, config,
            department_id=self.source.id, inventory=self.fixture.identity,
            fetcher=self.fixture.fetcher(self.html))
        return governance.propose_source(self.source.school_id, config, captured,
                                        department_id=self.source.id, origin='manual_review')

    def test_valid_child_cannot_replace_existing_joint_source(self):
        proposal = self.proposal({**self.original, 'list_selector': '#notices li'})
        result = governance.validate_proposal(proposal.id)
        self.assertFalse(result['passed'], result)
        self.assertIn('configured_joint_scope_requires_review', result['errors'])
        self.assertEqual(result['configured_scope']['selected_items'], 6)
        self.assertEqual(len(result['configured_scope']['regions']), 2)
        self.assertEqual(governance.source_config(self.source), self.original)
        self.assertEqual(Subscription.query.one().department_ids, [self.source.id])
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)

    def test_existing_joint_review_stops_before_model_and_network(self):
        proposal = self.proposal()
        with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
                patch('backend.services.source_governance.run_source_skill_for_proposal',
                      side_effect=AssertionError('No model needed')), \
                patch('backend.services.source_governance.capture_source_evidence',
                      side_effect=AssertionError('No repeated reads needed')):
            result = governance._process_source_review({'proposal_id': proposal.id})
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(result['workflow']['status'], 'configured_scope_preserved')
        self.assertFalse(result['workflow']['requires_user'])
        self.assertEqual(json.loads(proposal.candidate_json), self.original)
        self.assertEqual(governance.source_config(self.source), self.original)

    def test_direct_exploration_and_recheck_cannot_bypass_scope_hold(self):
        from backend.services.source_exploration import run
        from backend.database.source_governance_models import SourceReviewEvent
        from backend.services.structure_versions import current_version
        proposal = self.proposal()
        with patch('backend.ai.configuration.get_model_binding', side_effect=AssertionError('No model')), \
                patch.object(governance, 'capture_source_evidence', side_effect=AssertionError('No new reads')):
            self.assertEqual(run(proposal.id)['status'], 'configured_scope_preserved')
            version = current_version(self.source.school_id)
            result = governance.verify_source_proposal(proposal.id)
        self.assertEqual(result['workflow']['status'], 'configured_scope_preserved')
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(), 1)
        self.assertEqual(current_version(self.source.school_id), version)

    def test_old_validation_cannot_save_a_narrowed_source(self):
        proposal = self.proposal({**self.original, 'list_selector': '#notices li'})
        with patch('backend.services.source_scope.configured_joint_scope', return_value=None):
            self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        with self.assertRaisesRegex(ValueError, '多个列表'):
            governance.activate_proposal(proposal.id)
        self.assertEqual(governance.source_config(self.source), self.original)
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_candidate_rebase_and_evicted_material_do_not_erase_recorded_scope(self):
        from backend.services.source_scope import hold_configured_joint_scope, configured_joint_scope
        proposal = self.proposal()
        self.assertTrue(hold_configured_joint_scope(proposal))
        candidate = {**self.original, 'list_url': 'https://college.example.edu.cn/other/',
                     'list_selector': '#notices li'}
        other = governance.propose_source(self.source.school_id, candidate, department_id=self.source.id)
        self.assertEqual(configured_joint_scope(other)['selected_items'], 6)
        with patch.object(governance, 'read_snapshot', side_effect=ValueError('material unavailable')):
            scope = configured_joint_scope(other)
        self.assertFalse(scope['material_available'])
        self.assertFalse(scope['current_reexecution'])
        self.assertEqual(scope['config_hash'], governance._hash(self.original))

    def test_repeated_heading_names_are_distinct_regions_not_merged_entities(self):
        from backend.services.source_scope import observe_joint_selection
        html = self.html.replace('学院新闻', '通知公告')
        reference = governance._snapshot(html, self.original['list_url'])
        scope = observe_joint_selection(self.original, reference, html)
        self.assertEqual([r['name'] for r in scope['regions']], ['通知公告', '通知公告'])
        self.assertFalse(scope['publisher_assertion'])
        self.assertEqual(len({r['list_selector'] for r in scope['regions']}), 2)

    def test_partial_or_unlocated_selection_cannot_claim_complete_partition(self):
        from backend.services.source_scope import observe_joint_selection
        reference = governance._snapshot(self.html, self.original['list_url'])
        self.assertIsNone(observe_joint_selection({**self.original, 'list_selector': '#notices li'},
                                                 reference, self.html))
        extra = '<aside><li><a href="other.htm">关于其他事项的通知</a></li></aside>'
        self.assertIsNone(observe_joint_selection({**self.original, 'list_selector': 'li'},
                                                 reference, self.html + extra))

    def test_expired_worker_cannot_save_scope_state(self):
        from backend.services.source_scope import hold_configured_joint_scope
        from backend.services.tasks import LeaseLost
        from backend.database.source_governance_models import SourceReviewEvent
        from backend.services.structure_versions import current_version
        proposal = self.proposal()
        before = (proposal.validation_json, proposal.state, current_version(self.source.school_id))
        with patch('backend.services.tasks.assert_owned', side_effect=LeaseLost()):
            with self.assertRaises(LeaseLost):
                hold_configured_joint_scope(proposal)
        self.assertEqual((proposal.validation_json, proposal.state,
                          current_version(self.source.school_id)), before)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(), 0)


if __name__ == '__main__':
    unittest.main()
