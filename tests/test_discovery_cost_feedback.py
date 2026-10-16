"""Use actual invoices only within an existing audience/path scheduling tier."""
from copy import deepcopy
import unittest

from backend.scraper.discovery.cost_feedback import CostFeedback


A, B, C = ('https://example.edu.cn/' + part for part in ('one/', 'two/', 'unrelated/'))


def bucket(requests=1, tokens=100, *, seconds=2.0, unknown=0, reserved=0):
    return {'actual_requests': requests, 'confirmed_requests': requests - unknown,
            'confirmed_tokens': tokens, 'unknown_requests': unknown, 'reserved_requests': reserved,
            'reconciled_requests': 0, 'provider_active_seconds': seconds,
            'timed_requests': requests, 'untimed_requests': 0}


def costs():
    return {'scope': 'current_school_round', 'totals': bucket(2, 1000, seconds=10.0),
            'unattributed': bucket(0, 0, seconds=0),
            'by_page': {A: bucket(tokens=900, seconds=8), B: bucket(tokens=100, seconds=2)}}


class CostFeedbackTests(unittest.TestCase):
    def feedback(self, invoices=None, scopes=None):
        return CostFeedback(scopes or {A: A, B: B}, costs() if invoices is None else invoices)

    def test_lower_confirmed_cost_wins_without_inventing_a_reward_probability(self):
        f = self.feedback()
        self.assertLess(f.rank(B), f.rank(A))
        self.assertEqual(f.features(A)['actual_tokens'], 900)
        self.assertEqual(f.features(A)['provider_active_seconds'], 8)
        self.assertIsNone(f.features(A)['elapsed_seconds'])

    def test_exact_graph_assignment_aggregates_once_not_url_prefixes_or_branch_view(self):
        data = costs()
        data['by_branch'] = {A: bucket(2, 1000)}
        f = self.feedback(data, {A: A, B: A})
        self.assertEqual(f.features(A)['actual_tokens'], 1000)
        self.assertEqual(f.features(A)['actual_requests'], 2)

    def test_unknown_unattributed_reserved_and_reconciled_fees_are_not_cheap(self):
        for change in ('unknown', 'unattributed', 'reserved', 'reconciled', 'missing_page'):
            data = costs()
            if change == 'unknown':
                data['totals']['unknown_requests'] = data['by_page'][A]['unknown_requests'] = 1
            elif change == 'unattributed':
                data['unattributed'] = bucket()
            elif change == 'reserved':
                data['totals']['reserved_requests'] = 1
            elif change == 'reconciled':
                data['totals']['reconciled_requests'] = 1
            else:
                data['by_page'][C] = data['by_page'].pop(A)
            with self.subTest(change=change):
                f = self.feedback(data)
                self.assertEqual(f.rank(A), f.rank(B))
                self.assertEqual(f.features(B)['cost_comparison_state'], 'incomplete')

    def test_provider_duration_only_breaks_equal_token_cost_and_is_not_wall_time(self):
        data = costs();data['by_page'][A]['confirmed_tokens'] = 100
        data['totals']['confirmed_tokens'] = 200
        f = self.feedback(data)
        self.assertLess(f.rank(B), f.rank(A))
        data['by_page'][A]['untimed_requests'] = data['totals']['untimed_requests'] = 1
        self.assertEqual(self.feedback(data).rank(A), self.feedback(data).rank(B))

    def test_actual_cost_changes_only_equal_audience_path_tier_and_keeps_fair_turn(self):
        from backend.scraper.discovery.frontier import choose_route
        from tests.test_discovery_foraging import page, edge
        from backend.services.discovery_policy import get_attention_policy
        root = page('', 'root', 0)
        first = page('one/', 'unit', 1, state='pending', label='奖助信息', frontier_rowid=1)
        second = page('two/', 'unit', 1, state='pending', label='奖助信息', frontier_rowid=2)
        known = {p['url']: p for p in (root, first, second)}
        options = dict(history=known, parents={root['url']: root},
                       edges=[edge(root, first), edge(root, second)], attention=get_attention_policy())
        self.assertEqual(choose_route([first, second], **options)['url'], A)
        self.assertEqual(choose_route([first, second], costs=costs(), **options)['url'], B)
        self.assertEqual(choose_route([first, second], claim_count=4, costs=costs(), **options)['url'], A)

    def test_old_history_and_invalid_costs_do_not_change_priority(self):
        for data in ({}, dict(costs(), scope='recorded_school_history')):
            self.assertEqual(self.feedback(data).rank(A), self.feedback(data).rank(B))
        data = deepcopy(costs());data['by_page'][A]['confirmed_tokens'] = -1
        self.assertEqual(self.feedback(data).rank(A), self.feedback(data).rank(B))

    def test_missing_historical_execution_details_cannot_masquerade_as_low_round_cost(self):
        data = dict(costs(), attribution_complete=False)
        f = self.feedback(data)
        self.assertEqual(f.rank(A), f.rank(B))
        self.assertEqual(f.features(A)['cost_comparison_state'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
