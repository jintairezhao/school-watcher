"""Use provider-reported cache counts; historical omissions are unknown."""
import unittest
from backend.ai.providers import _usage


class ProviderCacheUsageTests(unittest.TestCase):
    def test_preserves_deepseek_counts_without_discounting_actual_tokens(self):
        value = _usage({'usage': {'prompt_tokens': 868679, 'completion_tokens': 132081,
                                 'prompt_cache_hit_tokens': 421120, 'prompt_cache_miss_tokens': 447559}})
        self.assertEqual(value['input_tokens'], 868679)
        self.assertEqual(value['total_tokens'], 1000760)
        self.assertEqual(value['cache_hit_tokens'], 421120)
        self.assertEqual(value['cache_miss_tokens'], 447559)

    def test_missing_cache_counts_stay_unknown(self):
        value = _usage({'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        self.assertNotIn('cache_hit_tokens', value)
        self.assertNotIn('cache_miss_tokens', value)

    def test_rejects_inconsistent_or_malformed_cache_breakdown(self):
        for hit, miss in [(101, 0), (10, 100), (-1, 101), (True, 99), ('10', 90), (10, None)]:
            with self.subTest(hit=hit, miss=miss):
                value = _usage({'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                                         'prompt_cache_hit_tokens': hit, 'prompt_cache_miss_tokens': miss}})
                self.assertEqual(value['total_tokens'], 110)
                self.assertNotIn('cache_hit_tokens', value)

    def test_reported_zero_hit_is_distinct_from_missing(self):
        value = _usage({'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                                 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100}})
        self.assertEqual(value['cache_hit_tokens'], 0)
        self.assertEqual(value['cache_miss_tokens'], 100)
