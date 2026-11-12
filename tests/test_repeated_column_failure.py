"""Unchanged zero-match list failures cannot buy repairs to unrelated selectors."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department
from backend.services import tasks
from tests.test_direct_onboarding import DirectOnboardingTests, HTML, URL, ai_column


class RepeatedColumnFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        no_provider = patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls'))
        no_provider.start()
        self.addCleanup(no_provider.stop)

    def outcome(self, column, decision, *, complete=True):
        return {'status': 'ready', 'columns': [column], 'decision_ids': [decision],
                'material_complete': complete}

    def test_changed_container_does_not_make_same_zero_match_list_new_evidence(self):
        from backend.services.source_onboarding import onboard_page
        missing = ai_column(list_selector='#missing li')
        changed = dict(missing, container_selector='body', heading_selector='h2',
                       content_selector='.another-body-rule')
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=[
                 self.outcome(missing, 'first'), self.outcome(changed, 'second'),
                 self.outcome(changed, 'third')]) as recognize:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL},
                                  fetcher=self.fixture.fetch)
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['error_code'], 'no_new_validation_evidence')
        self.assertIn('list_missing', result['issues'][0]['codes'])
        feedback = recognize.call_args_list[1].kwargs['feedback'][0]
        self.assertEqual(feedback['container_selector'], '#notices')
        self.assertEqual(feedback['heading_selector'], '#notices h2')
        self.assertEqual(feedback['content_selector'], 'article')
        self.assertEqual(feedback['execution']['list_item_count'], 0)
        self.assertEqual(Department.query.count(), 0)

    def test_changed_list_selector_keeps_real_correction_opportunity(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=[
                 self.outcome(ai_column(list_selector='#missing li'), 'first'),
                 self.outcome(ai_column(list_selector='#other-missing li'), 'second'),
                 self.outcome(ai_column(), 'third')]) as recognize:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL},
                                  fetcher=self.fixture.fetch)
        self.assertEqual(recognize.call_count, 3)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['new_count'], 3)
        self.assertFalse(result['issues'])

    def test_unread_material_slices_continue_before_repair_stops(self):
        from backend.services.source_onboarding import onboard_page
        missing = ai_column(list_selector='#missing li')
        changed = dict(missing, container_selector='body', heading_selector='h2')
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=[
                 self.outcome(missing, 'first'), self.outcome(changed, 'second', complete=False),
                 self.outcome(ai_column(), 'third')]) as recognize:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL},
                                  fetcher=self.fixture.fetch)
        self.assertEqual(recognize.call_count, 3)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['new_count'], 3)

    def test_restart_preserves_task_cursor_and_ledger_with_repeated_failure(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_pagination_checkpoint_upgrade import PaginationCheckpointUpgradeTests
        ledger = PaginationCheckpointUpgradeTests()
        ledger.fixture = self.fixture
        paid_before = ledger.paid_ledger()
        job = tasks.enqueue('onboard', 'repeated-list-restart',
                            {'school_id': self.fixture.school_id, 'url': URL})
        identity, generation = job.identity, job.generation
        handle = tasks.claim(capabilities=['http'])
        missing = ai_column(list_selector='#missing li')
        def defer(*args, **kwargs):
            tasks.defer(capability='directory', delay=0, reason='Synthetic worker handoff')
        first_call = True
        def recognize_then_defer(*args, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                return self.outcome(missing, 'first-paid')
            return defer(*args, **kwargs)
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=recognize_then_defer):
            with self.assertRaises(tasks.TaskDeferred) as deferred:
                onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        tasks.handoff(handle, deferred.exception)
        db.session.refresh(job)
        original_regions = {'columns': [missing], 'cursor': 1, 'decision_ids': ['original-paid']}
        job.checkpoint = dict(job.checkpoint, column_regions=deepcopy(original_regions),
                              school_round='original-round')
        db.session.commit()
        resumed = tasks.claim(capabilities=['directory'])
        changed = dict(missing, container_selector='body', heading_selector='h2')
        with tasks.execution_scope(resumed), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=[
                 self.outcome(changed, 'second-paid')]) as recognize:
            result = onboard_page(resumed['payload'], fetcher=self.fixture.fetch)
        tasks.finish(resumed, result)
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual(result['error_code'], 'no_new_validation_evidence')
        self.assertEqual((job.identity, job.generation), (identity, generation))
        self.assertEqual(job.checkpoint['column_regions'], original_regions)
        self.assertEqual(job.checkpoint['school_round'], 'original-round')
        self.assertEqual(ledger.ledger(), paid_before)
        self.assertEqual(job.checkpoint['column_validation']['progress']['decision_ids'],
                         ['first-paid', 'second-paid'])

    def test_failure_key_changes_when_material_validator_or_empty_selector_changes(self):
        from backend.services.source_onboarding import _repair_evidence_key
        from backend.services.source_agent import selector_observation, validation_feedback
        column = ai_column(list_selector='#missing li')
        observation = validation_feedback(column, ['list_missing'],
                                          execution=selector_observation(column, HTML))
        key = _repair_evidence_key([observation], 'original-material', 9)
        context_only = dict(observation, name='另一个模型名称', heading_selector='h2',
                            container_selector='body', date_selector='time')
        self.assertEqual(key, _repair_evidence_key([context_only], 'original-material', 9))
        for value, material, version in [
                (observation, 'new-material', 9), (observation, 'original-material', 10),
                (dict(observation, list_selector='#different li'), 'original-material', 9)]:
            self.assertNotEqual(key, _repair_evidence_key([value], material, version))

    def test_unmeasured_syntax_scope_and_body_failures_keep_complete_feedback(self):
        from backend.services.source_onboarding import _repair_evidence_key
        base = {'name': '通知公告', 'list_selector': '#missing li', 'container_selector': '#notices',
                'codes': ['list_missing'], 'execution': {'list_item_count': 0}}
        cases = [dict(base, codes=['column_identity_or_scope_unconfirmed']),
                 dict(base, codes=['article_sample_mismatch'], article_trials=[{'matched': False}]),
                 dict(base, codes=['article_material_incomplete']),
                 dict(base, execution={}),
                 dict(base, execution={'list_item_count': False}),
                 dict(base, execution={'list_item_count': 0, 'selector_errors': ['list_selector']}),
                 dict(base, sample_records=[{'url': URL+'item'}])]
        for row in cases:
            with self.subTest(row=row):
                self.assertNotEqual(_repair_evidence_key([row], 'page', 9),
                    _repair_evidence_key([dict(row, container_selector='body')], 'page', 9))


if __name__ == '__main__':
    unittest.main()
