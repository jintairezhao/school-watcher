"""Published structure remains usable and recoverable after history rotation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

import test_storage_management as fixtures
from backend.database.db import db
from backend.database.models import CatalogPublication
from backend.services.backups import create_backup
from backend.services.runtime_catalog import RuntimeCatalog, prune_catalog_generations
from backend.services.source_inventory import Inventory


class CatalogRetentionBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StorageManagementTests()
        self.fixture.setUp()
        self.root, self.app = self.fixture.root, self.fixture.app
        inventory = Inventory(self.root / 'isolated-investigation.db')
        self.key = inventory.ensure_site('Example', 'https://example.edu.cn/')
        inventory.finish(self.key, 'https://example.edu.cn/', state='fetched',
                         title='Verified official structure', html='<p>Official page evidence</p>')
        self.catalog = RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'])
        self.catalog.publish(inventory, self.key)
        publication = db.session.get(CatalogPublication, self.key)
        self.active = publication.path
        self.active_bytes = (self.root / self.active).read_bytes()
        self.legacy = []
        for offset in range(5):
            old = self.root / 'catalog-generations' / (self.key + '-' + uuid4().hex + '.sqlite3')
            shutil.copyfile(self.root / self.active, old)
            stamp = time.time() - 3600 - offset
            os.utime(old, (stamp, stamp))
            self.legacy.append(old)

    def tearDown(self):
        self.fixture.tearDown()

    def assert_published_and_backup(self, result):
        self.assertEqual((self.root / self.active).read_bytes(), self.active_bytes)
        self.assertEqual(self.catalog.get_page(self.key, 'https://example.edu.cn/')['title'],
                         'Verified official structure')
        archive = Path(self.app.config['BACKUP_DIR']) / result['backup']
        with zipfile.ZipFile(archive) as backup:
            manifest = json.loads(backup.read('manifest.json'))
            self.assertIn(self.active, manifest)
            self.assertEqual([name for name in manifest if name.startswith('catalog-generations/')], [self.active])
            restored = self.root / 'restored-active.sqlite3'
            restored.write_bytes(backup.read(self.active))
        with closing(sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True)) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT count(*) FROM catalog_pages').fetchone()[0], 1)
            with closing(sqlite3.connect((self.root / self.active).as_uri() + '?mode=ro', uri=True)) as source:
                for table in ('catalog_sites', 'catalog_pages'):
                    self.assertEqual(connection.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall(),
                                     source.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall())

    def test_rotation_keeps_current_readable_and_complete_backup_contains_current_only(self):
        preview = prune_catalog_generations(dry_run=True, offline_legacy=True)
        self.assertEqual(preview['files'], 3)
        self.assertEqual(sum(path.exists() for path in self.legacy), 5)
        removed = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(removed['files'], 3)
        self.assertEqual(sum(path.exists() for path in self.legacy), 2)
        self.assert_published_and_backup(create_backup())

    def test_retained_pdf_evidence_survives_checked_backup_and_restore(self):
        from backend.scraper.acquisition.attachments import pdf_observation
        from backend.services.backups import restore_backup,verify_evidence_file
        self.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH']=str(self.root/'pdf-evidence')
        payload=b'%PDF-1.4\ncontrolled retained evidence fixture\n%%EOF\n'
        observation=pdf_observation(payload,'https://example.edu.cn/0610.pdf',{},retain=True)
        result=create_backup()
        archive=Path(self.app.config['BACKUP_DIR'])/result['backup']
        name='source-governance-evidence/'+observation['sha256']+'.pdf'
        with zipfile.ZipFile(archive) as backup:
            self.assertEqual(backup.read(name),payload)
        from backend.services.data_transfer import read_backup
        with archive.open('rb') as stream:
            read_backup(stream)
        destination=self.root/'restored-pdf'
        restore_backup(archive,destination)
        restored=destination/name
        self.assertEqual(restored.read_bytes(),payload)
        verify_evidence_file(restored)
        restored.write_bytes(payload+b'changed')
        with self.assertRaises(ValueError):verify_evidence_file(restored)

    def test_backup_holds_publication_lock_until_all_current_files_are_saved(self):
        from backend.database.provider_backup import sqlite_snapshot
        entered, release = threading.Event(), threading.Event()
        def held_snapshot(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Backup test was not released')
            return sqlite_snapshot(*args, **kwargs)
        def backup_in_thread():
            with self.app.app_context():
                return create_backup()
        with patch('backend.database.provider_backup.sqlite_snapshot', side_effect=held_snapshot):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(backup_in_thread)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
                    self.assertTrue(all(path.exists() for path in self.legacy))
                finally:
                    release.set()
                result = future.result(timeout=10)
        self.assert_published_and_backup(result)
