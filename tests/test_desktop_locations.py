"""Location preferences and lossless moves use only disposable directories."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sys


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pref = self.root / 'preferences.json'
        self.env = patch.dict(os.environ, {'WATCHER_LOCATION_SETTINGS': str(self.pref)})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_saved_data_and_download_locations_survive_restart(self):
        from desktop.locations import save_locations, configured_data_dir, downloads_dir
        data, downloads = self.root / '通知 数据', self.root / '安装包'
        save_locations(data_dir=data, download_dir=downloads)
        self.assertEqual(configured_data_dir(), data)
        self.assertEqual(downloads_dir(data), downloads)

    def test_partial_registry_write_rolls_back_previous_locations(self):
        from desktop.locations import save_locations, REGISTRY_FIELDS
        before = {name: (str(self.root / field), 1) for field, name in REGISTRY_FIELDS.items()}
        registry = dict(before)
        fake = MagicMock(REG_SZ=1, HKEY_CURRENT_USER=0)
        fake.QueryValueEx.side_effect = lambda key, name: registry[name]
        failed = False
        def write(key, name, reserved, kind, value):
            nonlocal failed
            if name == 'CacheDirectory' and not failed:
                failed = True
                raise PermissionError('registry write denied')
            registry[name] = value, kind
        fake.SetValueEx.side_effect = write
        with patch.dict(os.environ), patch.dict(sys.modules, {'winreg': fake}), \
                patch('desktop.locations.sys.platform', 'win32'), \
                patch('desktop.locations.load_locations', return_value={field: value[0] for field, value in
                    ((f, before[n]) for f, n in REGISTRY_FIELDS.items())}):
            os.environ.pop('WATCHER_LOCATION_SETTINGS', None)
            with self.assertRaises(PermissionError):
                save_locations(data_dir=self.root / 'new', download_dir=self.root / 'downloads-new',
                               cache_dir=self.root / 'cache-new')
        self.assertEqual(registry, before)

    def test_default_download_directory_follows_data(self):
        from desktop.locations import downloads_dir
        self.assertEqual(downloads_dir(self.root / 'data'), self.root / 'data' / 'updates')

    def test_move_preserves_sqlite_wal_content_secrets_and_original(self):
        from desktop.locations import migrate_data
        source, target = self.root / 'source', self.root / 'target'
        source.mkdir()
        (source / '.env').write_text('FIELD_ENC_KEY=isolated-test-key', encoding='utf-8')
        conn = sqlite3.connect(source / 'school_watcher.db')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE reading (id INTEGER PRIMARY KEY, starred INTEGER, read_at TEXT)')
        conn.execute("INSERT INTO reading VALUES (1,1,'2026-09-28')"); conn.commit()
        (source / 'desktop-instance.json').write_text('{"token":"expired"}')
        migrate_data(source, target)
        with closing(sqlite3.connect(target / 'school_watcher.db')) as copied:
            self.assertEqual(copied.execute('SELECT * FROM reading').fetchall(), [(1, 1, '2026-09-28')])
        self.assertEqual((target / '.env').read_text(), (source / '.env').read_text())
        self.assertTrue((source / 'school_watcher.db').is_file())
        self.assertFalse((target / 'desktop-instance.json').exists())
        self.assertFalse((target / 'school_watcher.db-wal').exists())

    def test_refuses_nested_existing_or_application_targets(self):
        from desktop.locations import migrate_data, LocationError
        source = self.root / 'source'; source.mkdir()
        (source / 'notice.txt').write_text('keep')
        occupied = self.root / 'occupied'; occupied.mkdir()
        (occupied / 'keep.txt').write_text('unrelated')
        program = self.root / 'program'; program.mkdir()
        for target in (source / 'nested', self.root, occupied, program / 'data'):
            with self.subTest(target=target), self.assertRaises(LocationError):
                migrate_data(source, target, program_dir=program)
        self.assertEqual((occupied / 'keep.txt').read_text(), 'unrelated')
        self.assertEqual((source / 'notice.txt').read_text(), 'keep')

    def test_copy_failure_leaves_source_and_preferences_unchanged(self):
        from desktop.locations import migrate_data, save_locations
        source, target = self.root / 'source', self.root / 'target'
        source.mkdir(); (source / 'notice.txt').write_text('keep')
        save_locations(data_dir=source)
        before = self.pref.read_bytes()
        with patch('desktop.locations.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): migrate_data(source, target)
        self.assertFalse(target.exists())
        self.assertEqual(self.pref.read_bytes(), before)
        self.assertEqual((source / 'notice.txt').read_text(), 'keep')

    def test_new_install_can_select_an_empty_data_location(self):
        from desktop.locations import migrate_data
        target = self.root / 'selected'
        migrate_data(self.root / 'not-created', target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_all_locations_migrate_and_originals_remain(self):
        from desktop.locations import save_locations, apply_locations, effective_locations
        source = self.root / 'old'; source.mkdir()
        (source / '.field-key').write_text('secret-fixture')
        (source / 'discovery_cache.sqlite3').write_bytes(b'fixture-cache')
        (source / 'discovery_cache.sqlite3.snapshots.sqlite3').write_bytes(b'fixture-html')
        (source / 'backups').mkdir()
        (source / 'backups' / 'watcher-test.zip').write_bytes(b'backup')
        save_locations(data_dir=source)
        target, cache, backups, downloads = (self.root / x for x in ('new', 'cache', 'backups', 'downloads'))
        apply_locations(target, downloads, cache, backups)
        self.assertEqual(effective_locations()['cache'], cache)
        self.assertEqual((cache / 'discovery_cache.sqlite3').read_bytes(), b'fixture-cache')
        self.assertEqual((cache / 'discovery_cache.sqlite3.snapshots.sqlite3').read_bytes(), b'fixture-html')
        self.assertFalse((target / 'discovery_cache.sqlite3.snapshots.sqlite3').exists())
        self.assertEqual((backups / 'watcher-test.zip').read_bytes(), b'backup')
        self.assertEqual((target / '.field-key').read_text(), 'secret-fixture')
        self.assertTrue((source / '.field-key').exists())

    def test_defaults_follow_data_move_and_second_move(self):
        from desktop.locations import save_locations, apply_locations, effective_locations
        source = self.root / 'old'; source.mkdir()
        save_locations(data_dir=source)
        for name in ('new', 'newer'):
            target = self.root / name
            apply_locations(target)
            self.assertEqual(effective_locations()['data'], target)
            self.assertEqual(effective_locations()['cache'], target)
            self.assertEqual(effective_locations()['backups'], target / 'backups')

    def test_uninstall_removes_owned_files_but_preserves_unrelated_files(self):
        from desktop.locations import save_locations, remove_personal_data
        data, cache, backups = (self.root / x for x in ('data', 'cache', 'backup'))
        for path in (data, cache, backups): path.mkdir()
        (data / 'school_watcher.db').write_bytes(b'fixture')
        (data / '.field-key').write_bytes(b'secret')
        (data / 'personal.txt').write_bytes(b'keep')
        (cache / 'discovery_cache.sqlite3').write_bytes(b'fixture')
        (cache / 'discovery_cache.sqlite3.snapshots.sqlite3').write_bytes(b'html')
        (cache / 'personal.txt').write_bytes(b'keep')
        (backups / 'watcher-20260928T120000123456.zip').write_bytes(b'fixture')
        (backups / 'personal.zip').write_bytes(b'keep')
        save_locations(data_dir=data, cache_dir=cache, backup_dir=backups)
        remove_personal_data()
        self.assertFalse((data / 'school_watcher.db').exists())
        self.assertFalse((cache / 'discovery_cache.sqlite3').exists())
        self.assertFalse((cache / 'discovery_cache.sqlite3.snapshots.sqlite3').exists())
        self.assertFalse((backups / 'watcher-20260928T120000123456.zip').exists())
        self.assertTrue((data / 'personal.txt').exists())
        self.assertTrue((cache / 'personal.txt').exists())
        self.assertTrue((backups / 'personal.zip').exists())

    def test_running_profile_blocks_migration_and_uninstall(self):
        from desktop.locations import save_locations, apply_locations, remove_personal_data, LocationError
        from filelock import FileLock
        source = self.root / 'old'; source.mkdir()
        (source / '.field-key').write_text('keep')
        save_locations(data_dir=source)
        with FileLock(str(source / 'desktop-app.lock')):
            with self.assertRaises(LocationError): apply_locations(self.root / 'new')
            with self.assertRaises(LocationError): remove_personal_data()
        self.assertEqual((source / '.field-key').read_text(), 'keep')


if __name__ == '__main__': unittest.main()
