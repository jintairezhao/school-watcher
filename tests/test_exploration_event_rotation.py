"""Physical rotation must preserve executed ancestry on real local parses."""
from pathlib import Path
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3
import tempfile
import unittest
import hashlib
import zipfile
from unittest.mock import patch

from backend.services.discovery_cache import DiscoveryCache
from backend.scraper.discovery.inventory_crawler import inspect_page

ROOT = 'https://event-archive-fixture.example/'
HOME = '<title>Local execution</title><nav><a href="/unit/">院系入口</a></nav>'
CHILD = '<title>Local unit</title><p>Observed unit page</p>'


class ExplorationEventRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = DiscoveryCache(self.root/'progress.db')
        self.key = self.cache.ensure_site('Local execution archive', ROOT)

    def inspect(self, url=ROOT, html=HOME):
        inspect_page(self.cache, self.cache.report(self.key)['site'], self.cache.get_page(self.key, url),
            fetcher=lambda address: dict(url=address, html=html, status=200), ai_assist=False)

    def events(self):
        return self.cache.report(self.key)['exploration_events']

    def rotate(self, **options):
        from backend.services.discovery_history import rotate_events
        return rotate_events(self.cache, **{'keep_recent': 0, 'older_than_days': 0, **options})

    def rows(self):
        with self.cache.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM exploration_events ORDER BY sequence')]

    def test_cleanup_rotates_event_payloads_without_changing_the_logical_history(self):
        self.inspect(); self.inspect(ROOT+'unit/', CHILD); self.inspect()
        before = self.events()
        self.assertGreater(len(before), 2)
        with patch('backend.services.discovery_history.HOT_EVENT_COUNT', 2, create=True):
            self.cache.trim(all_cache=True)
        with self.cache.connect() as connection:
            hot = connection.execute('SELECT count(*) FROM exploration_events').fetchone()[0]
        self.assertLessEqual(hot, 2, 'A bounded published window must also have physical event rotation')
        self.assertEqual(self.events(), before)

    def test_published_window_and_ancestor_gap_metadata_are_identical_after_rotation(self):
        from backend.services.exploration_events import read_events
        self.inspect(); self.inspect(ROOT+'unit/', CHILD)
        with self.cache.connect() as connection:
            expected = {maximum: read_events(connection, self.key, limit=1, maximum=maximum) for maximum in (2, 8)}
        self.rotate()
        with self.cache.connect() as connection:
            actual = {maximum: read_events(connection, self.key, limit=1, maximum=maximum) for maximum in (2, 8)}
        self.assertEqual(actual, expected)
        self.assertTrue(actual[2][1]['missing_parent_ids'])
        self.assertFalse(actual[8][1]['missing_parent_ids'])

    def test_first_discovery_and_monotonic_sequence_survive_all_payloads_being_cold(self):
        self.inspect(); self.inspect(ROOT+'unit/', CHILD)
        original = self.events()
        discovered = next(event for event in original if event['event_type'] == 'page_discovered')
        self.rotate()
        self.assertEqual(self.rows(), [])
        self.inspect(ROOT+'unit/', CHILD)
        events = self.events()
        self.assertEqual(sum(event['event_type'] == 'page_discovered' for event in events), 1)
        self.assertEqual(events[-2]['parent_event_id'], discovered['id'])
        self.assertGreater(events[-2]['sequence'], original[-1]['sequence'])

    def test_missing_explicit_event_id_cannot_borrow_a_matching_old_inspection(self):
        from backend.services.exploration_events import _inspection_parent
        from backend.services.exploration_event_store import EventReader
        self.inspect(); self.rotate()
        material = self.cache.get_page(self.key, ROOT)['content_hash']
        with self.cache.connect() as connection:
            self.assertIsNone(EventReader(connection).get(self.key, None, url=ROOT))
            self.assertIsNone(_inspection_parent(connection, self.key, ROOT, material,
                dict(site_key=self.key, url=ROOT, content_hash=material, event_id=None)))

    def test_hot_count_policy_is_independent_for_each_site(self):
        from backend.services.discovery_history import rotate_events
        self.inspect(); self.inspect()
        other = self.cache.ensure_site('Another local school', 'https://other-event-fixture.example/')
        inspect_page(self.cache, self.cache.report(other)['site'], self.cache.get_page(other, 'https://other-event-fixture.example/'),
            fetcher=lambda address: dict(url=address, html='<p>Other school</p>', status=200), ai_assist=False)
        before = {key: self.cache.report(key)['exploration_events'] for key in (self.key, other)}
        rotate_events(self.cache, keep_recent=2, older_than_days=0)
        with self.cache.connect() as connection:
            self.assertEqual(dict(connection.execute('SELECT site_key,count(*) FROM exploration_events GROUP BY site_key')),
                {self.key: 2, other: 2})
        self.assertEqual({key: self.cache.report(key)['exploration_events'] for key in before}, before)

    def test_age_policy_archives_old_payloads_even_when_count_is_below_limit(self):
        from backend.services.discovery_history import rotate_events
        self.inspect()
        with self.cache.connect() as connection:
            connection.execute("UPDATE exploration_events SET observed_at='2026-08-01T23:20:00-03:00'")
        original = self.events()
        result = rotate_events(self.cache, keep_recent=4000, older_than_days=30,
            now=datetime(2026, 10, 22, tzinfo=timezone.utc))
        self.assertEqual(result['archived_events'], len(original))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.events(), original)
        with self.cache.connect() as connection:
            days = {row[0] for row in connection.execute('SELECT observation_day FROM discovery_archive_segments')}
        self.assertEqual(days, {'2026-08-02'})

    def test_file_write_failure_preserves_unarchived_event_payloads(self):
        self.inspect(); expected = self.rows()
        with patch('backend.services.discovery_history._write_segment', side_effect=OSError('fixture archive full')):
            with self.assertRaisesRegex(OSError, 'archive full'):
                self.rotate()
        self.assertEqual(self.rows(), expected)
        with self.cache.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM exploration_event_archive_index').fetchone()[0], 0)

    def test_index_commit_failure_rolls_back_deletion_and_retry_reuses_immutable_file(self):
        from backend.services.discovery_history import ensure_event_archive_schema, archive_root
        self.inspect(); expected = self.events()
        with self.cache.connect() as connection:
            ensure_event_archive_schema(connection)
            connection.execute("CREATE TRIGGER fixture_reject_event_index BEFORE INSERT ON exploration_event_archive_index "
                "BEGIN SELECT RAISE(ABORT,'fixture cold index failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cold index failure'):
            self.rotate()
        self.assertEqual(self.events(), expected)
        self.assertEqual(len(self.rows()), len(expected))
        self.assertEqual(len(list(archive_root(self.cache.path).rglob('*.json.gz'))), 1)
        with self.cache.connect() as connection:
            connection.execute('DROP TRIGGER fixture_reject_event_index')
        self.rotate()
        self.assertEqual(self.events(), expected)
        self.assertEqual(len(list(archive_root(self.cache.path).rglob('*.json.gz'))), 1)

    def test_reread_and_rotation_do_not_repeat_archived_payloads(self):
        self.inspect(); self.inspect(ROOT+'unit/', CHILD)
        before = self.events(); self.rotate()
        self.assertEqual(self.rotate()['archived_events'], 0)
        self.assertEqual(self.events(), before)
        self.assertEqual(self.events(), before)

    def test_archived_ids_and_sequences_cannot_be_reintroduced_as_new_hot_events(self):
        self.inspect(); original = self.rows()[0]; self.rotate()
        with self.cache.connect() as connection:
            for sequence, identity in ((100, original['event_id']), (original['sequence'], 'fixture-new-id')):
                with self.subTest(sequence=sequence, identity=identity), self.assertRaisesRegex(sqlite3.IntegrityError, 'already archived'):
                    connection.execute('INSERT INTO exploration_events(sequence,event_id,site_key,url,event_type,observed_at,payload_json) '
                        'VALUES(?,?,?,?,?,?,?)', (sequence, identity, self.key, ROOT, 'exploration_started', original['observed_at'], '{}'))

    def test_corrupt_or_missing_event_file_does_not_become_an_unknown_parent(self):
        from backend.services.discovery_history import archive_root
        self.inspect(); self.rotate()
        path = next(archive_root(self.cache.path).rglob('*.json.gz'))
        raw = path.read_bytes(); path.write_bytes(b'x'*len(raw))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.events()
        path.write_bytes(raw); path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.events()

    def test_modified_locator_metadata_cannot_override_original_event_contents(self):
        self.inspect(); self.rotate()
        with self.cache.connect() as connection:
            connection.execute("UPDATE exploration_event_archive_index SET url='https://wrong.example/'")
        with self.assertRaisesRegex(ValueError, 'original record'):
            self.events()

    def test_lost_query_index_rows_cannot_make_retained_history_appear_empty(self):
        self.inspect(); self.rotate()
        with self.cache.connect() as connection:
            connection.execute('DELETE FROM exploration_event_archive_index')
        with self.assertRaisesRegex(ValueError, 'missing records'):
            self.events()

    def test_lost_query_index_table_cannot_hide_retained_event_segments(self):
        self.inspect(); self.rotate()
        with self.cache.connect() as connection:
            connection.execute('DROP TABLE exploration_event_archive_index')
        with self.assertRaisesRegex(ValueError, 'query index is missing'):
            self.events()

    def test_malformed_or_oversized_payloads_are_not_silently_dropped(self):
        self.inspect()
        with self.cache.connect() as connection:
            connection.execute("UPDATE exploration_events SET payload_json='[]'")
        expected = self.rows()
        with self.assertRaisesRegex(ValueError, 'Invalid exploration archive record'):
            self.rotate()
        self.assertEqual(self.rows(), expected)
        with self.cache.connect() as connection:
            connection.execute("UPDATE exploration_events SET payload_json=?", (json.dumps({'large': 'x'*100}),))
        expected = self.rows()
        with patch('backend.services.discovery_history.MAX_RECORD_BYTES', 32):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.rotate()
        self.assertEqual(self.rows(), expected)

    def test_reader_snapshot_survives_concurrent_physical_rotation(self):
        from backend.services.exploration_events import read_events
        self.inspect(); self.inspect(ROOT+'unit/', CHILD)
        with self.cache.connect() as connection:
            connection.execute('BEGIN')
            before = read_events(connection, self.key)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self.rotate).result(timeout=10)
            self.assertEqual(read_events(connection, self.key), before)
        with self.cache.connect() as connection:
            self.assertEqual(read_events(connection, self.key), before)

    def test_reader_opened_before_first_rotation_keeps_its_original_storage_snapshot(self):
        from backend.services.exploration_event_store import EventReader
        self.inspect(); expected = self.rows()
        with self.cache.connect() as connection:
            reader = EventReader(connection)
            self.rotate()
            self.assertEqual(reader.query(self.key), expected)
            self.assertEqual(reader.total(self.key), len(expected))

    def test_republishing_after_rotation_keeps_the_same_published_execution_window(self):
        from backend.services.runtime_catalog import RuntimeCatalog
        self.inspect(); self.inspect(ROOT+'unit/', CHILD)
        catalog = RuntimeCatalog(self.root/'catalog.db')
        catalog.publish(self.cache, self.key)
        before = catalog.monitor_snapshot(self.key)
        self.rotate(); catalog.publish(self.cache, self.key)
        # Standalone compatibility publication updates its publication time;
        # facts and execution history must be identical across wall-clock ticks.
        after = catalog.monitor_snapshot(self.key)
        self.assertEqual({key: value for key, value in after.items() if key != 'catalogue_published_at'},
            {key: value for key, value in before.items() if key != 'catalogue_published_at'})

    def test_cleanup_reclaims_large_hot_payload_pages_without_losing_ids(self):
        # File-size behavior only; these are marked fixture records, not real
        # observations or independent website/threshold samples.
        self.inspect()
        with self.cache.connect() as connection:
            connection.executemany('INSERT INTO exploration_events(event_id,site_key,url,event_type,observed_at,payload_json) '
                'VALUES(?,?,?,?,?,?)', [(f'fixture-size-{n}', self.key, ROOT, 'fixture_storage_only',
                '2026-10-01T00:00:00Z', json.dumps(dict(fixture_only='x'*16384))) for n in range(100)])
        with self.cache.connect() as connection:
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = self.cache.path.stat().st_size
        original = self.events()
        with patch('backend.services.discovery_history.HOT_EVENT_COUNT', 2):
            self.cache.trim(all_cache=True)
        self.assertLess(self.cache.path.stat().st_size, before//2)
        self.assertEqual(self.events(), original)


class NavigationRotationIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_navigation_execution_events import NavigationExecutionEventTests
        self.f = NavigationExecutionEventTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from backend.services.discovery_history import rotate_events
        self.rotate = lambda store=None: rotate_events(store or self.f.store, keep_recent=0, older_than_days=0)

    def test_actual_navigation_application_resumes_from_the_exact_archived_inspection(self):
        original = next(event for event in self.f.events() if event['event_type'] == 'page_inspection_finished')
        self.rotate(); self.f.run_application()
        start = next(event for event in self.f.events() if event['event_type'] == 'navigation_application_started')
        self.assertEqual(start['parent_event_id'], original['id'])
        self.assertEqual(start['content_hash'], original['content_hash'])

    def test_application_completion_counts_children_archived_while_the_application_is_active(self):
        method = DiscoveryCache.enqueue
        def enqueue_and_rotate(store, *args, **kwargs):
            result = method(store, *args, **kwargs)
            self.rotate(store)
            return result
        with patch.object(DiscoveryCache, 'enqueue', autospec=True, side_effect=enqueue_and_rotate):
            self.f.run_application()
        final = next(event for event in self.f.events() if event['event_type'] == 'navigation_application_finished')
        self.assertEqual(final['new_page_count'], 1)
        discovered = next(event for event in self.f.events() if event['event_type'] == 'page_discovered')
        self.assertEqual(discovered['parent_event_id'], final['parent_event_id'])

    def test_actual_source_install_retains_receipt_ancestors_read_from_cold_files(self):
        from backend.services.runtime_catalog import RuntimeCatalog
        method = RuntimeCatalog.publish
        def rotate_before_publish(catalog, store, *args, **kwargs):
            self.rotate(store)
            return method(catalog, store, *args, **kwargs)
        self.rotate()
        with patch.object(RuntimeCatalog, 'publish', autospec=True, side_effect=rotate_before_publish):
            version = self.f.connect()
        body = version.execution_json
        self.assertEqual(body['upstream_status'], 'recorded')
        self.assertFalse(body['page_receipt']['missing_parent_id'])
        types = {event['event_type'] for event in body['page_receipt']['events']}
        self.assertTrue({'navigation_application_started', 'page_discovered', 'page_inspection_finished'} <= types)

    def test_physical_rotation_and_republication_do_not_change_business_structure_revision(self):
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.structure_versions import current_version
        self.f.connect()
        catalog = RuntimeCatalog(self.f.app.config['SOURCE_CATALOG_PATH'])
        # connect() updates the live directory; pin that baseline before testing rotation.
        catalog.publish(self.f.store, self.f.key)
        report = catalog.monitor_snapshot(self.f.key)
        before = current_version(self.f.school.id)
        self.rotate()
        self.assertEqual(current_version(self.f.school.id), before)
        catalog.publish(self.f.store, self.f.key)
        self.assertEqual(current_version(self.f.school.id), before)
        after = catalog.monitor_snapshot(self.f.key)
        for field in ('pages','structure_nodes','navigation_edges'):
            self.assertEqual(after[field], report[field])


class EventRotationBackupTests(unittest.TestCase):
    def setUp(self):
        import test_storage_management as fixtures
        from backend.services.discovery_history import rotate_events
        from backend.services.exploration_event_store import EventReader
        self.f = fixtures.StorageManagementTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.cache = DiscoveryCache(self.f.app.config['DISCOVERY_CACHE_PATH'])
        self.key = self.cache.ensure_site('Local backup execution archive', ROOT)
        for url, html in ((ROOT, HOME), (ROOT+'unit/', CHILD)):
            inspect_page(self.cache, self.cache.report(self.key)['site'], self.cache.get_page(self.key, url),
                fetcher=lambda address: dict(url=address, html=html, status=200), ai_assist=False)
        with self.cache.connect() as connection:
            self.expected = EventReader(connection).query(self.key)
        rotate_events(self.cache, keep_recent=0, older_than_days=0)

    def save(self):
        from backend.services.backups import create_backup
        result = create_backup()
        return self.f.root/'backups'/result['backup']

    def test_full_restore_retains_cold_records_and_future_sequence_after_empty_hot_table(self):
        from backend.services.backups import restore_backup
        from backend.services.source_inventory import Inventory
        from backend.services.exploration_event_store import EventReader
        destination = self.f.root/'restored'
        restore_backup(self.save(), destination)
        recovered = Inventory(destination/self.cache.path.name)
        with recovered.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM exploration_events').fetchone()[0], 0)
            self.assertEqual(EventReader(connection).query(self.key), self.expected)
        inspect_page(recovered, recovered.report(self.key)['site'], recovered.get_page(self.key, ROOT+'unit/'),
            fetcher=lambda address: dict(url=address, html=CHILD, status=200), ai_assist=False)
        events = recovered.report(self.key)['exploration_events']
        self.assertGreater(events[-2]['sequence'], self.expected[-1]['sequence'])
        discovered = next(event for event in events if event['event_type'] == 'page_discovered')
        self.assertEqual(events[-2]['parent_event_id'], discovered['id'])

    def test_backup_rejects_missing_cold_index_rows(self):
        with self.cache.connect() as connection:
            connection.execute('DELETE FROM exploration_event_archive_index WHERE sequence=(SELECT min(sequence) FROM exploration_event_archive_index)')
        with self.assertRaisesRegex(ValueError, 'missing records'):
            self.save()
        self.assertEqual(list((self.f.root/'backups').glob('watcher-*.zip')), [])

    def test_backup_rejects_index_fingerprint_changes_even_with_valid_files(self):
        with self.cache.connect() as connection:
            connection.execute('UPDATE exploration_event_archive_index SET record_sha256=?', ('0'*64,))
        with self.assertRaisesRegex(ValueError, 'original record'):
            self.save()

    def test_restore_checks_inner_index_after_the_outer_zip_checksum(self):
        from backend.services.backups import restore_backup
        saved = self.save()
        temporary = self.f.root/'tampered-progress.db'
        broken = self.f.root/'broken.zip'
        with zipfile.ZipFile(saved) as source:
            names = source.namelist()
            values = {name: source.read(name) for name in names}
        info = json.loads(values['backup-info.json'])
        manifest = json.loads(values['manifest.json'])
        discovery_file = info['discovery_file']
        temporary.write_bytes(values[discovery_file])
        with closing(sqlite3.connect(temporary)) as writer:
            with writer:
                writer.execute("UPDATE exploration_event_archive_index SET url='https://wrong-archive.example/'")
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        values[discovery_file] = temporary.read_bytes()
        manifest[discovery_file] = len(values[discovery_file])
        info['checksums'][discovery_file] = hashlib.sha256(values[discovery_file]).hexdigest()
        values['backup-info.json'] = json.dumps(info).encode()
        values['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(broken, 'w') as output:
            for name in names:
                output.writestr(name, values[name])
        with self.assertRaisesRegex(ValueError, 'original record'):
            restore_backup(broken, self.f.root/'rejected')


if __name__ == '__main__':
    unittest.main()
