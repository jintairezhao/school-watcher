"""Confirmed model cost as a tie breaker, never a coverage or ownership claim."""
from collections import defaultdict
import math


COUNTS = ('actual_requests', 'confirmed_requests', 'confirmed_tokens', 'unknown_requests',
          'reserved_requests', 'reconciled_requests', 'timed_requests', 'untimed_requests')


def _valid(bucket):
    if not isinstance(bucket, dict):
        return False
    return all(type(bucket.get(key, 0)) is int and bucket.get(key, 0) >= 0 for key in COUNTS)


class CostFeedback:
    def __init__(self, scopes, costs=None):
        self.scopes = scopes
        self.buckets = defaultdict(lambda: {key: 0 for key in (*COUNTS, 'provider_active_seconds')})
        self.comparable = self.timed = False
        costs = costs if isinstance(costs, dict) else {}
        totals = costs.get('totals', {})
        if costs.get('scope') != 'current_school_round' or not _valid(totals):
            return
        pages = costs.get('by_page', {})
        if not isinstance(pages, dict):
            return
        complete = (costs.get('attribution_complete', True) is True
                    and _valid(costs.get('unattributed', {}))
                    and not costs.get('unattributed', {}).get('actual_requests'))
        assigned = tokens = 0
        timing_complete = not totals.get('untimed_requests', 0)
        for url, row in pages.items():
            if not _valid(row) or url not in scopes:
                complete = False
                continue
            bucket = self.buckets[scopes[url]]
            for key in COUNTS:
                bucket[key] += row.get(key, 0)
            assigned += row.get('actual_requests', 0)
            tokens += row.get('confirmed_tokens', 0)
            seconds = row.get('provider_active_seconds')
            valid_time = (type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0
                          and row.get('timed_requests', 0) == row.get('actual_requests', 0)
                          and not row.get('untimed_requests', 0))
            if valid_time:
                bucket['provider_active_seconds'] += seconds
            else:
                timing_complete = False
                bucket['untimed_requests'] += 1
        self.comparable = bool(complete and assigned == totals.get('actual_requests', 0)
            and tokens == totals.get('confirmed_tokens', 0)
            and not any(totals.get(key, 0) for key in ('unknown_requests', 'reserved_requests', 'reconciled_requests')))
        self.timed = self.comparable and timing_complete

    def rank(self, url):
        if not self.comparable or url not in self.scopes:
            return (0, 0)
        bucket = self.buckets[self.scopes[url]]
        return (bucket['confirmed_tokens'], bucket['provider_active_seconds'] if self.timed else 0)

    def features(self, url):
        bucket = self.buckets.get(self.scopes.get(url))
        return {'cost_state': 'confirmed' if self.comparable else 'partial' if bucket else 'unknown',
                'cost_comparison_state': 'confirmed' if self.comparable else 'incomplete',
                'actual_tokens': bucket['confirmed_tokens'] if bucket else 0 if self.comparable else None,
                'actual_requests': bucket['actual_requests'] if bucket else 0 if self.comparable else None,
                'provider_active_seconds': bucket['provider_active_seconds'] if bucket and not bucket['untimed_requests'] else
                    0 if self.timed and not bucket else None,
                # Summed provider activity can overlap across concurrent calls;
                # it is never advertised as elapsed school onboarding time.
                'elapsed_seconds': None}
