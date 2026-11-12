"""Batch accounting explains omitted sources without widening collection scope."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from test_inbox_refresh import InboxRefreshTests
from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask, Department, Subscription
from backend.services.inbox_refresh import queue_sources


class RefreshScopeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = InboxRefreshTests()
        self.fixture.setUp()
        self.client = self.fixture.client

    def tearDown(self):
        self.fixture.tearDown()

    def sync(self, payload=None):
        return self.client.post('/api/inbox/sync', json=payload or {'scope': 'all'},
                                headers={'X-CSRF-Token': 'token'}).get_json()

    def resume(self):
        return self.client.get('/api/inbox/refresh?resume=1').get_json()

    def recent(self, source, *, outcome='done', state='done'):
        ident = queue_sources([source], manual=True)[0]
        task = db.session.get(BackgroundTask, ident)
        task.state = state
        task.checked_at = task.finished_at = datetime.utcnow() - timedelta(minutes=2)
        task.next_run_at = datetime.utcnow() + timedelta(minutes=58)
        task.result = {'state': outcome, 'new_count': 0}
        if outcome == 'done' and state == 'done':
            source.last_scraped_at = task.finished_at
        db.session.commit()
        return task

    def test_recent_failed_attempt_is_visible_but_not_counted_as_success(self):
        f = self.fixture
        f.sub.department_ids = None
        f.a.list_selector = f.b.list_selector = 'li'
        db.session.commit()
        self.recent(f.a, outcome='waiting')
        data = self.sync()
        self.assertEqual(data['total'], 1)
        scope = data['scope_summary']
        self.assertEqual((scope['total'], scope['configured'], scope['batch_total']), (2, 2, 1))
        excluded = data['outside_batch'][0]
        self.assertEqual(excluded['id'], f.a.id)
        self.assertEqual(excluded['admission_reason'], 'recent_attempt')
        self.assertEqual(excluded['state'], 'retry_wait')
        self.assertEqual(data['done'], 0)
        with patch('backend.services.inbox_refresh.queue_sources', side_effect=AssertionError('Read only')):
            restored = self.resume()
        self.assertEqual(restored['outside_batch'], data['outside_batch'])
        self.assertEqual(restored['scope_summary'], scope)

    def test_zero_queued_sources_still_explain_successful_cache_and_retry_delay(self):
        f = self.fixture
        f.sub.department_ids = None
        db.session.commit()
        self.recent(f.a)
        self.recent(f.b, state='failed')
        data = self.sync()
        self.assertEqual((data['scheduled'], data['total'], data['done']), (0, 0, 0))
        self.assertTrue(data['tracked'])
        self.assertEqual({s['admission_reason'] for s in data['outside_batch']},
                         {'recent_success', 'retry_delay'})
        self.assertEqual(BackgroundTask.query.count(), 2)

    def test_legacy_batch_preserves_unknown_reason_instead_of_reconstructing_history(self):
        f = self.fixture
        f.sub.department_ids = None
        db.session.commit()
        self.recent(f.a)
        self.recent(f.b, outcome='waiting')
        AppConfig.set(f'inbox_refresh:user:{f.user.id}', json.dumps({
            'scope': 'all', 'department_ids': [f.a.id], 'started_at': '2026-11-09T12:00:00'}))
        data = self.resume()
        self.assertFalse(data['scope_summary']['recorded_at_admission'])
        self.assertEqual(data['scope_summary']['total'], 2)
        self.assertEqual(data['outside_batch'][0]['admission_reason'], 'unrecorded')
        self.assertEqual(data['outside_batch'][0]['state'], 'retry_wait')

    def test_unsubscription_removes_names_counts_and_reasons_from_restored_scope(self):
        f = self.fixture
        f.sub.department_ids = None
        db.session.commit()
        self.recent(f.a, outcome='waiting')
        self.sync()
        f.sub.department_ids = [f.b.id]
        db.session.commit()
        data = self.resume()
        self.assertEqual(data['outside_batch'], [])
        self.assertEqual(data['scope_summary']['total'], 1)
        self.assertNotIn(f.a.name, json.dumps(data, ensure_ascii=False))

    def test_local_request_accounts_only_for_requested_school_and_sources(self):
        f = self.fixture
        self.client = f.client_as(f.other.id)
        self.recent(f.a)
        data = self.sync({'scope': 'current', 'school_id': f.school.id, 'department_ids': [f.a.id]})
        self.assertEqual(data['scope_summary']['total'], 1)
        self.assertEqual([s['id'] for s in data['outside_batch']], [f.a.id])
        self.assertEqual([s['id'] for s in data['scope_summary']['schools']], [f.school.id])

    def test_concurrent_local_batch_keeps_global_exclusions_and_active_work(self):
        f = self.fixture
        self.client = f.client_as(f.other.id)
        self.recent(f.a, outcome='waiting')
        first = self.sync()
        self.assertEqual(first['scope_summary']['total'], 3)
        data = self.sync({'scope': 'current', 'school_id': f.school.id, 'department_ids': [f.b.id]})
        self.assertEqual(data['tracking']['scope'], 'all')
        self.assertEqual(data['scope_summary']['total'], 3)
        self.assertEqual([s['id'] for s in data['outside_batch']], [f.a.id])
        self.assertEqual({s['id'] for s in data['sources']}, {f.b.id, f.c.id})

    def test_all_89_configured_sources_are_accounted_for_and_manual_request_covers_all(self):
        f = self.fixture
        f.sub.department_ids = None
        f.a.list_selector = f.b.list_selector = 'li'
        db.session.add_all(Department(school_id=f.school.id, name=f'来源{i}',
            list_url=f'https://example.edu.cn/list/{i}', list_selector='li') for i in range(87))
        db.session.commit()
        self.recent(f.a, outcome='waiting')
        data = self.sync()
        self.assertEqual((data['scope_summary']['configured'], data['total'],
                          len(data['outside_batch'])), (89, 88, 1))
        self.assertEqual(data['scope_summary']['schools'][0]['configured'], 89)
        self.assertEqual(data['scope_summary']['schools'][0]['batch_configured'], 88)
        data = self.fixture.post({'scope': 'all'}).get_json()
        self.assertEqual((data['total'], data['scope_summary']['configured']), (89, 89))
        self.assertEqual(data['outside_batch'], [])

    def test_legacy_71_mixed_batch_does_not_masquerade_as_71_of_89_configured_sources(self):
        f = self.fixture
        self.client = f.client_as(f.other.id)
        f.a.list_selector = f.b.list_selector = 'li'
        configured = [f.a, f.b] + [Department(school_id=f.school.id, name=f'已启用{i}',
            list_url=f'https://example.edu.cn/configured/{i}', list_selector='li') for i in range(87)]
        pending = [Department(school_id=f.school.id, name=f'待识别{i}',
            list_url=f'https://example.edu.cn/pending/{i}') for i in range(18)]
        other = [f.c] + [Department(school_id=f.other_school.id, name=f'其他入口{i}',
            list_url=f'https://other.edu.cn/pending/{i}') for i in range(15)]
        db.session.add_all(configured + pending + other)
        db.session.commit()
        chosen = configured[:55] + pending[:14] + other[:2]
        AppConfig.set(f'inbox_refresh:user:{f.other.id}', json.dumps({
            'scope':'all', 'department_ids':[d.id for d in chosen]}))
        data = self.resume()
        self.assertEqual(data['total'], 71)
        self.assertEqual(data['scope_summary']['total'], 123)
        school = next(s for s in data['scope_summary']['schools'] if s['id'] == f.school.id)
        self.assertEqual((school['configured'], school['batch_configured'], school['batch_total']),
                         (89, 55, 69))
        self.assertEqual(sum(s['configured'] and s['school_id'] == f.school.id
                             for s in data['outside_batch']), 34)
        self.assertTrue(all(s['admission_reason'] == 'unrecorded' for s in data['outside_batch']))
        self.assertEqual(BackgroundTask.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
