"""Archive real local parse changes before evicting their working rows."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from backend.services.discovery_cache import DiscoveryCache
from backend.scraper.discovery.inventory_crawler import inspect_page


ROOT = 'https://history-fixture.example/'


class DiscoveryHistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = DiscoveryCache(self.root / 'progress.db')
        self.key = self.cache.ensure_site('Local archive fixture', ROOT)

    def observe(self, name):
        html = f'<title>Local fixture</title><nav><a href="/{name}/">{name}</a></nav>'
        inspect_page(self.cache, self.cache.report(self.key)['site'], self.cache.get_page(self.key, ROOT),
            fetcher=lambda url: dict(url=url, html=html, status=200), ai_assist=False)

    def history(self):
        with self.cache.connect() as connection:
            return {table: [dict(row) for row in connection.execute('SELECT * FROM '+table+' ORDER BY id')]
                for table in ('edge_history', 'structure_history')}

    def archive_rows(self):
        with self.cache.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM discovery_archive_segments ORDER BY batch_number')]

    def raw_histories(self):
        from backend.services.discovery_history import iter_history
        return {table: list(iter_history(self.cache, table)) for table in ('edge_history', 'structure_history')}

    def test_cache_cleanup_archives_original_parse_rows_before_removing_them(self):
        self.observe('first'); self.observe('second')
        before = self.history()
        self.assertTrue(all(before.values()))
        events = self.cache.report(self.key)['exploration_events']
        self.cache.trim(all_cache=True)
        archive_root = Path(str(self.cache.path)+'.history')
        self.assertTrue(list(archive_root.rglob('*.json.gz')), 'Old parse records must have a durable archive')
        from backend.services.discovery_history import iter_history
        for table in before:
            self.assertEqual(list(iter_history(self.cache, table)), before[table])
            self.assertEqual(self.history()[table], [])
        self.assertEqual(self.cache.report(self.key)['exploration_events'], events)

    def test_rotating_again_is_idempotent_and_does_not_manufacture_observations(self):
        self.observe('first'); self.observe('second')
        expected = self.history()
        self.cache.trim(all_cache=True)
        manifests = self.archive_rows()
        self.cache.trim(all_cache=True)
        self.assertEqual(self.archive_rows(), manifests)
        self.assertEqual(self.raw_histories(), expected)

    def test_reused_sql_row_ids_remain_distinct_historical_batches(self):
        self.observe('first'); self.observe('second')
        first = self.history()
        self.cache.trim(all_cache=True)
        self.observe('third')
        second = self.history()
        self.assertEqual(first['edge_history'][0]['id'], second['edge_history'][0]['id'])
        self.cache.trim(all_cache=True)
        combined = self.raw_histories()
        for table in first:
            self.assertEqual(combined[table], first[table]+second[table])
        self.assertEqual(len({row['batch_number'] for row in self.archive_rows()}), 4)

    def test_physical_files_are_grouped_by_observation_utc_month_and_day(self):
        self.observe('first'); self.observe('second')
        with self.cache.connect() as connection:
            connection.execute("UPDATE edge_history SET archived_at='2026-09-01T01:20:00+08:00'")
            connection.execute("UPDATE structure_history SET archived_at='2026-10-01T01:20:00Z'")
        self.cache.trim(all_cache=True)
        self.assertEqual([row['relative_path'].split('/')[:2] for row in self.archive_rows()],
            [['2026-08', '2026-08-31'], ['2026-10', '2026-10-01']])

    def test_write_failure_keeps_working_history_current_relations_and_html(self):
        self.observe('first'); self.observe('second')
        before = self.history()
        current = self.cache.structure(self.key)
        html = self.cache.snapshot(self.key, ROOT)
        with patch('backend.services.discovery_history._write_segment', side_effect=OSError('Fixture disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.cache.trim(all_cache=True)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.cache.structure(self.key), current)
        self.assertEqual(self.cache.snapshot(self.key, ROOT), html)

    def test_database_failure_after_file_commit_keeps_rows_and_retry_deduplicates_file(self):
        self.observe('first'); self.observe('second')
        before = self.history()
        from backend.services.discovery_history import ensure_schema, archive_root
        with self.cache.connect() as connection:
            ensure_schema(connection)
            connection.execute("CREATE TRIGGER reject_archive BEFORE INSERT ON discovery_archive_segments "
                "BEGIN SELECT RAISE(ABORT,'fixture archive rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture archive rollback'):
            self.cache.trim(all_cache=True)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.archive_rows(), [])
        orphan = list(archive_root(self.cache.path).rglob('*.json.gz'))
        self.assertEqual(len(orphan), 1)
        with self.cache.connect() as connection:
            connection.execute('DROP TRIGGER reject_archive')
        self.cache.trim(all_cache=True)
        self.assertEqual(self.raw_histories(), before)
        self.assertEqual(len(list(archive_root(self.cache.path).rglob('*.json.gz'))), 2)
        self.assertTrue(orphan[0].exists())

    def test_invalid_inner_history_is_not_deleted_or_repaired_by_guessing(self):
        self.observe('first'); self.observe('second')
        with self.cache.connect() as connection:
            connection.execute("UPDATE edge_history SET records_gzip=?", (b'fixture corrupt gzip',))
        before = self.history()
        with self.assertRaises((ValueError, OSError)):
            self.cache.trim(all_cache=True)
        self.assertEqual(self.history(), before)

    def test_unknown_timestamp_does_not_get_assigned_to_today(self):
        self.observe('first'); self.observe('second')
        with self.cache.connect() as connection:
            connection.execute("UPDATE edge_history SET archived_at='2026-10-01T00:00:00'")
        with self.assertRaisesRegex(ValueError, 'time zone'):
            self.cache.trim(all_cache=True)
        self.assertTrue(self.history()['edge_history'])

    def test_corrupt_or_missing_cold_file_is_explicit_error_not_empty_history(self):
        from backend.services.discovery_history import archive_root
        self.observe('first'); self.observe('second'); self.cache.trim(all_cache=True)
        item = self.archive_rows()[0]
        path = archive_root(self.cache.path)/item['relative_path']
        original = path.read_bytes()
        path.write_bytes(b'0'*len(original))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.raw_histories()
        path.write_bytes(original)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.raw_histories()

    def test_archive_filters_sites_without_merging_same_urls_or_labels(self):
        from backend.services.discovery_history import iter_history
        self.observe('first'); self.observe('second')
        with self.cache.connect() as connection:
            row = connection.execute('SELECT * FROM edge_history').fetchone()
            entries = json.loads(gzip.decompress(row['records_gzip']))
            for entry in entries:
                entry['site_key'] = 'another-site'
            connection.execute('INSERT INTO edge_history(site_key,parent_url,archived_at,records_gzip) VALUES(?,?,?,?)',
                ('another-site', row['parent_url'], row['archived_at'], gzip.compress(json.dumps(entries).encode())))
        expected = [row for row in self.history()['edge_history'] if row['site_key'] == self.key]
        self.cache.trim(all_cache=True)
        self.assertEqual(list(iter_history(self.cache, 'edge_history', site_key=self.key)), expected)
        self.assertEqual(len(list(iter_history(self.cache, 'edge_history', site_key='another-site'))), 1)
        self.assertEqual(list(iter_history(self.cache, 'edge_history', site_key='not-seen')), [])

    def test_multiple_rotators_keep_each_original_record_once(self):
        self.observe('first'); self.observe('second')
        expected = self.history()
        from backend.services.discovery_history import rotate_history
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: rotate_history(self.cache), range(2)))
        self.assertEqual(sum(result['archived_records'] for result in results), 2)
        self.assertEqual(self.raw_histories(), expected)

    def test_large_history_uses_bounded_chunks_without_losing_records(self):
        from backend.services.discovery_history import rotate_history, iter_history
        self.observe('first'); self.observe('second')
        with self.cache.connect() as connection:
            row = connection.execute('SELECT * FROM edge_history').fetchone()
            connection.executemany('INSERT INTO edge_history(site_key,parent_url,archived_at,records_gzip) VALUES(?,?,?,?)',
                [(row['site_key'], row['parent_url'], row['archived_at'], row['records_gzip'])]*6)
        expected = self.history()['edge_history']
        with patch('backend.services.discovery_history.MAX_ROWS', 3):
            result = rotate_history(self.cache)
            self.assertEqual(list(iter_history(self.cache, 'edge_history')), expected)
        self.assertEqual(result['archived_records'], 8)
        self.assertEqual([row['record_count'] for row in self.archive_rows()], [3, 3, 1, 1])

    def test_symlinked_archive_location_is_rejected_before_history_deletion(self):
        from backend.services.discovery_history import archive_root
        self.observe('first'); self.observe('second')
        outside = self.root/'outside'; outside.mkdir()
        try:
            archive_root(self.cache.path).symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest('This Windows test account cannot create symlinks: '+type(error).__name__)
        before = self.history()
        with self.assertRaisesRegex(ValueError, 'link'):
            self.cache.trim(all_cache=True)
        self.assertEqual(self.history(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_plain_inventory_without_archive_schema_can_still_be_read_without_writes(self):
        from backend.services.discovery_history import iter_history
        from backend.services.source_inventory import Inventory
        old = Inventory(self.root/'legacy.db')
        self.assertEqual(list(iter_history(old, 'edge_history')), [])
        with old.connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_archive_state'").fetchone())

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows junction regression')
    def test_windows_junction_does_not_receive_archive_files(self):
        from backend.services.discovery_history import archive_root
        self.observe('first'); self.observe('second')
        outside = self.root/'outside'; outside.mkdir()
        marker = outside/'keep.txt'; marker.write_text('untouched')
        link = archive_root(self.cache.path)
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(created.returncode, 0, created.stderr)
        expected = self.history()
        with self.assertRaisesRegex(ValueError, 'link'):
            self.cache.trim(all_cache=True)
        self.assertEqual(self.history(), expected)
        self.assertEqual(list(outside.iterdir()), [marker])


class DiscoveryHistoryBackupTests(unittest.TestCase):
    def setUp(self):
        import test_storage_management as fixtures
        self.fixture = fixtures.StorageManagementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.app = self.fixture.root, self.fixture.app
        self.cache = DiscoveryCache(self.app.config['DISCOVERY_CACHE_PATH'])
        self.key = self.cache.ensure_site('Backup fixture', ROOT)
        for name in ('first', 'second'):
            html = f'<title>Backup fixture</title><nav><a href="/{name}/">{name}</a></nav>'
            inspect_page(self.cache, self.cache.report(self.key)['site'], self.cache.get_page(self.key, ROOT),
                fetcher=lambda url: dict(url=url, html=html, status=200), ai_assist=False)
        from backend.services.discovery_history import iter_history
        self.expected = {table: list(iter_history(self.cache, table)) for table in ('edge_history', 'structure_history')}
        self.events = self.cache.report(self.key)['exploration_events']
        self.cache.trim(all_cache=True)

    def save(self):
        from backend.services.backups import create_backup
        result = create_backup()
        return self.root/'backups'/result['backup']

    def test_full_backup_restore_keeps_all_raw_events_and_parser_archives(self):
        from backend.services.backups import restore_backup
        from backend.services.discovery_history import iter_history
        from backend.services.source_inventory import Inventory
        # Published windows are bounded; the backup must retain records beyond
        # that window, including observations with no current graph node.
        with self.cache.connect() as connection:
            connection.executemany('INSERT INTO exploration_events(event_id,site_key,url,event_type,observed_at,payload_json) '
                'VALUES(?,?,?,?,?,?)', [(f'old-event-{n}', self.key, ROOT+'historical/', 'page_inspection_started',
                '2026-09-01T00:00:00Z', '{}') for n in range(2100)])
            before = [tuple(row) for row in connection.execute('SELECT * FROM exploration_events ORDER BY sequence')]
        target = self.root/'restored'; saved = self.save()
        restored = restore_backup(saved, target)
        self.assertIn(self.cache.path.name, restored)
        recovered = Inventory(target/self.cache.path.name)
        for table in self.expected:
            self.assertEqual(list(iter_history(recovered, table)), self.expected[table])
        with recovered.connect() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM exploration_events ORDER BY sequence')], before)
        self.assertTrue(recovered.get_page(self.key, ROOT))

    def test_backup_refuses_missing_or_corrupt_required_archive(self):
        from backend.services.discovery_history import archive_root
        path = next(archive_root(self.cache.path).rglob('*.json.gz'))
        path.write_bytes(b'fixture corruption')
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            self.save()
        self.assertEqual(list((self.root/'backups').glob('watcher-*.zip')), [])

    def test_unreferenced_archive_files_are_not_silently_adopted_into_backups(self):
        from backend.services.discovery_history import archive_root
        extra = archive_root(self.cache.path)/'unreferenced.json.gz'
        extra.write_bytes(b'Not a declared observation')
        with zipfile.ZipFile(self.save()) as archive:
            self.assertFalse(any(name.endswith('unreferenced.json.gz') for name in archive.namelist()))

    def test_full_backup_can_still_be_used_for_notice_only_additive_import(self):
        from backend.services.data_transfer import read_backup, merge_data
        with self.save().open('rb') as reader:
            data = read_backup(reader)
        result = merge_data(data)
        self.assertEqual(result['added'], 0)
        self.assertEqual(len(data['schools']), 1)

    def test_storage_counts_archives_as_durable_data_and_cache_cleanup_keeps_them(self):
        from backend.services.discovery_history import archive_files
        originals = {p: p.read_bytes() for p in archive_files(self.cache.path)}
        response = self.fixture.client.get('/api/storage')
        self.assertEqual(response.status_code, 200)
        result = response.json
        files = [row for row in result['files'] if row['kind'] == 'discovery_history']
        self.assertEqual(sum(row['bytes'] for row in files), sum(len(raw) for raw in originals.values()))
        self.assertEqual(result['usage_groups']['data'], sum(row['bytes'] for row in result['files'] if row['kind'] in
            ('discovery_history', 'discovery_progress', 'main_database', 'source_catalog')))
        cleaned = self.fixture.client.post('/api/storage/cleanup', json={'mode': 'cache'}, headers=self.fixture.headers)
        self.assertEqual(cleaned.status_code, 200)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_relocated_archive_files_are_counted_even_outside_storage_root(self):
        from backend.services.discovery_history import archive_files, archive_root
        from backend.database.provider_backup import sqlite_snapshot
        with tempfile.TemporaryDirectory() as external:
            moved = Path(external)/self.cache.path.name
            sqlite_snapshot(self.cache.path, moved)
            shutil.copytree(archive_root(self.cache.path), archive_root(moved))
            with patch.dict(self.app.config, {'DISCOVERY_CACHE_PATH': str(moved)}):
                response = self.fixture.client.get('/api/storage')
            self.assertEqual(response.status_code, 200)
            result = response.json
            expected = sum(p.stat().st_size for p in archive_files(moved))
            self.assertEqual(sum(row['bytes'] for row in result['files'] if row['kind'] == 'discovery_history'), expected)
            # The retained old copy is still in STORAGE_ROOT and counted once;
            # only the currently configured archive receives the managed kind.
            old_files = [row for row in result['files'] if row['name'].replace('\\', '/').startswith(self.cache.path.name+'.history/')]
            self.assertEqual(sum(row['bytes'] for row in old_files), expected)
            self.assertEqual(sum(row['bytes'] for row in result['files']), result['total_bytes'])

    def test_restore_refuses_declared_but_absent_segment(self):
        from backend.services.backups import restore_backup
        saved = self.save()
        broken = self.root/'missing.zip'
        with zipfile.ZipFile(saved) as source, zipfile.ZipFile(broken, 'w') as output:
            names = source.namelist()
            removed = next(name for name in names if '.history/' in name)
            manifest = json.loads(source.read('manifest.json')); manifest.pop(removed)
            info = json.loads(source.read('backup-info.json')); info['checksums'].pop(removed)
            for name in names:
                if name not in (removed, 'manifest.json', 'backup-info.json'):
                    output.writestr(name, source.read(name))
            output.writestr('manifest.json', json.dumps(manifest))
            output.writestr('backup-info.json', json.dumps(info))
        with self.assertRaises(FileNotFoundError):
            restore_backup(broken, self.root/'broken-restore')


if __name__ == '__main__':
    unittest.main()
