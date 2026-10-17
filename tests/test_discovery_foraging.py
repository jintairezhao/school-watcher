"""Path hints are bounded by real current graph evidence and source identity."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import unittest
from unittest.mock import patch

from backend.scraper.discovery.foraging import ForagingFeedback


ROOT = 'https://example.edu.cn/'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def page(suffix, kind, depth, *, state='fetched', **changes):
    url = ROOT + suffix
    return dict({'url': url, 'kind': kind, 'depth': depth, 'state': state,
                 'content_hash': digest(url), 'status_code': 200, 'error': '',
                 'health': 'healthy', 'label': suffix}, **changes)


def edge(parent, target, **changes):
    return dict({'parent_url': parent['url'], 'target_url': target['url'],
                 'decision': 'follow', 'content_hash': parent['content_hash'],
                 'locator': 'main nav a'}, **changes)


def receipt(source, ident=1, **changes):
    return dict({'source_id': ident, 'url': source['url'], 'content_hash': source['content_hash'],
                 'receipt_id': 'native:' + str(ident), 'config_hash': digest('config-' + str(ident)),
                 'region': {'container': '#notices', 'heading': '#notices h2'}}, **changes)


class DiscoveryForagingTests(unittest.TestCase):
    def setUp(self):
        self.root = page('', 'root', 0)
        self.directory = page('layout/', 'directory', 1)
        self.unit = page('college/', 'unit', 2)
        self.topic = page('opaque-study-topic/', 'gateway', 3)
        self.column = page('new-notices/', 'channel', 4)
        self.sibling = page('next-opaque-entry/', 'navigation', 4, state='pending')
        self.other = page('another-college/', 'unit', 2, state='pending')
        self.history = {row['url']: row for row in [self.root, self.directory, self.unit,
                                                   self.topic, self.column, self.sibling, self.other]}
        self.edges = [edge(self.root, self.directory), edge(self.directory, self.unit),
                      edge(self.directory, self.other), edge(self.unit, self.topic),
                      edge(self.topic, self.column), edge(self.topic, self.sibling)]

    def feedback(self, receipts=None, **changes):
        return ForagingFeedback(self.history, self.edges,
            receipts=[receipt(self.column)] if receipts is None else receipts, **changes)

    def test_opaque_intermediate_entry_inherits_its_actual_successful_local_path(self):
        feedback = self.feedback()
        info = feedback.features(self.sibling)
        self.assertTrue(info['path_credit'])
        self.assertEqual(info['scope_url'], self.unit['url'])
        self.assertEqual(info['verified_source_count'], 1)
        self.assertEqual(info['complete_material_count'], 3)
        self.assertEqual(info['observed_forward_entry_count'], 3)
        self.assertEqual(feedback.rank(self.sibling), (-Fraction(2, 4), 3))

    def test_trace_retains_real_root_directory_unit_topic_column_navigation(self):
        info = self.feedback().features(self.column)
        self.assertEqual([row['url'] for row in info['observed_path']],
            [self.root['url'], self.directory['url'], self.unit['url'],
             self.topic['url'], self.column['url']])
        self.assertEqual(info['path_reason'], 'root_reached')

    def test_one_college_success_cannot_credit_school_or_other_college(self):
        feedback = self.feedback()
        for row in (self.root, self.directory, self.other):
            with self.subTest(url=row['url']):
                self.assertFalse(feedback.features(row)['path_credit'])
                self.assertEqual(feedback.features(row)['verified_source_count'], 0)
        self.assertEqual(feedback.features(self.other)['complete_material_count'], 0)

    def test_duplicate_validations_and_more_notices_cannot_inflate_bounded_credit(self):
        original = self.feedback()
        repeated = [receipt(self.column), receipt(self.column, receipt_id='native:99')]
        self.history[self.column['url']].update(item_count=50000, notice_count=50000,
            feed_json='{"samples": ["many notices"]}', attempts=999999)
        duplicate = self.feedback(repeated)
        self.assertEqual(original.features(self.sibling), duplicate.features(self.sibling))
        self.assertEqual(original.rank(self.sibling), duplicate.rank(self.sibling))

    def test_same_page_separate_validated_columns_remain_two_sources_one_hint(self):
        feedback = self.feedback([receipt(self.column), receipt(self.column, 2,
            region={'container': '#admissions', 'heading': '#admissions h2'})])
        self.assertEqual(feedback.features(self.sibling)['verified_source_count'], 2)
        self.assertEqual(feedback.rank(self.sibling), self.feedback().rank(self.sibling))

    def test_reused_source_id_with_conflicting_receipts_is_not_credit(self):
        conflict = page('separate-column/', 'channel', 4)
        self.history[conflict['url']] = conflict
        self.edges.append(edge(self.topic, conflict))
        info = self.feedback([receipt(self.column), receipt(conflict)]).features(self.sibling)
        self.assertFalse(info['path_credit'])
        self.assertEqual(info['verified_source_count'], 0)

    def test_current_material_hash_is_required_for_receipt(self):
        for invalid in (receipt(self.column, content_hash=digest('old HTML')),
                        receipt(self.column, config_hash='bad'), receipt(self.column, receipt_id='draft:1'),
                        receipt(self.column, source_id=True), receipt(self.column, source_id=0),
                        receipt(self.column, region={}),
                        receipt(self.column, region={'container': '#x', 'heading': '#x h2', 'empty': True})):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.feedback([invalid]).features(self.sibling)['path_credit'])

    def test_stale_edges_cannot_transmit_path_credit(self):
        self.edges[-2]['content_hash'] = digest('old parent')
        info = self.feedback().features(self.sibling)
        self.assertFalse(info['path_credit'])
        path = self.feedback().features(self.column)
        self.assertEqual(path['observed_path'], [{'url': self.column['url'], 'kind': 'channel',
                                                 'label': self.column['label']}])
        self.assertEqual(path['path_reason'], 'no_current_forward_parent')

    def test_reference_edges_are_not_successful_navigation_paths(self):
        for decision in ('route_reference', 'article_reference', 'navigation_pending', 'external_review'):
            with self.subTest(decision=decision):
                changed = deepcopy(self.edges)
                changed[-2]['decision'] = decision
                feedback = ForagingFeedback(self.history, changed, [receipt(self.column)])
                self.assertFalse(feedback.features(self.sibling)['path_credit'])

    def test_official_external_observed_edge_is_allowed_without_host_prefix_guess(self):
        external = page('external-column/', 'channel', 4)
        external['url'] = 'https://official-other-host.example.org/opaque-notices/'
        external['content_hash'] = digest(external['url'])
        self.history[external['url']] = external
        self.edges.append(edge(self.topic, external, decision='official_external_link'))
        self.assertTrue(self.feedback([receipt(external)]).features(self.sibling)['path_credit'])

    def test_multiple_forward_parents_stop_the_path_before_borrowing_success(self):
        other = page('office/', 'unit', 2)
        self.history[other['url']] = other
        self.edges.extend([edge(self.directory, other), edge(other, self.column)])
        feedback = self.feedback()
        self.assertFalse(feedback.features(self.sibling)['path_credit'])
        self.assertEqual(feedback.features(self.column)['path_reason'], 'ambiguous_parents')
        self.assertEqual(feedback.features(self.column)['scope_url'], self.column['url'])
        self.assertEqual(feedback.features(self.column)['verified_source_count'], 1)

    def test_backlinks_cycles_and_self_links_are_never_forward_progress(self):
        control = self.feedback()
        self.edges.extend([edge(self.column, self.root), edge(self.column, self.topic),
                           edge(self.unit, self.unit), edge(self.column, self.directory)])
        changed = self.feedback()
        self.assertEqual(control.features(self.sibling), changed.features(self.sibling))

    def test_lateral_link_with_nonincreasing_depth_cannot_import_other_branch(self):
        other = page('office/', 'unit', 2)
        sibling = page('office-column/', 'channel', 3)
        self.history.update({other['url']: other, sibling['url']: sibling})
        self.edges.extend([edge(self.directory, other), edge(other, sibling), edge(self.topic, sibling)])
        feedback = self.feedback([receipt(sibling)])
        self.assertFalse(feedback.features(self.sibling)['path_credit'])
        self.assertTrue(feedback.features(other)['path_credit'])

    def test_unobserved_address_or_matching_url_prefix_has_no_invented_path(self):
        unobserved = page('college/unseen/', 'navigation', 3, state='pending')
        info = self.feedback().features(unobserved)
        self.assertFalse(info['path_credit'])
        self.assertEqual(info['observed_path'], [])
        self.assertEqual(info['path_reason'], 'page_not_observed')
        orphan = page('college/nested-but-no-link/', 'navigation', 3, state='pending')
        self.history[orphan['url']] = orphan
        self.assertFalse(self.feedback().features(orphan)['path_credit'])

    def test_kind_or_owner_name_cannot_turn_unobserved_relationship_into_credit(self):
        self.other['label'] = self.unit['label'] = '完全相同的学院名称'
        feedback = self.feedback()
        self.assertFalse(feedback.features(self.other)['path_credit'])

    def test_unexplored_branch_keeps_neutral_optimism(self):
        feedback = self.feedback([])
        self.assertEqual(feedback.rank(self.other), (-Fraction(1, 1), 0))
        self.assertLess(feedback.rank(self.other), feedback.rank(self.sibling))
        self.assertEqual(feedback.features(self.other)['verified_source_count'], 0)

    def test_success_hint_diminishes_as_distinct_complete_materials_accumulate(self):
        before = self.feedback().rank(self.sibling)
        for index in range(8):
            material = page('more-' + str(index), 'navigation', 4)
            self.history[material['url']] = material
            self.edges.append(edge(self.topic, material))
        after = self.feedback().rank(self.sibling)
        self.assertGreater(after, before)
        self.assertLess(self.feedback().rank(self.other), after)

    def test_normal_hash_refresh_does_not_multiply_material_or_credit(self):
        before = self.feedback().features(self.sibling)
        self.column['content_hash'] = digest('new ordinary contents')
        old_receipt = self.feedback([receipt(self.column, content_hash=digest(self.column['url']))])
        self.assertFalse(old_receipt.features(self.sibling)['path_credit'])
        refreshed = self.feedback([receipt(self.column, receipt_id='native:2')]).features(self.sibling)
        self.assertEqual(before, refreshed)

    def test_failed_requests_waits_and_attempts_have_no_value_or_cost_effect(self):
        baseline = self.feedback().features(self.sibling)
        for index, (state, status, health, error) in enumerate((
                ('failed', 502, 'unreachable', 'bad gateway'),
                ('pending', None, 'unreachable', 'timed out'),
                ('blocked', 403, 'dynamic_content', 'access_verification_required'),
                ('fetched', 200, 'dynamic_content', ''),
                ('fetched', 200, 'template_error', ''),
                ('fetched', 200, 'healthy', 'incomplete response'))):
            failed = page('failure-' + str(index), 'navigation', 4, state=state,
                          status_code=status, health=health, error=error, attempts=10000)
            self.history[failed['url']] = failed
            # Reads that failed cannot supply any trustworthy outgoing edge.
            self.edges.extend([edge(self.topic, failed), edge(failed, self.sibling)])
        info = self.feedback().features(self.sibling)
        self.assertEqual(info['path_credit'], baseline['path_credit'])
        self.assertEqual(info['complete_material_count'], baseline['complete_material_count'])
        self.assertEqual(info['cost_state'], 'unknown')
        self.assertIsNone(info['actual_tokens'])
        self.assertIsNone(info['elapsed_seconds'])

    def test_failed_or_incomplete_page_cannot_supply_a_positive_source_receipt(self):
        for update in ({'state': 'failed'}, {'state': 'pending'}, {'status_code': 502},
                       {'health': 'dynamic_content'}, {'error': 'incomplete'}, {'content_hash': ''}):
            history = deepcopy(self.history)
            history[self.column['url']].update(update)
            feedback = ForagingFeedback(history, self.edges, [receipt(self.column)])
            with self.subTest(update=update):
                self.assertFalse(feedback.features(self.sibling)['path_credit'])

    def test_dynamic_shells_use_the_same_complete_material_contract_as_attention(self):
        for update in ({'health': 'needs_adapter'}, {'health': 'partial'},
                       {'outcome': 'needs_adapter'}, {'outcome': 'requires_render'},
                       {'outcome': 'needs_manual'}, {'error_code': 'dynamic_material_incomplete'}):
            history = deepcopy(self.history)
            history[self.column['url']].update(update)
            feedback = ForagingFeedback(history, self.edges, [receipt(self.column)])
            with self.subTest(update=update):
                self.assertFalse(feedback.features(self.sibling)['path_credit'])
                self.assertEqual(feedback.features(self.sibling)['complete_material_count'], 2)
        for health in ('date_unknown', 'stale', 'infrequent'):
            history = deepcopy(self.history)
            history[self.column['url']]['health'] = health
            feedback = ForagingFeedback(history, self.edges, [receipt(self.column)])
            with self.subTest(health=health):
                self.assertTrue(feedback.features(self.sibling)['path_credit'])

    def test_bad_cached_branch_assignment_cannot_widen_proven_scope(self):
        fake = {url: self.unit['url'] for url in self.history}
        info = self.feedback(branches=fake).features(self.other)
        self.assertFalse(info['path_credit'])
        self.assertEqual(info['scope_url'], self.other['url'])

    def test_features_are_json_safe_explanations_not_ownership_or_cost_claims(self):
        import json
        info = self.feedback().features(self.sibling)
        self.assertEqual(json.loads(json.dumps(info)), info)
        self.assertNotIn('department_id', info)
        self.assertNotIn('estimated_tokens', info)

    def test_inputs_are_not_mutated_and_reconstruction_is_deterministic(self):
        before = deepcopy((self.history, self.edges))
        one, two = self.feedback(), self.feedback()
        self.assertEqual(before, (self.history, self.edges))
        self.assertEqual(one.features(self.sibling), two.features(self.sibling))
        self.assertEqual(one.rank(self.sibling), two.rank(self.sibling))

    def test_central_policy_parameters_can_change_only_the_auxiliary_hint(self):
        feedback = self.feedback(unseen_prior=3, path_credit_weight=2)
        self.assertEqual(feedback.rank(self.sibling), (-Fraction(5, 6), 3))
        self.assertEqual(feedback.features(self.sibling), self.feedback().features(self.sibling))
        for options in ({'unseen_prior': 0}, {'unseen_prior': True}, {'path_credit_weight': -1},
                        {'path_credit_weight': 1.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.feedback(**options)

    def test_ranking_never_builds_explanations_or_rescans_branch_edges(self):
        feedback = self.feedback()
        expected = feedback.rank(self.sibling)
        with patch.object(feedback, 'features', side_effect=AssertionError('No explanations in rank')), \
             patch.object(feedback, 'observed_path', side_effect=AssertionError('No trail in rank')):
            # Removing raw collections after construction demonstrates that
            # ranking reads only the per-scope aggregates and bounded credit.
            feedback.material, feedback.targets = {}, {}
            self.assertEqual(feedback.rank(self.sibling), expected)

    def test_precomputed_counts_match_distinct_current_graph_and_final_explanation(self):
        feedback = self.feedback()
        before = feedback.features(self.sibling)
        feedback.material, feedback.targets = {}, {}
        after = feedback.features(self.sibling)
        self.assertEqual(before, after)
        self.assertEqual(after['complete_material_count'], 3)
        self.assertEqual(after['observed_forward_entry_count'], 3)


if __name__ == '__main__':
    unittest.main()
