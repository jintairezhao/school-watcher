"""A later saved equivalent configuration resolves old work, preserving failure proof."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Subscription, User
from backend.database.source_governance_models import SourceProposal, SourceConfigVersion, SourceReviewEvent
from backend.services import source_governance as governance
from backend.services.source_workflow import workflow
from backend.services.structure_versions import current_version
from tests import test_source_activation_atomic as atomic
from tests import test_school_structure as structure


class EquivalentReviewResolutionTests(unittest.TestCase):
    def setUp(self):
        self.case = atomic.SourceActivationAtomicTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.source = self.case.legacy_source()
        self.original_config = governance.source_config(self.source)
        self.user = User.query.first(); self.user.role = 'admin'
        self.subscription = Subscription.query.one(); self.subscription.department_ids = []
        db.session.commit()
        def unavailable_independent(url, purpose):
            if purpose == 'independent_list': raise OSError('controlled unavailable independent read')
            return self.case.f.fixture.fetch(url, purpose) if purpose == 'body' else structure.HTML
        evidence = governance.capture_source_evidence(self.case.school.id, self.case.config, department_id=self.source.id,
            seed_html=structure.HTML, inventory=self.case.f.catalog, fetcher=unavailable_independent)
        self.old = governance.propose_source(self.case.school.id, self.case.config, evidence, department_id=self.source.id, origin='manual_review')
        result = governance.validate_proposal(self.old.id)
        self.assertFalse(result['passed']); self.assertIn('independent_sample_missing', result['errors'])
        self.old_validation = self.old.validation_json; self.old_evidence = self.old.evidence_json
        db.session.add(SourceReviewEvent(proposal_id=self.old.id, action='subscribe_on_activation', actor_id=self.user.id, detail_json='{}'))
        db.session.commit()
        self.current = self.case.proposal(self.source)
        self.assertNotEqual(self.old.id, self.current.id)

    def test_equivalent_old_failure_is_resolved_at_commit_and_not_checked_again(self):
        version = current_version(self.case.school.id)
        result = governance.activate_proposal(self.current.id)
        self.assertEqual(result.id, self.source.id)
        self.assertEqual(self.old.state, 'superseded')
        self.assertEqual(self.old.validation_json, self.old_validation)
        self.assertEqual(self.old.evidence_json, self.old_evidence)
        event = SourceReviewEvent.query.filter_by(proposal_id=self.old.id, action='superseded_by_configuration').one()
        self.assertEqual(json.loads(event.detail_json)['replacement_proposal_id'], self.current.id)
        self.assertEqual(json.loads(event.detail_json)['previous_state'], 'needs_review')
        self.assertEqual(self.subscription.department_ids, [self.source.id])
        self.assertEqual(current_version(self.case.school.id)['revision'], version['revision'] + 1)
        self.assertEqual(governance.school_governance_status(self.case.school.id)['proposals'].get('needs_review', 0), 0)
        self.assertEqual(workflow(self.old)['replacement']['replacement_proposal_id'], self.current.id)
        with patch.object(governance, '_fetch', side_effect=AssertionError('must not fetch')), \
                patch.object(governance, 'run_source_skill_for_proposal', side_effect=AssertionError('must not call AI')):
            historical = governance.process_source_review({'proposal_id':self.old.id})
        self.assertEqual(historical['state'], 'superseded')
        self.assertEqual(historical['validation']['errors'], json.loads(self.old_validation)['errors'])
        self.subscription.department_ids = []; db.session.commit()
        saved = current_version(self.case.school.id)
        governance.activate_proposal(self.current.id)
        self.assertEqual(current_version(self.case.school.id), saved)
        self.assertEqual(self.subscription.department_ids, [])
        self.assertEqual(SourceReviewEvent.query.filter_by(action='superseded_by_configuration').count(), 1)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_failed_save_rolls_back_resolution_event_and_pending_subscription(self):
        before = current_version(self.case.school.id)
        with patch('backend.services.school_structure.sync_official_structure', side_effect=RuntimeError('resolution transaction failure')):
            with self.assertRaisesRegex(RuntimeError, 'resolution transaction'):
                governance.activate_proposal(self.current.id)
        db.session.commit()
        self.assertEqual(db.session.get(SourceProposal,self.old.id).state, 'needs_review')
        self.assertEqual(db.session.get(SourceProposal,self.current.id).state, 'validated')
        self.assertEqual(self.subscription.department_ids, [])
        self.assertEqual(SourceReviewEvent.query.filter_by(action='superseded_by_configuration').count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(governance.source_config(self.source), self.original_config)
        self.assertEqual(current_version(self.case.school.id), before)
        self.assertEqual(self.old.validation_json, self.old_validation)

    def test_other_configuration_rejection_and_later_revision_are_not_resolved(self):
        different = governance.propose_source(self.case.school.id, dict(self.case.config,name='另一栏目名称'),
            department_id=self.source.id,origin='manual_review')
        rejected = governance.propose_source(self.case.school.id, self.case.config,
            department_id=self.source.id,origin='manual_review')
        governance.review_proposal(rejected.id, 'reject', self.user.id)
        governance.activate_proposal(self.current.id)
        self.assertEqual(self.old.state, 'superseded')
        self.assertEqual(different.state, 'proposed')
        self.assertEqual(rejected.state, 'rejected')
        later = governance.propose_source(self.case.school.id, self.case.config,
            department_id=self.source.id,origin='manual_review')
        governance.activate_proposal(self.current.id)
        self.assertEqual(later.state, 'proposed')
        self.assertEqual(different.state, 'proposed')
        self.assertEqual(rejected.state, 'rejected')


if __name__ == '__main__':
    unittest.main()
