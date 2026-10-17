"""A better first batch must never reorder an already paid decision sequence."""
import copy
import unittest
from unittest.mock import patch

from backend.services.discovery_policy import get_policy
from backend.scraper.discovery.layered import (
    append_navigation_evidence, assist, legacy_navigation_decision, navigation_candidates,
    ordered_navigation, route_structure,
)

ROOT = 'https://example.edu.cn/'
PAGE = {'url': ROOT, 'kind': 'root', 'discovery_policy': 'valuable'}


def candidates():
    labels = ['图书馆数据库', '综合新闻', '校友动态', '探索入口', '普通栏目',
              '普通导航', '更多资源', '其他入口', '组织机构', '院系设置']
    return [({'url': ROOT + str(index), 'label': label, 'kind': 'navigation',
              'path': [], 'locator': '#nav-' + str(index)}, label)
            for index, label in enumerate(labels)]


class NavigationOrderCheckpointTests(unittest.TestCase):
    def test_new_sequence_prioritizes_layout_and_preserves_every_original_candidate(self):
        original = candidates()
        before = copy.deepcopy(original)
        checkpoint = {}
        ordered = ordered_navigation(original, PAGE, {'nodes': []}, checkpoint, get_policy(), 1)
        self.assertEqual([pair[0]['label'] for pair in ordered[:2]], ['院系设置', '组织机构'])
        self.assertEqual(original, before)
        self.assertEqual({id(pair) for pair in ordered}, {id(pair) for pair in original})
        self.assertEqual(len(checkpoint['route_candidate_order']), len(original))

    def test_saved_order_survives_changed_preferences_without_rewriting_input(self):
        original, checkpoint = candidates(), {}
        first = ordered_navigation(original, PAGE, {'nodes': []}, checkpoint, get_policy(), 1)
        with patch('backend.services.discovery_policy.get_attention_policy',
                   side_effect=AssertionError('Saved paid order must not be recomputed')):
            second = ordered_navigation(original, PAGE, {'nodes': []}, checkpoint, get_policy(), 1)
        self.assertEqual(first, second)

    def test_old_material_cursor_and_unknown_plans_keep_original_dom_sequence(self):
        for checkpoint in ({'route_material_hash': 'old'}, {'route_cursor': 3},
                           {'route_pending_decision': {'fingerprint': 'possibly-paid'}}):
            original = candidates()
            with self.subTest(checkpoint=checkpoint), patch(
                    'backend.services.discovery_policy.get_attention_policy',
                    side_effect=AssertionError('Legacy input must remain unchanged')):
                self.assertIs(ordered_navigation(original, PAGE, {}, checkpoint, get_policy(), 1), original)

    def test_later_legacy_unknown_batch_blocks_reordering_of_a_new_sequence(self):
        original, checkpoint = candidates(), {}
        with patch('backend.scraper.discovery.layered.legacy_navigation_decision',
                   return_value={'batch_offset': 3}) as recover:
            self.assertIs(ordered_navigation(original, PAGE, {}, checkpoint, get_policy(), 1), original)
        self.assertEqual(recover.call_args.kwargs['batch_size'], 8)
        self.assertNotIn('route_candidate_order', checkpoint)
        self.assertIn('route_legacy_unsettled', checkpoint)


class ActualUnsettledBatchTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_byte_limited_old_batch_at_non_multiple_offset_is_recovered_without_dispatch(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        from backend.database.db import db
        original = candidates()
        evidence = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        for link, context in original[3:7]:
            append_navigation_evidence(evidence, link, context)
        skill = load_skill('student-information', 'navigation')
        row = AIExecution(execution_id='routes:older-unsettled', purpose='directory',
            config_version='1', provider='deepseek', model='test-only',
            skill_id=skill.id, skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': evidence, 'repair_feedback': None}), mode='navigation',
            status='uncertain', request_sent=True, usage={'known': False}, reserved_tokens=200)
        db.session.add(row)
        db.session.commit()
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No model dispatch')):
            found = legacy_navigation_decision(original, self.fixture.school_id,
                                               get_policy()['evidence_max_bytes'], batch_size=8)
            self.assertEqual(found['batch_offset'], 3)
            self.assertEqual(found['batch_count'], 4)
            self.assertEqual(found['evidence'], evidence)
            self.assertIs(ordered_navigation(original, PAGE, {}, {}, get_policy(),
                                            self.fixture.school_id), original)
        self.assertEqual(AIExecution.query.count(), 1)
        self.assertEqual(row.status, 'uncertain')
        # A confirmed unpaid failure does not permanently block new priorities.
        row.status, row.request_sent, row.usage = 'failed', False, {'known': True, 'total_tokens': 0}
        db.session.commit()
        self.assertIsNone(legacy_navigation_decision(original, self.fixture.school_id,
            get_policy()['evidence_max_bytes'], batch_size=8))

    def test_assist_cannot_buy_a_larger_batch_over_an_old_unknown_subset(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        from backend.database.db import db
        from backend.scraper.discovery.structure import extract_structure
        html = '<nav>' + ''.join(f'<a href="/{index}/">真实入口{index}</a>'
                                for index in range(16)) + '</nav>'
        page = dict(PAGE, label='测试大学', depth=0, path_json='[]')
        parsed = route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT,
                                 policy='valuable')
        original = navigation_candidates(html, parsed, page)
        evidence = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        for link, context in original[3:6]:
            append_navigation_evidence(evidence, link, context)
        skill = load_skill('student-information', 'navigation')
        old = AIExecution(execution_id='routes:old-paid-subset', purpose='directory',
            config_version='1', provider='deepseek', model='test-only',
            skill_id=skill.id, skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': evidence, 'repair_feedback': None}), mode='navigation',
            status='uncertain', request_sent=True, usage={'known': False}, reserved_tokens=200)
        db.session.add(old)
        db.session.commit()
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Do not buy overlapping unknown input')) as model:
            result = assist({'root_url': ROOT}, page, html, parsed)
            second = assist({'root_url': ROOT}, page, html, parsed)
        self.assertEqual(result['error_code'], 'ai_result_unknown')
        self.assertEqual(second['error_code'], 'ai_result_unknown')
        self.assertEqual(result['waiting_for_execution'], old.execution_id)
        self.assertEqual(result['remaining_routes'], len(original))
        self.assertNotIn('route_cursor', handle['checkpoint'])
        self.assertEqual(handle['checkpoint']['route_legacy_unsettled']['batch_offset'], 3)
        self.assertEqual(model.call_count, 0)
        self.assertEqual(AIExecution.query.count(), 1)
        self.assertEqual(old.status, 'uncertain')
        # A changed page title/context cannot invalidate the saved billing
        # identity and purchase the same unknown work again.
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Changed context is not settlement')) as model:
            changed = assist({'root_url': ROOT}, page, '<title>页面标题变化</title>' + html, parsed)
        self.assertEqual(changed['waiting_for_execution'], old.execution_id)
        self.assertEqual(model.call_count, 0)
        # Missing billing metadata after a partial restore also stays unknown.
        db.session.delete(old)
        db.session.commit()
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Missing row is not settlement')) as model:
            missing = assist({'root_url': ROOT}, page, html, parsed)
        self.assertEqual(missing['error_code'], 'ai_result_unknown')
        self.assertEqual(model.call_count, 0)

    def test_new_job_for_changed_context_respects_exact_page_execution_metadata(self):
        from backend.ai.models import AIExecution
        from backend.database.db import db
        row = AIExecution(execution_id='connection-resume:old-page-context', purpose='directory',
            config_version='1', provider='deepseek', model='test-only', skill_id='student-information',
            skill_version='1.2.0', skill_digest='old-prompt', input_digest='unreconstructable-old-input',
            mode='navigation', status='uncertain', request_sent=True, usage={'known': False},
            reserved_tokens=200, diagnostics={'discovery_context': {
                'school_id': self.fixture.school_id, 'page_url': ROOT}})
        db.session.add(row)
        db.session.commit()
        checkpoint, original = {}, candidates()
        self.assertIs(ordered_navigation(original, PAGE, {}, checkpoint, get_policy(),
                                        self.fixture.school_id), original)
        self.assertEqual(checkpoint['route_legacy_unsettled']['execution_id'], row.execution_id)
        # The complete alias, not a fabricated routes: prefix, survives a new
        # title and the next checkpoint restore.
        self.assertIs(ordered_navigation(original, dict(PAGE, title='新标题'), {}, checkpoint,
                                        get_policy(), self.fixture.school_id), original)
        self.assertEqual(checkpoint['route_legacy_unsettled']['execution_id'], row.execution_id)
        # Explicit known-unpaid completion permits the original progress to
        # continue; no marker or cursor is used to claim that work was done.
        row.status, row.request_sent, row.usage = 'failed', False, {'known': True, 'total_tokens': 0}
        db.session.commit()
        ordered = ordered_navigation(original, PAGE, {}, checkpoint, get_policy(), self.fixture.school_id)
        self.assertEqual(ordered[0][0]['label'], '院系设置')
        self.assertNotIn('route_legacy_unsettled', checkpoint)

    def test_legacy_alias_without_context_is_recovered_by_its_original_input(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        from backend.database.db import db
        original = candidates()
        evidence = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        for link, context in original[2:5]:
            append_navigation_evidence(evidence, link, context)
        skill = load_skill('student-information', 'navigation')
        row = AIExecution(execution_id='connection-resume:legacy-no-context', purpose='directory',
            config_version='1', provider='deepseek', model='test-only', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': evidence, 'repair_feedback': None}),
            mode='navigation', status='uncertain', request_sent=True, usage={'known': False})
        db.session.add(row)
        db.session.commit()
        checkpoint = {}
        self.assertIs(ordered_navigation(original, PAGE, {}, checkpoint, get_policy(),
                                        self.fixture.school_id), original)
        marker = checkpoint['route_legacy_unsettled']
        self.assertEqual(marker['execution_id'], row.execution_id)
        self.assertEqual(marker['batch_offset'], 2)

    def test_ordinary_layered_unknown_wait_is_not_a_permanent_dispatch_plan(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        from backend.database.db import db
        from backend.scraper.discovery.structure import extract_structure
        html = '<nav><a href="/opaque/">真实入口</a></nav>'
        page = dict(PAGE, discovery_policy='layered', label='测试大学', path_json='[]', depth=0)
        parsed = route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT, policy='layered')
        evidence = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        for link, context in navigation_candidates(html, parsed, page):
            append_navigation_evidence(evidence, link, context)
        skill = load_skill('student-information', 'navigation')
        row = AIExecution(execution_id='connection-resume:ordinary-layered', purpose='directory',
            config_version='1', provider='deepseek', model='test-only', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': evidence, 'repair_feedback': None}),
            mode='navigation', status='uncertain', request_sent=True, usage={'known': False})
        db.session.add(row)
        db.session.commit()
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Keep unknown alias')):
            waiting = assist({'root_url': ROOT}, page, html, parsed)
        self.assertEqual(waiting['waiting_for_execution'], row.execution_id)
        self.assertNotIn('route_pending_decision', handle['checkpoint'])
        row.status, row.request_sent, row.usage = 'failed', False, {'known': True, 'total_tokens': 0}
        db.session.commit()
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'failed', 'error_code': 'known_unpaid_failure'}) as model:
            continued = assist({'root_url': ROOT}, page, html, parsed)
        self.assertEqual(model.call_count, 1)
        self.assertNotEqual(continued.get('error_code'), 'ai_result_unknown')
        self.assertNotIn('route_legacy_unsettled', handle['checkpoint'])


if __name__ == '__main__':
    unittest.main()
