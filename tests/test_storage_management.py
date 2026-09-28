"""Storage administration and additive restores only touch isolated fixture data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementSource, AppConfig, Department,
    DepartmentDirectoryEntry, School, ScrapeLog, Subscription, User, UserAnnouncementState)
from backend.services.content_cache import prune_content
from backend.services.data_transfer import export_data, read_backup, merge_data, FORMAT
from backend.services.storage_policy import policy, save_policy


def packed(data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('data.json', json.dumps(data, ensure_ascii=False))
    stream.seek(0)
    return stream


class StorageManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'storage-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(self.root / 'main.db'),
            'SOURCE_CATALOG_PATH': str(self.root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(self.root / 'discovery.db'),
            'STORAGE_ROOT': str(self.root), 'BACKUP_DIR': str(self.root / 'backups'), 'BACKUP_COPY_DIR': ''})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.admin = User(username='admin', password_hash='secret-hash', role='admin')
        self.reader = User(username='reader', password_hash='reader-hash')
        self.school = School(name='示例学校', url='https://example.edu.cn/')
        db.session.add_all([self.admin, self.reader, self.school]); db.session.flush()
        self.dept = Department(school_id=self.school.id, name='教务处', list_url='https://example.edu.cn/list')
        db.session.add(self.dept)
        from backend.database.school_registry_models import SchoolRegistryEntry
        from backend.services.school_registry import registry_key
        db.session.add(SchoolRegistryEntry(registry_key=registry_key(self.school.name),
            canonical_name=self.school.name, root_url=self.school.url, school_id=self.school.id,
            aliases=['示例学校', '示例学校旧名'], origin='catalog'))
        db.session.commit()
        self.client = self.client_as(self.admin.id)
        self.headers = {'X-CSRF-Token': 'token'}

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def client_as(self, ident):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=ident, _csrf_token='token')
        return client

    def article(self, suffix='old', days=60, **values):
        stamp = datetime.utcnow() - timedelta(days=days)
        row = Announcement(school_id=self.school.id, department_id=self.dept.id, title='通知 ' + suffix,
            url='https://example.edu.cn/' + suffix, content_html='<p>已缓存正文</p>', content_text='已缓存正文',
            content_cached_at=stamp, content_accessed_at=stamp, content_bytes=100,
            created_at=stamp, published_at=stamp, summary='原有摘要')
        for key, value in values.items():
            setattr(row, key, value)
        db.session.add(row); db.session.commit()
        return row

    def upload(self, stream):
        return self.client.post('/api/storage/import', data={'file': (stream, 'backup.zip')}, headers=self.headers)

    def sample(self):
        return {'format': FORMAT, 'version': 1,
            'schools': [{'id': 99, 'name': '示例学校旧名', 'url': self.school.url}],
            'departments': [{'id': 88, 'school_id': 99, 'name': self.dept.name, 'list_url': self.dept.list_url}],
            'announcements': [{'id': 77, 'school_id': 99, 'department_id': 88, 'title': '只在备份中的历史通知',
                'url': 'https://example.edu.cn/history', 'created_at': '2020-01-02T00:00:00Z',
                'published_at': '2020-01-01T00:00:00Z', 'content_html': '<p>历史正文</p>', 'content_text': '历史正文'}],
            'announcement_sources': [], 'department_directory_entries': []}

    def test_administrator_and_csrf_required_for_every_operation(self):
        reader = self.client_as(self.reader.id)
        self.assertEqual(reader.get('/admin/storage').status_code, 403)
        self.assertEqual(reader.get('/api/storage').status_code, 403)
        for endpoint, method in [('policy', 'put'), ('cleanup', 'post'), ('export', 'post'), ('import', 'post')]:
            self.assertEqual(getattr(reader, method)('/api/storage/' + endpoint, headers=self.headers).status_code, 403)
            self.assertEqual(getattr(self.client, method)('/api/storage/' + endpoint).status_code, 403)
        self.assertEqual(self.client.get('/admin/storage').status_code, 200)

    def test_policy_validated_atomically_and_saving_does_not_clean(self):
        ann = self.article()
        initial = policy()
        values = {**initial, 'body_cache_days': 90, 'discovery_cache_days': 0, 'backup_keep_count': 3,
                  'scrape_log_retention_days': 180}
        self.assertEqual(self.client.put('/api/storage/policy', json=values, headers=self.headers).status_code, 200)
        for invalid in [None, [], {}, {**values, 'unexpected': 7}, {**values, 'body_cache_days': True},
                        {**values, 'body_cache_days': '30'}, {**values, 'body_cache_days': -1},
                        {**values, 'body_cache_mb': -1}, {**values, 'backup_keep_count': 1.5},
                        {**values, 'scrape_log_retention_days': 9007199254740992}]:
            self.assertEqual(self.client.put('/api/storage/policy', json=invalid, headers=self.headers).status_code, 400)
            self.assertEqual(policy(), values)
        db.session.refresh(ann)
        self.assertTrue(ann.content_html)
        with self.app.app_context():
            self.assertEqual(policy(), values)
        self.assertEqual(self.client.get('/api/admin/scrape-logs/retention').get_json()['days'], 180)

    def test_cleaning_honors_days_and_keeps_titles_saved_content_and_states(self):
        recent, old, saved = self.article('recent', days=20), self.article(), self.article('saved')
        state = UserAnnouncementState(user_id=self.reader.id, announcement_id=saved.id, starred=True)
        db.session.add(state); db.session.commit()
        save_policy({**policy(), 'body_cache_days': 90})
        self.assertEqual(prune_content()['evicted'], 0)
        save_policy({**policy(), 'body_cache_days': 30})
        self.assertEqual(prune_content()['evicted'], 1)
        db.session.refresh(old); db.session.refresh(recent); db.session.refresh(saved)
        self.assertFalse(old.content_html); self.assertTrue(recent.content_html); self.assertTrue(saved.content_html)
        self.assertEqual(old.summary, '原有摘要')
        self.assertEqual(Announcement.query.count(), 3)
        response = self.client.post('/api/storage/cleanup', json={'mode': 'all_cache'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['evicted'], 1)
        db.session.refresh(saved); self.assertTrue(saved.content_html)
        self.assertTrue(db.session.get(UserAnnouncementState, (self.reader.id, saved.id)).starred)

    def test_no_time_expiry_still_enforces_capacity_and_legacy_body_cleanup(self):
        ann = self.article(days=500)
        save_policy({**policy(), 'body_cache_days': 0})
        self.assertEqual(prune_content()['evicted'], 0)
        ann.content_bytes = 2 * 1048576
        db.session.commit()
        save_policy({'body_cache_mb': 1})
        self.assertEqual(prune_content()['evicted'], 1)
        legacy = self.article('legacy', content_bytes=0, content_cached_at=None, content_accessed_at=None)
        self.assertEqual(prune_content(all_cache=True)['evicted'], 1)
        db.session.refresh(legacy); self.assertFalse(legacy.content_html)

    def test_custom_and_unlimited_rules_reach_real_cleanup(self):
        ann = self.article(days=500, content_bytes=200 * 1048576)
        values = {**policy(), 'body_cache_mb': 0, 'body_cache_days': 5000,
                  'backup_keep_count': 200, 'scrape_log_retention_days': 12,
                  'discovery_cache_mb': 1024, 'fetch_cache_mb': 2048}
        self.assertEqual(save_policy(values), values)
        self.assertEqual(prune_content()['evicted'], 0)
        folder = self.root / 'backups'; folder.mkdir()
        for i in range(3): (folder / f'watcher-{i}.zip').write_bytes(b'fixture')
        save_policy({'backup_keep_count': 0})
        from backend.services.backups import rotate
        self.assertEqual(rotate(folder), 0)
        save_policy({'body_cache_days': 9007199254740991})
        self.assertEqual(prune_content()['evicted'], 0)
        save_policy({'body_cache_mb': 1})
        self.assertEqual(prune_content()['evicted'], 1)

    def test_row_cleanup_only_changes_its_category(self):
        ann = self.article()
        folder = self.root / 'backups'; folder.mkdir()
        (folder / 'watcher-fixture.zip').write_bytes(b'fixture')
        (folder / 'personal-document.zip').write_bytes(b'keep')
        result = self.client.post('/api/storage/cleanup', json={'mode':'backups'}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['backups_deleted'], 1)
        db.session.refresh(ann)
        self.assertTrue(ann.content_html)
        self.assertTrue((folder / 'personal-document.zip').exists())

    def test_external_cache_and_backup_usage_is_counted_once(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            (folder / 'discovery.sqlite3').write_bytes(b'cache')
            (folder / 'backups').mkdir()
            (folder / 'backups' / 'watcher-fixture.zip').write_bytes(b'backup')
            self.app.config.update(DISCOVERY_CACHE_PATH=str(folder / 'discovery.sqlite3'), BACKUP_DIR=str(folder / 'backups'))
            data = self.client.get('/api/storage').get_json()
            self.assertEqual(sum(f['bytes'] for f in data['files'] if f['name'].startswith('backups/')), 6)
            self.assertEqual(sum(f['bytes'] for f in data['files'] if f['name'].endswith('discovery.sqlite3')), 5)

    def test_cleanup_logs_rotation_and_busy_discovery(self):
        from filelock import FileLock
        self.article()
        stamp = datetime.utcnow() - timedelta(days=60)
        db.session.add_all([ScrapeLog(status='success', started_at=stamp), ScrapeLog(status='running', started_at=stamp)])
        db.session.commit()
        folder = self.root / 'backups'; folder.mkdir()
        for i in range(4): (folder / f'watcher-2026010{i}.zip').write_bytes(b'fixture')
        (folder / 'my-backup.zip').write_bytes(b'keep')
        save_policy({**policy(), 'backup_keep_count': 2})
        with FileLock(str(self.root / 'discovery.db') + '.worker.lock'):
            result = self.client.post('/api/storage/cleanup', json={'mode': 'expired'}, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertTrue(Announcement.query.first().content_html)
            self.assertEqual(ScrapeLog.query.count(), 2)
        result = self.client.post('/api/storage/cleanup', json={'mode': 'expired'}, headers=self.headers).get_json()
        self.assertEqual(result['backups_deleted'], 2)
        self.assertEqual(result['scrape_logs_deleted'], 1)
        self.assertEqual(ScrapeLog.query.first().status, 'running')
        self.assertTrue((folder / 'my-backup.zip').exists())

    def test_discovery_retention_keeps_pending_frontier_and_published_data(self):
        from backend.services.discovery_cache import DiscoveryCache
        cache = DiscoveryCache(self.root / 'discovery.db')
        key = cache.ensure_site('示例学校', self.school.url)
        stamp = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec='seconds')
        with cache.connect() as connection:
            connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)', (key, self.school.url, 'hash', b'body', stamp))
        save_policy({**policy(), 'discovery_cache_days': 30})
        self.assertEqual(cache.trim(), 0)
        save_policy({**policy(), 'discovery_cache_days': 0})
        self.assertEqual(cache.trim(), 0)
        save_policy({**policy(), 'discovery_cache_days': 7})
        self.assertEqual(cache.trim(), 1)
        with cache.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM pages WHERE state='pending'").fetchone()[0], 1)

    def test_export_and_reimport_retains_personal_state_and_omits_secrets(self):
        ann = self.article()
        db.session.add(UserAnnouncementState(user_id=self.reader.id, announcement_id=ann.id, starred=True))
        db.session.add(Subscription(user_id=self.reader.id, school_id=self.school.id))
        db.session.commit()
        response = self.client.post('/api/storage/export', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            raw = archive.read('data.json').decode()
            self.assertNotIn('secret-hash', raw); self.assertNotIn('users', raw)
        result = self.upload(io.BytesIO(response.data))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['duplicates'], 1)
        self.assertEqual(Announcement.query.count(), 1)
        self.assertEqual(Subscription.query.count(), 1)
        self.assertTrue(db.session.get(UserAnnouncementState, (self.reader.id, ann.id)).starred)

    def test_merge_preserves_new_current_and_old_only_notices_and_is_idempotent(self):
        current = self.article('current')
        existing = self.article('old', title='现有较新标题', content_text='现有较新正文')
        data = self.sample()
        duplicate = {**data['announcements'][0], 'id': existing.id, 'title': '旧备份标题',
                     'url': existing.url + '?utm_source=backup#section', 'content_text': '过时正文'}
        data['announcements'].append(duplicate)
        for added in (1, 0):
            response = self.upload(packed(data))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.get_json()['added'], added)
            self.assertEqual(Announcement.query.count(), 3)
        db.session.refresh(existing)
        self.assertEqual(existing.title, '现有较新标题'); self.assertEqual(existing.content_text, '现有较新正文')
        self.assertIsNotNone(db.session.get(Announcement, current.id))
        historical = Announcement.query.filter_by(url='https://example.edu.cn/history').one()
        self.assertEqual(historical.published_at.year, 2020)
        self.assertEqual(School.query.count(), 1); self.assertEqual(Department.query.count(), 1)
        self.assertEqual(self.school.name, '示例学校')

    def test_empty_body_recovered_html_sanitized_and_url_less_records_deduplicated(self):
        ann = self.article('history', content_html='', content_text='', content_bytes=0, content_cached_at=None)
        data = self.sample()
        data['announcements'][0]['content_html'] = '<p onclick="bad()">历史正文</p><script>bad()</script>'
        result = self.upload(packed(data)).get_json()
        self.assertEqual(result['added'], 0); self.assertEqual(result['bodies_restored'], 1)
        db.session.refresh(ann)
        self.assertNotIn('<script', ann.content_html); self.assertNotIn('onclick', ann.content_html)
        data['announcements'][0]['url'] = ''
        self.assertEqual(self.upload(packed(data)).get_json()['added'], 1)
        prune_content(all_cache=True)
        self.assertEqual(self.upload(packed(data)).get_json()['added'], 0)

    def test_source_memberships_and_directory_ids_are_remapped(self):
        data = self.sample()
        data['departments'].append({'id': 89, 'school_id': 99, 'name': '专业目录', 'list_url': 'https://example.edu.cn/majors'})
        data['department_directory_entries'] = [{'parent_id': 88, 'department_id': 89, 'position': 3}]
        data['announcement_sources'] = [{'announcement_id': 77, 'department_id': 89,
            'list_url': 'https://example.edu.cn/majors', 'article_url': 'https://example.edu.cn/history',
            'first_seen_at': '2020-01-01', 'last_seen_at': '2020-01-03'}]
        for _ in range(2):
            self.assertEqual(self.upload(packed(data)).status_code, 200)
        self.assertEqual(AnnouncementSource.query.count(), 2)
        directory = DepartmentDirectoryEntry.query.one()
        self.assertEqual(directory.parent_id, self.dept.id)
        self.assertEqual(db.session.get(Department, directory.department_id).name, '专业目录')

    def test_undated_url_less_history_deduplicates_without_collapsing_different_bodies(self):
        data = self.sample()
        row = data['announcements'][0]
        row.update(url='', published_at=None, created_at=None, content_text='通知正文' * 200)
        data['announcements'].append({**row, 'id': 78, 'content_html': '<p>另一份附件通知</p>'})
        for added in (2, 0):
            response = self.upload(packed(data))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['added'], added)
            self.assertEqual(Announcement.query.count(), 2)
        prune_content(all_cache=True)
        with export_data() as exported:
            self.assertEqual(self.upload(io.BytesIO(exported.read())).get_json()['added'], 0)
        self.assertEqual(self.upload(packed(data)).get_json()['added'], 0)

    def test_historical_runtime_zip_is_importable_without_replacing_accounts(self):
        from backend.services.backups import create_backup
        ann = self.article()
        backup = create_backup()
        ann.title = '更新后的标题'; db.session.commit()
        with (self.root / 'backups' / backup['backup']).open('rb') as stream:
            response = self.upload(io.BytesIO(stream.read()))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.get_json()['duplicates'], 1)
        self.assertEqual(User.query.count(), 2)
        db.session.refresh(ann); self.assertEqual(ann.title, '更新后的标题')

    def test_invalid_backup_or_references_leave_database_unchanged(self):
        data = self.sample()
        invalid = []
        for table, field, value in [('announcements', 'department_id', 1000), ('announcements', 'title', None),
                                  ('announcements', 'published_at', 'not-date'), ('schools', 'url', 'javascript:bad()'),
                                  ('announcements', 'id', True)]:
            bad = deepcopy(data); bad[table][0][field] = value; invalid.append(bad)
        bad = deepcopy(data); bad['announcements'].append(deepcopy(bad['announcements'][0])); invalid.append(bad)
        for bad in invalid:
            response = self.upload(packed(bad))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(Announcement.query.count(), 0)
        self.assertEqual(self.upload(io.BytesIO(b'not a zip')).status_code, 400)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape.db', 'no')
        archive.seek(0)
        self.assertEqual(self.upload(archive).status_code, 400)
        with patch('backend.services.data_transfer.MAX_EXPANDED_BYTES', 10):
            self.assertEqual(self.upload(packed(data)).status_code, 400)
        with patch('backend.services.data_transfer.MAX_UPLOAD_BYTES', 100):
            self.assertEqual(self.upload(packed(data)).status_code, 413)

    def test_write_failure_rolls_back_all_imported_rows(self):
        data = self.sample(); data['schools'][0]['url'] = 'https://new-school.edu.cn/'
        validated = read_backup(packed(data))
        with patch('backend.services.data_transfer._merge_source', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError): merge_data(validated)
        self.assertEqual(Announcement.query.count(), 0)
        self.assertEqual(School.query.count(), 1); self.assertEqual(Department.query.count(), 1)

    def test_concurrent_imports_do_not_duplicate_notices(self):
        raw = packed(self.sample()).getvalue()
        db.session.remove()
        def import_one():
            with self.app.app_context():
                return merge_data(read_backup(io.BytesIO(raw)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: import_one(), range(2)))
        self.assertEqual(sum(r['added'] for r in results), 1)
        self.assertEqual(Announcement.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
