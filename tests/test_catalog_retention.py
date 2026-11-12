"""Bounded catalogue history never removes a current, reader, writer or backup."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import update

from backend.database.db import db
from backend.database.models import BackgroundTask, CatalogPublication, School
from backend.services import runtime_catalog as module, tasks
from backend.services.runtime_catalog import RuntimeCatalog, prune_catalog_generations, catalog_publication_lock
from backend.services.source_inventory import Inventory


class CatalogRetentionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()
        self.app = self.fixture.app
        # Windows hosted runners can expose the same temporary folder through
        # an 8.3 alias. Match the canonical paths used by the deletion guards,
        # so the injected permission/link failures actually intercept them.
        self.root = Path(self.fixture.temp.name).resolve()
        self.catalog = RuntimeCatalog(Path(self.app.config['SOURCE_CATALOG_PATH']).resolve())
        self.inv = Inventory(self.root / 'investigation.db')
        self.key = self.inv.ensure_site('Example', 'https://example.edu.cn/')
        self.url = 'https://example.edu.cn/'
        self.inv.finish(self.key, self.url, state='fetched', title='Original', html='<p>Evidence one</p>')
        page = self.inv.get_page(self.key, self.url)
        check = {'id': 'proof', 'reference_url': self.url, 'reference_hash': page['content_hash'],
            'scope_passed': True, 'reference_current': True, 'category': 'academic'}
        with self.inv.connect() as connection:
            connection.execute('INSERT INTO reference_checks VALUES(?,?,?,?,?)',
                (self.key, 'proof', '{}', json.dumps(check), '2026-10-20'))
        self.first = self.catalog.publish(self.inv, self.key)
        self.directory = self.catalog.path.parent / 'catalog-generations'

    def tearDown(self):
        self.fixture.tearDown()

    def active(self, key=None):
        return next(row for row in module._active_publications(self.catalog.path.resolve())
            if row['site_key'] == (key or self.key))

    def active_path(self):
        return self.catalog.path.parent / self.active()['path']

    def histories(self, count=5, key=None):
        source = self.active_path()
        paths = []
        for offset in range(count):
            path = self.directory / f'{key or self.key}-{uuid4().hex}.sqlite3'
            shutil.copyfile(source, path)
            stamp = time.time() - 3600 - offset
            os.utime(path, (stamp, stamp))
            paths.append(path)
        return paths

    def publish_title(self, title):
        self.inv.finish(self.key, self.url, state='fetched', title=title, html=f'<p>{title}</p>')
        return self.catalog.publish(self.inv, self.key, merge=True)

    def test_offline_rotation_preserves_active_bytes_and_two_previous_versions(self):
        active = self.active_path(); expected = active.read_bytes()
        old = time.time() - 30 * 86400
        os.utime(active, (old, old))
        histories = self.histories(7)
        removed = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(removed['files'], 5)
        self.assertEqual(sum(path.exists() for path in histories), 2)
        self.assertEqual(active.read_bytes(), expected)
        self.assertEqual(self.catalog.get_page(self.key, self.url)['title'], 'Original')

    def test_unknown_files_get_observation_grace_and_dry_run_never_writes_ledger(self):
        histories = self.histories(5)
        ledger = self.catalog.path.with_name(self.catalog.path.name + '.retention.json')
        self.assertFalse(ledger.exists())
        self.assertEqual(prune_catalog_generations(dry_run=True, offline_legacy=True)['files'], 3)
        self.assertFalse(ledger.exists())
        self.assertTrue(all(path.exists() for path in histories))
        start = time.time()
        with patch('backend.services.runtime_catalog.time.time', return_value=start):
            result = prune_catalog_generations()
        self.assertEqual(result['files'], 0)
        before = ledger.read_bytes()
        self.assertEqual(prune_catalog_generations(dry_run=True)['files'], 0)
        self.assertEqual(ledger.read_bytes(), before)
        with patch('backend.services.runtime_catalog.time.time', return_value=start + 601):
            self.assertEqual(prune_catalog_generations()['files'], 3)

    def test_history_byte_budget_excludes_active_and_can_reduce_two_version_limit(self):
        histories = self.histories(5)
        size = self.active_path().stat().st_size
        with patch.object(module, 'HISTORY_BYTES', size):
            result = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(result['history_files'], 1)
        self.assertEqual(result['history_bytes'], size)
        self.assertEqual(result['files'], 4)
        self.assertTrue(self.active_path().exists())
        self.assertEqual(sum(path.exists() for path in histories), 1)

    def test_real_retirement_grace_uses_commit_time_not_old_generation_mtime(self):
        first = self.active_path()
        os.utime(first, (1, 1))
        start = time.time()
        with patch('backend.services.runtime_catalog.time.time', return_value=start):
            self.publish_title('Second')
            self.publish_title('Third')
            self.publish_title('Fourth')
        with patch('backend.services.runtime_catalog.time.time', return_value=start + 1), patch.object(module, 'HISTORY_BYTES', 0):
            result = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(result['files'], 0)
        self.assertTrue(first.exists())
        self.assertGreater(result['over_budget_bytes'], 0)
        with patch('backend.services.runtime_catalog.time.time', return_value=start + 601):
            self.assertEqual(prune_catalog_generations()['files'], 1)
        self.assertFalse(first.exists())

    def test_unchanged_publish_reuses_generation_and_keeps_caller_commit(self):
        original = self.active_path().read_bytes()
        published_at = self.catalog.report(self.key)['catalogue_published_at']
        with self.inv.connect() as connection:
            connection.execute('UPDATE sites SET updated_at=? WHERE site_key=?', ('2099-01-01', self.key))
        school = db.session.get(School, self.fixture.school_id)
        school.name = 'Caller business update'
        self.assertEqual(self.catalog.publish(self.inv, self.key), self.first)
        db.session.remove()
        self.assertEqual(db.session.get(School, self.fixture.school_id).name, 'Caller business update')
        self.assertEqual(self.active_path().read_bytes(), original)
        self.assertEqual(self.catalog.report(self.key)['catalogue_published_at'], published_at)
        self.assertEqual(len(list(self.directory.glob('*.sqlite3'))), 1)

    def test_repaired_evidence_is_not_lost_even_when_metadata_is_identical(self):
        # A damaged historical body must not make metadata-only comparison
        # discard the freshly verified original document.
        with self.catalog.pin(self.key).connect(write=True) as connection:
            connection.execute('UPDATE catalog_pages SET evidence_gzip=? WHERE url=?',
                (gzip.compress(b'<p>Damaged preserved evidence</p>'),self.url))
        second = self.catalog.publish(self.inv, self.key)
        self.assertNotEqual(second, self.first)
        self.assertEqual(self.catalog.snapshot(self.key, self.url), '<p>Evidence one</p>')

    def test_actual_verification_timestamp_change_is_published(self):
        report = self.inv.report(self.key)
        report['reference_checks'][0]['updated_at'] = '2026-10-20T10:00:00Z'
        with patch.object(self.inv, 'report', return_value=report):
            second = self.catalog.publish(self.inv, self.key)
        self.assertNotEqual(second, self.first)
        self.assertEqual(self.catalog.report(self.key)['reference_checks'][0]['updated_at'],
            '2026-10-20T10:00:00Z')

    def test_failed_unlink_reports_actual_residual_history(self):
        histories = self.histories(3)
        blocked = histories[-1]
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('A legacy reader still owns this file')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            result = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(result['files'], 0)
        self.assertEqual(result['skipped_files'], 1)
        self.assertEqual(result['history_files'], 3)
        self.assertEqual(result['history_bytes'], sum(path.stat().st_size for path in histories))
        self.assertTrue(all(path.exists() for path in histories))

    def test_stale_task_cannot_activate_changed_or_unchanged_publication(self):
        active = self.active_path(); expected = active.read_bytes()
        job = tasks.enqueue('discover', 'retention-owner', {'school_id': self.fixture.school_id})
        handle = tasks.claim(capabilities=['directory'])
        db.session.execute(update(BackgroundTask).where(BackgroundTask.id == job.id).values(token='replacement'))
        db.session.commit()
        for changed in (False, True):
            with self.subTest(changed=changed):
                if changed:
                    self.inv.finish(self.key, self.url, state='fetched', title='Rejected', html='<p>Rejected</p>')
                with tasks.execution_scope(handle), self.assertRaises(tasks.LeaseLost):
                    self.catalog.publish(self.inv, self.key)
        self.assertEqual(self.active_path(), active)
        self.assertEqual(active.read_bytes(), expected)
        self.assertEqual(len(list(self.directory.glob('*.sqlite3'))), 1)

    def test_maintenance_failure_does_not_reverse_successful_publication(self):
        with patch('backend.services.runtime_catalog._prune_generations', side_effect=OSError('busy')):
            generation = self.publish_title('Committed despite maintenance')
        self.assertEqual(self.active()['generation'], generation)
        self.assertEqual(self.catalog.get_page(self.key, self.url)['title'], 'Committed despite maintenance')

    def test_fail_closed_for_missing_current_bad_pointer_or_unreadable_main_database(self):
        histories = self.histories(5)
        active = self.active_path()
        missing = active.with_name(active.name + '.temporarily-missing')
        active.rename(missing)
        try:
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
            self.assertTrue(all(path.exists() for path in histories))
        finally:
            missing.rename(active)
        original = self.active()['path']
        db.session.execute(update(CatalogPublication).where(CatalogPublication.site_key == self.key).values(path='../elsewhere.sqlite3'))
        db.session.commit()
        self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        db.session.execute(update(CatalogPublication).where(CatalogPublication.site_key == self.key).values(path=original))
        db.session.commit()
        with patch.object(db.engine, 'connect', side_effect=RuntimeError('Database unavailable')):
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        self.assertTrue(all(path.exists() for path in histories))

    def test_ledger_failures_preserve_every_snapshot(self):
        histories = self.histories(5)
        with patch('backend.services.runtime_catalog._write_retirements', side_effect=OSError('Cannot persist metadata')):
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        self.assertTrue(all(path.exists() for path in histories))
        ledger = self.catalog.path.with_name(self.catalog.path.name + '.retention.json')
        ledger.write_text('not valid JSON', encoding='utf8')
        self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        self.assertEqual(ledger.read_text('utf8'), 'not valid JSON')
        self.assertTrue(all(path.exists() for path in histories))

    def test_unmanaged_names_sidecars_and_links_are_never_removed(self):
        self.histories(4)
        protected = [self.directory / name for name in ('backup.sqlite3', 'site-not-a-uuid.sqlite3',
            'unmanaged!-'+uuid4().hex+'.sqlite3')]
        # Exercise the overlong identifier guard without exceeding Windows MAX_PATH.
        from backend.services.runtime_catalog import _GENERATION_NAME
        self.assertIsNone(_GENERATION_NAME.fullmatch(('x'*129)+'-'+uuid4().hex+'.sqlite3'))
        for path in protected:
            path.write_bytes(b'unmanaged')
        partial = self.histories(1)[0]
        wal = partial.with_name(partial.name + '-wal'); wal.write_bytes(b'unknown writer')
        outside = self.root / 'outside.sqlite3'; outside.write_bytes(b'never touch')
        link = self.directory / f'{self.key}-{uuid4().hex}.sqlite3'
        try:
            link.symlink_to(outside)
        except OSError:
            link = None
        result = prune_catalog_generations(offline_legacy=True)
        self.assertGreater(result['files'], 0)
        self.assertTrue(all(path.exists() for path in protected))
        self.assertTrue(partial.exists() and wal.exists())
        self.assertEqual(outside.read_bytes(), b'never touch')
        if link is not None:
            self.assertTrue(link.is_symlink())

    def test_nested_merge_reads_and_manual_outer_lock_are_reentrant_without_lock_debris(self):
        self.histories(5)
        with catalog_publication_lock():
            self.assertEqual(prune_catalog_generations(dry_run=True, offline_legacy=True)['files'], 3)
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 3)
            self.publish_title('Nested merge')
            self.assertEqual(self.catalog.report(self.key)['pages'][0]['title'], 'Nested merge')
            self.assertTrue(self.catalog.all_sites())
        self.assertEqual(list(self.directory.glob('*.lock')), [])

    def test_long_reader_blocks_publish_and_prune_until_its_actual_read_finishes(self):
        histories = self.histories(5)
        entered, release, publishing = threading.Event(), threading.Event(), threading.Event()
        original_unpack = module.unpack
        def held_unpack(value):
            if threading.current_thread().name.startswith('catalog-reader') and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Reader was not released')
            return original_unpack(value)
        def reader():
            threading.current_thread().name = 'catalog-reader'
            with self.app.app_context():
                return self.catalog.report(self.key)
        def publisher():
            with self.app.app_context():
                publishing.set()
                return self.publish_title('After held reader')
        with patch('backend.services.runtime_catalog.unpack', side_effect=held_unpack), ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(reader)
            self.assertTrue(entered.wait(5))
            publish = pool.submit(publisher)
            self.assertTrue(publishing.wait(5))
            try:
                self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
                self.assertTrue(all(path.exists() for path in histories))
                self.assertFalse(publish.done())
            finally:
                release.set()
            self.assertEqual(read.result(timeout=10)['pages'][0]['title'], 'Original')
            publish.result(timeout=10)
        self.assertEqual(self.catalog.get_page(self.key, self.url)['title'], 'After held reader')

    def test_fresh_pointer_bypasses_stale_orm_identity(self):
        session = db.session(); session.expire_on_commit = False
        stale = db.session.get(CatalogPublication, self.key)
        old_path = stale.path
        def publisher():
            with self.app.app_context():
                return self.publish_title('Fresh current')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(publisher).result(timeout=10)
        self.assertEqual(stale.path, old_path)
        with patch.object(module, 'HISTORY_BYTES', 0), patch('backend.services.runtime_catalog.time.time', return_value=time.time() + 601):
            self.assertEqual(prune_catalog_generations()['files'], 1)
        self.assertFalse((self.catalog.path.parent / old_path).exists())
        self.assertEqual(self.catalog.get_page(self.key, self.url)['title'], 'Fresh current')

    def test_post_commit_pruning_bounds_history_between_housekeeping_runs(self):
        start = time.time()
        for number in range(6):
            with patch('backend.services.runtime_catalog.time.time', return_value=start + number * 601):
                self.publish_title(f'Publication {number}')
        self.assertLessEqual(len(list(self.directory.glob('*.sqlite3'))), 3)

    def test_recent_real_retirements_rank_ahead_of_newly_observed_ancient_orphans(self):
        histories = self.histories(5)
        first = self.active_path()
        os.utime(first, (1, 1))
        start = time.time()
        with patch('backend.services.runtime_catalog.time.time', return_value=start):
            self.publish_title('Second real publication')
        second = self.active_path()
        with patch('backend.services.runtime_catalog.time.time', return_value=start + 1):
            self.publish_title('Third real publication')
        with patch('backend.services.runtime_catalog.time.time', return_value=start + 602):
            prune_catalog_generations()
        self.assertTrue(first.exists() and second.exists())
        self.assertFalse(any(path.exists() for path in histories))

    def test_all_sites_current_files_are_hard_protected_under_zero_history_budget(self):
        another = Inventory(self.root / 'second-investigation.db')
        key = another.ensure_site('Second', 'https://second.example.edu.cn/')
        another.finish(key, 'https://second.example.edu.cn/', state='fetched', title='Second current', html='<p>Second</p>')
        self.catalog.publish(another, key)
        active = {self.catalog.path.parent / row['path'] for row in module._active_publications(self.catalog.path.resolve())}
        expected = {path: path.read_bytes() for path in active}
        self.histories(4); self.histories(4, key=key)
        with patch.object(module, 'HISTORY_BYTES', 0):
            result = prune_catalog_generations(offline_legacy=True)
        self.assertEqual(result['active_files'], 2)
        self.assertEqual(result['files'], 8)
        self.assertEqual({path: path.read_bytes() for path in active}, expected)

    def test_linked_directory_or_ledger_is_rejected_before_cleanup(self):
        histories = self.histories(5)
        actual = Path.is_junction
        def directory_junction(path):
            return path == self.directory or actual(path)
        with patch.object(Path, 'is_junction', directory_junction):
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        ledger = self.catalog.path.with_name(self.catalog.path.name + '.retention.json')
        actual_link = Path.is_symlink
        def ledger_link(path):
            return path == ledger or actual_link(path)
        with patch.object(Path, 'is_symlink', ledger_link):
            self.assertEqual(prune_catalog_generations(offline_legacy=True)['files'], 0)
        self.assertTrue(all(path.exists() for path in histories))


if __name__ == '__main__':
    unittest.main()
