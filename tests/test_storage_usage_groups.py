"""Coarse usage is exact; cache-only cleanup preserves durable and personal data."""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from filelock import FileLock

from backend.database.db import db
from backend.database.models import Announcement, AnnouncementResource, UserAnnouncementState
from backend.database.source_governance_models import SchoolOnboarding
from backend.routes.operations import _storage_usage_groups
from backend.services.discovery_cache import DiscoveryCache
from backend.services import tasks


class StorageUsageGroupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_storage_management import StorageManagementTests
        self.fixture = StorageManagementTests()
        self.fixture.setUp()
        self.root, self.app = self.fixture.root, self.fixture.app
        self.client, self.headers = self.fixture.client, self.fixture.headers
        self.app.config.update(FETCH_EVIDENCE_DIR=str(self.root / 'fetch-evidence'),
            SOURCE_GOVERNANCE_EVIDENCE_PATH=str(self.root / 'source-governance-evidence'))

    def tearDown(self):
        self.fixture.tearDown()

    def write(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        return path

    def test_groups_classify_once_and_keep_installer_webview_and_body_bytes_distinct(self):
        files = [
            {'name': 'custom.db', 'kind': 'main_database', 'bytes': 100},
            {'name': 'catalog-generations/current.sqlite3', 'kind': 'source_catalog', 'bytes': 50},
            {'name': 'discovery_cache.sqlite3', 'kind': 'discovery_progress', 'bytes': 30},
            {'name': 'discovery_cache.sqlite3.snapshots.sqlite3', 'bytes': 11},
            {'name': 'fetch-evidence/captured.json', 'bytes': 12},
            {'name': 'proof.html.gz', 'kind': 'governance_evidence', 'bytes': 13},
            {'name': 'desktop.log', 'bytes': 4},
            {'name': 'desktop-web.log.1', 'bytes': 5},
            {'name': 'logs/2026-10/2026-10-20-worker-123.1.log', 'bytes': 6},
            {'name': 'school_watcher.db.backup-previous', 'bytes': 21},
            {'name': 'backups/watcher-test.zip', 'bytes': 22},
            {'name': 'updates/2.27/backup-setup.exe', 'bytes': 31},
            {'name': 'webview/EBWebView/Default/LOG', 'bytes': 32},
            {'name': 'webview/EBWebView/backup.bak', 'bytes': 33},
            {'name': 'desktop-instance.json', 'bytes': 2},
        ]
        result = _storage_usage_groups(files, database_bytes=300)
        self.assertEqual(result, {'data': 480, 'cache': 36, 'logs': 15,
            'backups': 43, 'application': 98})
        self.assertEqual(sum(result.values()), 300 + sum(row['bytes'] for row in files))

    def test_api_and_template_share_exact_totals_without_adding_database_body_payload_twice(self):
        ann = self.fixture.article('large-logical-body', content_bytes=5_000_000)
        db.session.add(UserAnnouncementState(user_id=self.fixture.reader.id, announcement_id=ann.id, starred=True))
        db.session.commit()
        for name, size in [('catalog.db', 101), ('discovery.db', 102),
                ('discovery.db.snapshots.sqlite3', 103), ('fetch-evidence/page.json', 104),
                ('source-governance-evidence/' + 'a' * 64 + '.html.gz', 105),
                ('desktop.log', 106), ('logs/2026-10/2026-10-20-web-1.log', 107),
                ('backups/watcher-old.zip', 108), ('updates/2.27/installer.exe', 109),
                ('webview/EBWebView/Default/Cache/data', 110), ('desktop-settings.json', 111)]:
            self.write(name, size)
        response = self.client.get('/api/storage')
        self.assertEqual(response.status_code, 200, response.json)
        data = response.json
        self.assertEqual(set(data['usage_groups']), {'data', 'cache', 'logs', 'backups', 'application'})
        self.assertEqual(sum(data['usage_groups'].values()), data['total_bytes'])
        self.assertEqual(sum(row['bytes'] for row in data['files']), data['total_bytes'])
        self.assertEqual(data['usage_groups']['cache'], 103 + 104 + 105)
        self.assertEqual(data['usage_groups']['logs'], 106 + 107)
        self.assertEqual(data['usage_groups']['backups'], 108)
        self.assertEqual(data['usage_groups']['application'], 109 + 110 + 111)
        self.assertEqual(data['saved_body_bytes'], 5_000_000)
        self.assertLess(data['total_bytes'], data['saved_body_bytes'])
        with patch('backend.routes.operations.render_template', return_value='coarse groups') as render:
            page = self.client.get('/admin/storage')
        self.assertEqual(page.status_code, 200)
        context = render.call_args.kwargs
        self.assertEqual(context['groups'], {name: context['storage']['usage_groups'][key] for name, key in (
            ('通知与目录', 'data'), ('缓存', 'cache'), ('日志', 'logs'), ('备份', 'backups'), ('应用文件', 'application'))})

    def test_relocated_governance_overlapping_fetch_inventory_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as external:
            fetch = Path(external)
            governance = fetch / 'proofs'
            governance.mkdir()
            proof = governance / ('a' * 64 + '.html.gz')
            proof.write_bytes(b'official proof')
            self.app.config.update(FETCH_EVIDENCE_DIR=str(fetch),
                SOURCE_GOVERNANCE_EVIDENCE_PATH=str(governance))
            data = self.client.get('/api/storage').json
            proofs = [row for row in data['files'] if row['kind'] == 'governance_evidence']
            self.assertEqual(len(proofs), 1)
            self.assertEqual(data['usage_groups']['cache'], len(b'official proof'))
            self.assertEqual(data['total_bytes'], sum(row['bytes'] for row in data['files']))
            self.assertEqual(data['source_governance_evidence_bytes'], len(b'official proof'))

    def test_cache_only_preserves_favorites_recent_trash_progress_active_fetch_and_bound_proof(self):
        ordinary, favorite, trash = [self.fixture.article(name) for name in ('ordinary', 'favorite', 'trash')]
        db.session.add_all([
            UserAnnouncementState(user_id=self.fixture.reader.id, announcement_id=favorite.id, starred=True),
            UserAnnouncementState(user_id=self.fixture.reader.id, announcement_id=trash.id, starred=False,
                trashed_at=datetime.utcnow() - timedelta(days=2)),
        ])
        payload = b'protected saved attachment'
        for ann in (favorite, trash):
            db.session.add(AnnouncementResource(announcement_id=ann.id, url_key=hashlib.sha256(str(ann.id).encode()).hexdigest(),
                url='https://example.edu.cn/proof.pdf', kind='attachment', filename='proof.pdf',
                state='ready', payload=payload, byte_size=len(payload)))
        proof_hash, orphan_hash, recent_hash = 'a' * 64, 'b' * 64, 'c' * 64
        db.session.add(SchoolOnboarding(school_id=self.fixture.school.id,
            checkpoint_json=json.dumps({'reference_hash': proof_hash})))
        db.session.commit()
        cache = DiscoveryCache(self.app.config['DISCOVERY_CACHE_PATH'])
        key = cache.ensure_site('Example', 'https://example.edu.cn/')
        cache.finish(key, 'https://example.edu.cn/', state='fetched', title='Official home', html='<p>Disposable HTML</p>')
        cache.enqueue(key, 'https://example.edu.cn/pending', 'Pending', 'navigation', 1, [], 1)
        with cache.connect() as connection:
            progress_before = [tuple(row) for row in connection.execute('SELECT * FROM pages ORDER BY url')]
        active = tasks.enqueue('onboard', 'cache-only-active', {'school_id': self.fixture.school.id})
        active_fetch = self.write(f'fetch-evidence/{active.id}-{active.generation}-active.json', 10)
        expired_fetch = self.write('fetch-evidence/999999-1-finished.json', 11)
        protected = self.write(f'source-governance-evidence/{proof_hash}.html.gz', 12)
        orphan = self.write(f'source-governance-evidence/{orphan_hash}.html.gz', 13)
        recent = self.write(f'source-governance-evidence/{recent_hash}.html.gz', 14)
        old = time.time() - 3 * 86400
        for path in (protected, orphan):
            os.utime(path, (old, old))
        log = self.write('desktop.log', 20)
        backup = self.write('backups/watcher-preserved.zip', 21)
        generation = self.write('catalog-generations/previous.sqlite3', 22)
        with patch('backend.services.scrape_logs.prune_scrape_logs', side_effect=AssertionError('Cache must not clean logs')), \
             patch('backend.services.backups.rotate', side_effect=AssertionError('Cache must not rotate backups')), \
             patch('backend.services.runtime_catalog.prune_catalog_generations', side_effect=AssertionError('Cache must not prune catalogs')), \
             patch('filelock.FileLock', wraps=FileLock) as locks:
            response = self.client.post('/api/storage/cleanup', json={'mode': 'cache'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(sum(str(call.args[0]) == self.app.config['DISCOVERY_CACHE_PATH'] + '.worker.lock'
            for call in locks.call_args_list), 1)
        self.assertEqual(response.json['evicted'], 1)
        self.assertGreaterEqual(response.json['discovery_snapshots_deleted'], 1)
        self.assertEqual(response.json['fetch_evidence']['removed'], 1)
        self.assertEqual(response.json['governance_evidence']['removed'], 1)
        self.assertEqual((response.json['scrape_logs_deleted'], response.json['backups_deleted']), (0, 0))
        db.session.expire_all()
        self.assertFalse(db.session.get(Announcement, ordinary.id).content_html)
        self.assertTrue(db.session.get(Announcement, favorite.id).content_html)
        self.assertTrue(db.session.get(Announcement, trash.id).content_html)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual([row.payload for row in AnnouncementResource.query.all()], [payload, payload])
        self.assertTrue(all(path.exists() for path in (active_fetch, protected, recent, log, backup, generation)))
        self.assertFalse(expired_fetch.exists())
        self.assertFalse(orphan.exists())
        with cache.connect() as connection:
            self.assertEqual(progress_before, [tuple(row) for row in connection.execute('SELECT * FROM pages ORDER BY url')])
        self.assertEqual(cache.snapshots.get(key, 'https://example.edu.cn/', cache.get_page(key, 'https://example.edu.cn/')['content_hash']), None)

    def test_busy_worker_and_missing_authorization_prevent_cache_changes(self):
        ann = self.fixture.article('busy')
        scratch = Path(self.app.config['DISCOVERY_CACHE_PATH'])
        with FileLock(str(scratch) + '.worker.lock', timeout=0):
            response = self.client.post('/api/storage/cleanup', json={'mode': 'cache'}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(db.session.get(Announcement, ann.id).content_html)
        reader = self.fixture.client_as(self.fixture.reader.id)
        self.assertEqual(reader.post('/api/storage/cleanup', json={'mode': 'cache'}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post('/api/storage/cleanup', json={'mode': 'cache'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
