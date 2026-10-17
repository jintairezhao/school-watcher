"""Audience coverage is actually wired into the durable exploration queue."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.services.discovery_policy import attention_summary, get_attention_policy, get_policy
from backend.services.source_inventory import Inventory


ROOT = 'https://example.edu.cn/'


class AttentionFrontierIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inventory.sqlite3'
        self.store = Inventory(self.path)
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.store.finish(self.key, ROOT, html='<title>测试大学</title>', state='fetched', status_code=200)

    def add(self, suffix, label, kind='channel'):
        url = ROOT + suffix
        self.store.enqueue(self.key, url, label, kind, 1, [], 'school_domain')
        return url

    def test_known_news_column_cannot_displace_unread_core_need(self):
        self.add('a-news', '综合新闻')
        funding = self.add('z-funding', '学生资助管理中心', 'unit')
        chosen = self.store.claim(self.key, focus='valuable')
        self.assertEqual(chosen['url'], funding)
        trace = self.store.exploration_choices(self.key)[0]
        self.assertEqual(trace['reason'], 'audience_coverage')
        self.assertIn('funding', trace['attention']['needs'])
        self.assertEqual(trace['candidate_count'], 2)
        self.assertEqual(trace['foraging']['cost_state'], 'unknown')
        self.assertEqual(trace['first_claim'], 1)

    def test_teacher_preferences_reach_production_claim_without_student_veto(self):
        funding = self.add('a-funding', '奖学金通知')
        personnel = self.add('z-personnel', '职称评审与教师发展', 'unit')
        app = Flask(__name__)
        app.config['DISCOVERY_ATTENTION'] = {'profiles': ['teacher']}
        with app.app_context():
            self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], personnel)
        self.store.finish(self.key, personnel, state='fetched')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], funding)

    def test_restart_preserves_work_and_does_not_make_profile_a_filter(self):
        funding = self.add('funding', '奖学金通知')
        low = self.add('utility-news', '校友动态')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], funding)
        self.store.finish(self.key, funding, state='fetched')
        restored = Inventory(self.path)
        self.assertEqual(restored.claim(self.key, focus='valuable')['url'], low)
        self.assertEqual(restored.exploration_choices(self.key)[0]['url'], funding)

    def test_saved_choice_is_latest_per_page_with_original_first_claim(self):
        funding = self.add('funding', '奖学金通知')
        self.store.claim(self.key, focus='valuable')
        with self.store.connect() as c:
            c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (self.key, funding))
        self.store.claim(self.key, focus='valuable')
        choices = Inventory(self.path).exploration_choices(self.key)
        self.assertEqual(len(choices), 1)
        self.assertEqual((choices[0]['first_claim'], choices[0]['last_claim']), (1, 2))

    def test_waiting_network_request_keeps_its_wait_despite_high_attention(self):
        funding = self.add('funding', '奖学金通知')
        other = self.add('news', '综合新闻')
        with self.store.connect() as c:
            c.execute("UPDATE pages SET retry_at='2999-01-01T00:00:00+00:00' WHERE url=?", (funding,))
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], other)
        self.assertEqual(self.store.get_page(self.key, funding)['state'], 'pending')


class AttentionPolicyTests(unittest.TestCase):
    def test_defaults_are_shared_audiences_and_equal_unvalidated_weights(self):
        with patch.dict(os.environ, {'WATCHER_DISCOVERY_ATTENTION': '{}'}):
            policy = get_attention_policy()
        self.assertEqual(set(policy['profiles']), {'undergraduate', 'graduate', 'teacher'})
        self.assertEqual(set(policy['weights'].values()), {1.0})

    def test_explicit_preferences_override_without_changing_fee_limits(self):
        app = Flask(__name__)
        app.config['DISCOVERY_ATTENTION'] = {'profiles': ['teacher'], 'weights': {'research': 3}}
        shared = {'profiles': ['graduate'], 'weights': {'funding': 2, 'research': 2}}
        with patch.dict(os.environ, {'WATCHER_DISCOVERY_ATTENTION': json.dumps(shared)}), app.app_context():
            before = get_policy()
            policy = get_attention_policy()
            self.assertEqual(policy['profiles'], ['teacher'])
            self.assertEqual(policy['weights']['research'], 3)
            self.assertEqual(policy['weights']['funding'], 2)
            self.assertEqual(get_policy(), before)
            self.assertEqual(before['ai_school_request_limit'], 40)

    def test_bad_preferences_fail_clearly_instead_of_silently_changing_goals(self):
        for value in ({'profiles': ['typo']}, {'weights': {'unknown': 2}},
                      {'weights': {'research': 0}}, {'weights': {'research': True}},
                      {'weights': {'research': float('inf')}}, {'something': 1}, []):
            with self.subTest(value=value), patch.dict(os.environ, {
                    'WATCHER_DISCOVERY_ATTENTION': json.dumps(value)}):
                with self.assertRaises(ValueError):
                    get_attention_policy()

    def test_reader_description_uses_the_same_active_profiles(self):
        app = Flask(__name__)
        app.config['DISCOVERY_ATTENTION'] = {'profiles': ['teacher']}
        with app.app_context():
            text = attention_summary()
        self.assertIn('教师', text)
        self.assertNotIn('本科生', text)
        self.assertNotIn('研究生', text)


if __name__ == '__main__':
    unittest.main()
