"""Admin cleanup routes use isolated data and never clear the actual profile."""
from datetime import datetime
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import test_storage_management as fixtures
from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, UserAnnouncementState


class StorageControlsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StorageManagementTests()
        self.fixture.setUp()
        self.client = self.fixture.client
        self.headers = self.fixture.headers

    def tearDown(self):
        self.fixture.tearDown()

    def preview(self, **extra):
        return self.client.post('/api/storage/notices/preview', json={
            'school_id': self.fixture.school.id, 'before': '2021-01-01', **extra}, headers=self.headers)

    def article(self, suffix):
        return self.fixture.article(suffix, published_at=datetime(2020, 1, 1))

    def test_busy_space_statistics_keep_storage_page_and_other_controls_available(self):
        from backend.services.database_space import DatabaseSpaceError, BUSY_MESSAGE
        with patch('backend.services.database_space.database_stats',
                   side_effect=DatabaseSpaceError(BUSY_MESSAGE, 409)):
            response = self.client.get('/api/storage')
            self.assertEqual(response.status_code, 200)
            space = response.get_json()['database_space']
            self.assertFalse(space['available'])
            self.assertFalse(space['can_compact'])
            self.assertEqual(space['message'], BUSY_MESSAGE)
            page = self.client.get('/admin/storage')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'noticeCleanupForm', page.data)
        self.assertTrue(self.client.get('/api/storage').get_json()['database_space']['supported'])

    def test_all_new_operations_require_administrator_and_csrf(self):
        reader = self.fixture.client_as(self.fixture.reader.id)
        for endpoint in ['notices/preview', 'notices/cleanup', 'database/compact']:
            url = '/api/storage/' + endpoint
            self.assertEqual(reader.post(url, json={}, headers=self.headers).status_code, 403)
            self.assertEqual(self.client.post(url, json={}).status_code, 403)

    def test_preview_then_cleanup_keeps_other_users_saved_notice_and_catalog(self):
        ordinary, saved = self.article('ordinary'), self.article('saved')
        ordinary_id, saved_id = ordinary.id, saved.id
        db.session.add(UserAnnouncementState(user_id=self.fixture.reader.id, announcement_id=saved_id, starred=True))
        db.session.add(Subscription(user_id=self.fixture.reader.id, school_id=self.fixture.school.id))
        db.session.commit()
        response = self.preview()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((response.json['matched_count'], response.json['deletable_count'], response.json['protected_count']), (2, 1, 1))
        self.assertEqual(Announcement.query.count(), 2, 'Preview must not remove records')
        result = self.client.post('/api/storage/notices/cleanup', json={
            'preview_token': response.json['preview_token']}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['deleted_count'], 1)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Announcement, ordinary_id))
        self.assertIsNotNone(db.session.get(Announcement, saved_id))
        self.assertEqual((School.query.count(), Department.query.count(), Subscription.query.count()), (1, 1, 1))

    def test_favorite_change_requires_new_preview_and_removes_nothing(self):
        ann = self.article('newly-saved')
        token = self.preview().json['preview_token']
        db.session.add(UserAnnouncementState(user_id=self.fixture.reader.id, announcement_id=ann.id, starred=True))
        db.session.commit()
        result = self.client.post('/api/storage/notices/cleanup', json={'preview_token': token}, headers=self.headers)
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(Announcement.query.count(), 1)

    def test_scope_and_confirmation_are_validated(self):
        self.article('validation')
        for payload in [None, [], {}, {'preview_token': ''}, {'preview_token': 'forged'},
                        {'preview_token': 'forged', 'school_id': self.fixture.school.id}]:
            response = self.client.post('/api/storage/notices/cleanup', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
        for values in [{'before': '2021-99-12'}, {'school_id': True}, {'school_id': -1}, {'unexpected': 'ignored?'}]:
            response = self.preview(**values)
            self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(Announcement.query.count(), 1)

    def test_usage_exposes_space_and_classifies_nondefault_database_and_external_catalog(self):
        self.article('usage')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'custom-source.sqlite3'
            path.write_bytes(b'catalog')
            versions = path.parent / 'catalog-generations'
            versions.mkdir()
            (versions / 'old.sqlite3').write_bytes(b'old-version')
            self.fixture.app.config['SOURCE_CATALOG_PATH'] = str(path)
            with patch('backend.services.database_space.compact_database', side_effect=AssertionError('GET must be read-only')):
                response = self.client.get('/api/storage')
            self.assertEqual(response.status_code, 200, response.json)
            self.assertIn('database_space', response.json)
            self.assertIn('supported', response.json['database_space'])
            self.assertEqual(response.json['notice_schools'], [{'id': self.fixture.school.id, 'name': self.fixture.school.name}])
            self.assertEqual(len([f for f in response.json['files'] if f['kind'] == 'main_database' and f['name'] == 'main.db']), 1)
            self.assertEqual(sum(f['bytes'] for f in response.json['files'] if f['kind'] == 'source_catalog'), len(b'catalogold-version'))
            self.assertFalse(any('_path' in f for f in response.json['files']))
            page = self.client.get('/admin/storage')
            self.assertEqual(page.status_code, 200)
            self.assertIn('data-storage-group="通知与目录"', page.get_data(as_text=True))
            self.assertEqual(sum(response.json['usage_groups'].values()), response.json['total_bytes'])

    def test_compaction_route_does_not_request_notice_deletion(self):
        self.article('retained')
        with patch('backend.services.database_space.compact_database', return_value={
                'reclaimed_bytes': 4096, 'before_bytes': 8192, 'after_bytes': 4096}) as compact:
            response = self.client.post('/api/storage/database/compact', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['reclaimed_bytes'], 4096)
        self.assertEqual(compact.call_count, 1)
        self.assertEqual(Announcement.query.count(), 1)
        response = self.client.post('/api/storage/database/compact', json={'path': '/untrusted'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
