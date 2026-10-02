"""Analytics must reflect executions (including failures), not estimate paid usage."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import test_shared_summaries as fixture_module
from backend.ai.models import AIExecution, AIBudget
from backend.ai.usage import usage_dashboard
from backend.database.db import db
from backend.database.models import AppConfig


def execution(key, at, *, tokens=100, status='succeeded', purpose='directory',
              provider='deepseek', model='deepseek-chat', skill_id='fixture', usage=None):
    return AIExecution(execution_id=key, created_at=at, finished_at=at + timedelta(seconds=2),
                       purpose=purpose, provider=provider, model=model, config_version='1',
                       skill_id=skill_id, skill_version='1', skill_digest='digest', input_digest='input',
                       mode='test', status=status, output={'private': 'never-return-this-output'},
                       usage=usage if usage is not None else {'known': True, 'total_tokens': tokens,
                           'input_tokens': tokens * 3 // 4, 'output_tokens': tokens - tokens * 3 // 4})


class AIUsageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.SharedSummaryTests()
        self.fixture.setUp()
        self.now = datetime(2026, 9, 29, 17, 0)

    def tearDown(self):
        self.fixture.tearDown()

    def test_totals_include_billed_failures_and_tests_without_double_counting(self):
        db.session.add_all([
            execution('one', self.now, tokens=100),
            execution('failed', self.now, tokens=40, status='failed'),
            execution('test', self.now, tokens=12, purpose='summary', skill_id='connection-test'),
            execution('summary', self.now, tokens=20, purpose='summary'),
            AIBudget(key='total:2026-09', used_tokens=172, reserved_tokens=900),
            AIBudget(key='directory:2026-09', used_tokens=140, reserved_tokens=900),
            AIBudget(key='summary:2026-09', used_tokens=32),
        ])
        db.session.commit()
        result = usage_dashboard(now=self.now)
        self.assertEqual(result['summary']['tokens'], 172)
        self.assertEqual(result['summary']['calls'], 4)
        self.assertEqual(result['summary']['success_rate'], 75)
        self.assertEqual({r['key']: r['tokens'] for r in result['purposes']},
                         {'directory': 140, 'summary': 20, 'test': 12})
        self.assertEqual(result['budgets'][0]['reserved_tokens'], 900)
        self.assertNotIn('never-return-this-output', str(result))
        self.assertNotIn('input_digest', str(result))

    def test_unknown_is_not_zero_and_pending_is_not_a_failure(self):
        db.session.add_all([
            execution('ok-unknown', self.now, usage={'known': False}),
            execution('uncertain', self.now, status='uncertain', usage={'known': False}),
            execution('sending', self.now, status='sending', usage={}),
            execution('reserved', self.now, status='reserved', usage={}),
        ])
        db.session.commit()
        summary = usage_dashboard(now=self.now)['summary']
        self.assertEqual(summary['calls'], 3)
        self.assertEqual(summary['tokens'], 0)
        self.assertEqual(summary['pending'], 1)
        self.assertEqual(summary['uncertain'], 1)
        self.assertEqual(summary['unknown_usage'], 2)
        self.assertEqual(summary['success_rate'], 100)

    def test_local_midnight_range_and_year_boundary_zero_filled(self):
        today_utc = datetime(2026, 9, 29, 16)
        first_utc = today_utc - timedelta(days=6)
        db.session.add_all([
            execution('today', today_utc),
            execution('yesterday', today_utc - timedelta(seconds=1), tokens=200),
            execution('first', first_utc, tokens=300),
            execution('before-range', first_utc - timedelta(seconds=1), tokens=400),
            execution('too-old', today_utc - timedelta(days=365), tokens=999),
            execution('future', self.now + timedelta(seconds=1), tokens=999),
        ])
        db.session.commit()
        result = usage_dashboard(7, 480, now=self.now)
        self.assertEqual(result['end'], '2026-09-30')
        self.assertEqual(result['start'], '2026-09-24')
        self.assertEqual(len(result['calendar']), 365)
        self.assertEqual(len(result['daily']), 7)
        self.assertEqual(result['daily'][-1]['tokens'], 100)
        self.assertEqual(result['daily'][-2]['tokens'], 200)
        self.assertEqual(result['daily'][0]['tokens'], 300)
        self.assertEqual(result['summary']['tokens'], 600)
        self.assertEqual(sum(d['tokens'] for d in result['calendar']), 1000)
        self.assertEqual(result['summary']['peak_date'], '2026-09-24')
        self.assertEqual(result['daily'][1]['calls'], 0)

    def test_model_history_separates_providers_and_reconciled_totals(self):
        db.session.add_all([
            execution('a', self.now, model='same-model', tokens=100),
            execution('b', self.now, model='same-model', provider='dashscope',
                      usage={'known': True, 'total_tokens': 55, 'source': 'administrator_reconciliation'}),
        ])
        db.session.commit()
        result = usage_dashboard(now=self.now)
        self.assertEqual(len(result['models']), 2)
        self.assertEqual(result['summary']['unclassified_tokens'], 55)
        self.assertEqual(sum(m['tokens'] for m in result['models']), 155)

    def test_lifetime_includes_history_before_calendar_without_reserved_or_future_calls(self):
        old = self.now - timedelta(days=800)
        db.session.add_all([
            execution('old', old, tokens=700),
            execution('old-failed', old + timedelta(days=1), tokens=30, status='failed'),
            execution('old-unknown', old + timedelta(days=2), status='uncertain', usage={'known': False}),
            execution('today', self.now, tokens=20),
            execution('reserved', self.now, status='reserved', tokens=999),
            execution('future', self.now + timedelta(seconds=1), tokens=999),
        ])
        db.session.commit()
        result = usage_dashboard(7, 480, now=self.now)
        lifetime = result['lifetime']
        self.assertEqual(lifetime['tokens'], 750)
        self.assertEqual(lifetime['calls'], 4)
        self.assertEqual(lifetime['unknown_usage'], 1)
        self.assertEqual(lifetime['start'], (old + timedelta(hours=8)).date().isoformat())
        self.assertEqual(lifetime['end'], '2026-09-30')
        self.assertEqual(result['summary']['tokens'], 20)
        self.assertEqual(sum(day['tokens'] for day in result['calendar']), 20)

    def test_calendar_has_exactly_365_local_days_across_leap_day(self):
        result = usage_dashboard(now=datetime(2024, 3, 1, 8), offset_minutes=480)
        self.assertEqual(len(result['calendar']), 365)
        self.assertEqual(result['calendar'][0]['date'], '2023-03-03')
        self.assertEqual(result['calendar'][-2]['date'], '2024-02-29')
        self.assertEqual(result['calendar'][-1]['date'], '2024-03-01')
        self.assertEqual(result['lifetime']['tokens'], 0)
        self.assertIsNone(result['lifetime']['start'])

    def test_budgets_use_current_utc_month_and_do_not_invent_limits(self):
        db.session.add_all([
            AIBudget(key='total:2026-09', used_tokens=999, reserved_tokens=12),
            AIBudget(key='total:2026-08', used_tokens=123456),
            AppConfig(key='ai_token_limit_total', value='1000'),
        ])
        db.session.commit()
        result = usage_dashboard(now=self.now)
        self.assertEqual(result['summary']['tokens'], 0)
        self.assertEqual(result['budgets'][0]['used_tokens'], 999)
        self.assertEqual(result['budgets'][0]['limit'], 1000)
        self.assertEqual(result['budgets'][1]['limit'], 0)

    def test_malformed_historic_usage_does_not_crash_or_invent_negative_tokens(self):
        db.session.add_all([
            execution('bad', self.now, usage={'known': True, 'total_tokens': -1}),
            execution('string', self.now, usage={'known': True, 'total_tokens': '90'}),
            execution('overflow', self.now, usage={'known': True, 'total_tokens': 20, 'input_tokens': 200}),
        ])
        db.session.commit()
        result = usage_dashboard(now=self.now)
        self.assertEqual(result['summary']['tokens'], 20)
        self.assertEqual(result['summary']['unknown_usage'], 2)
        self.assertEqual(result['summary']['unclassified_tokens'], 20)

    def test_empty_api_permissions_validation_and_no_paid_requests(self):
        client = self.fixture.client(self.fixture.admin)
        with patch('backend.ai.providers.complete', side_effect=AssertionError('must not call provider')):
            response = client.get('/api/admin/ai/usage?days=7&offset=480')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            result = response.get_json()
            self.assertEqual(len(result['daily']), 7)
            self.assertEqual(result['summary']['tokens'], 0)
            self.assertIsNone(result['summary']['success_rate'])
            for query in ('days=8', 'days=bad', 'offset=900', 'offset=-841'):
                self.assertEqual(client.get('/api/admin/ai/usage?' + query).status_code, 400)
        self.assertEqual(self.fixture.client().get('/api/admin/ai/usage').status_code, 401)
        self.assertEqual(self.fixture.client(self.fixture.reader).get('/api/admin/ai/usage').status_code, 403)

    def test_dedicated_page_is_admin_only_and_settings_stay_separate(self):
        client = self.fixture.client(self.fixture.admin)
        page = client.get('/admin/ai-usage')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="aiUsagePanel"', page.data)
        self.assertIn(b'aria-current="page">API', page.data)
        self.assertNotIn(b'id="aiProfileForm"', page.data)
        settings = client.get('/admin')
        self.assertIn(b'id="aiProfileForm"', settings.data)
        self.assertNotIn(b'id="aiUsagePanel"', settings.data)
        self.assertEqual(self.fixture.client().get('/admin/ai-usage').status_code, 403)
        self.assertEqual(self.fixture.client(self.fixture.reader).get('/admin/ai-usage').status_code, 403)


if __name__ == '__main__':
    unittest.main()
