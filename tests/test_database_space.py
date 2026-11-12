"""Compaction acts on one isolated SQLite main file and preserves user data."""
from collections import namedtuple
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

from filelock import FileLock
from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementResource, AnnouncementSource,
    Department, Subscription, User, UserAnnouncementState, UserRead)
from backend.services import database_space


class DatabaseSpaceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.root = Path(self.fixture.temp.name)
        self.path = self.root / 'test.db'
        source = Department(school_id=self.fixture.school_id, name='通知', list_url='https://example.edu.cn/notices/')
        db.session.add(source); db.session.flush()
        ann = Announcement(school_id=self.fixture.school_id, department_id=source.id,
            title='必须保留的通知', url='https://example.edu.cn/info/1.htm', content_text='已保存正文')
        db.session.add(ann); db.session.flush()
        user = User.query.one()
        db.session.add_all([
            AnnouncementSource(announcement_id=ann.id, department_id=source.id),
            UserAnnouncementState(user_id=user.id, announcement_id=ann.id, starred=True),
            UserRead(user_id=user.id, announcement_id=ann.id),
            AnnouncementResource(announcement_id=ann.id, url_key='a' * 64,
                url='https://example.edu.cn/retained.pdf', kind='attachment', state='ready',
                payload=b'favorite attachment bytes', byte_size=25),
        ])
        db.session.commit()
        self.ann_id = ann.id
        db.session.remove()

    def tearDown(self):
        self.fixture.tearDown()

    def _empty_pages(self):
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE unused_space_fixture (id INTEGER PRIMARY KEY, payload BLOB)')
            connection.exec_driver_sql('INSERT INTO unused_space_fixture(payload) VALUES (?)',
                [(b'fixture bytes' * 4096,) for _ in range(48)])
        with db.engine.begin() as connection:
            connection.exec_driver_sql('DELETE FROM unused_space_fixture')
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.exec_driver_sql('PRAGMA main.wal_checkpoint(TRUNCATE)')

    def _snapshot(self):
        db.session.remove()
        with db.engine.connect() as connection:
            tables = ['announcements', 'announcement_sources', 'announcement_resources',
                'schools', 'departments', 'subscriptions', 'user_reads', 'user_announcement_states', 'users']
            return {name: [tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM {name} ORDER BY 1, 2').all()]
                for name in tables}

    def test_stats_are_read_only_and_report_reusable_pages_without_reclaiming(self):
        self._empty_pages()
        before_bytes, before_data = self.path.stat().st_size, self._snapshot()
        statements = []
        connect = sqlite3.connect
        def traced_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(lambda statement: statements.append(statement.upper()))
            return connection
        with patch('backend.services.database_space.sqlite3.connect', side_effect=traced_connection):
            first = database_space.database_stats()
            second = database_space.database_stats()
        self.assertTrue(first['supported'])
        self.assertGreater(first['free_pages'], 0)
        self.assertEqual(first['reclaimable_bytes'], first['free_pages'] * first['page_size'])
        self.assertEqual(first, second)
        self.assertEqual(first['database_bytes'], before_bytes)
        self.assertEqual(self.path.stat().st_size, before_bytes)
        self.assertTrue(any('FREELIST_COUNT' in sql for sql in statements))
        self.assertFalse(any('VACUUM' in sql or 'WAL_CHECKPOINT' in sql for sql in statements))
        self.assertEqual(self._snapshot(), before_data)

    def test_compaction_reduces_real_file_and_preserves_favorites_subscriptions_and_bytes(self):
        self._empty_pages()
        before_data = self._snapshot()
        before_bytes = self.path.stat().st_size
        with db.engine.connect() as connection:
            journal_mode = connection.exec_driver_sql('PRAGMA journal_mode').scalar()
            original_timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar()
        # A request can have a read-only auth transaction when maintenance starts.
        self.assertEqual(User.query.count(), 1)
        result = database_space.compact_database()
        self.assertEqual(result['state'], 'reclaimed')
        self.assertTrue(result['vacuumed'])
        self.assertGreater(result['reclaimed_bytes'], 1024 * 1024)
        self.assertLess(self.path.stat().st_size, before_bytes)
        self.assertEqual(result['before_bytes'] - result['after_bytes'], result['reclaimed_bytes'])
        self.assertEqual(result['free_pages'], 0)
        self.assertEqual(self._snapshot(), before_data)
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar(), journal_mode)
            self.assertEqual(connection.exec_driver_sql('PRAGMA busy_timeout').scalar(), original_timeout)
            self.assertEqual(connection.exec_driver_sql('PRAGMA integrity_check').scalar(), 'ok')

    def test_existing_storage_lock_refuses_compaction_immediately(self):
        self._empty_pages()
        before = self._snapshot()
        started = time.monotonic()
        with FileLock(str(self.path.resolve()) + '.storage-maintenance.lock', timeout=0):
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self._snapshot(), before)

    def test_unrelated_discovery_lock_does_not_block_compaction(self):
        self._empty_pages()
        with FileLock(str(self.root / 'discovery.db') + '.worker.lock', timeout=0):
            self.assertGreater(database_space.compact_database()['reclaimed_bytes'], 0)

    def test_sqlite_writer_lock_reports_conflict_without_discarding_data(self):
        self._empty_pages()
        before = self._snapshot()
        writer = sqlite3.connect(str(self.path), timeout=0)
        try:
            writer.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
            self.assertEqual(caught.exception.status_code, 409)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            writer.rollback(); writer.close()
        self.assertEqual(self._snapshot(), before)
        self.assertGreater(database_space.database_stats()['free_pages'], 0)

    def test_wal_reader_blocks_truncation_and_cannot_be_reported_as_success(self):
        self._empty_pages()
        before = self._snapshot()
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode=WAL').scalar(), 'wal')
            connection.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
        reader = sqlite3.connect(str(self.path), timeout=0)
        try:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM announcements').fetchall()
            with db.engine.begin() as connection:
                connection.exec_driver_sql('INSERT INTO unused_space_fixture(payload) VALUES (?)', (b'new WAL frame',))
            self.assertGreater(database_space.database_stats()['wal_bytes'], 0)
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            reader.rollback(); reader.close()
        self.assertEqual(self._snapshot(), before)
        result = database_space.compact_database()
        self.assertTrue(result['vacuumed'])
        self.assertEqual(result['wal_bytes'], 0)
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar(), 'wal')

    def test_insufficient_disk_space_does_not_start_rewrite(self):
        self._empty_pages()
        before_data, before_bytes = self._snapshot(), self.path.stat().st_size
        Usage = namedtuple('Usage', 'total used free')
        with patch('backend.services.database_space.shutil.disk_usage', return_value=Usage(100, 100, 0)):
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('磁盘空间不足', str(caught.exception))
        self.assertEqual(self.path.stat().st_size, before_bytes)
        self.assertEqual(self._snapshot(), before_data)

    def test_no_free_pages_and_no_wal_is_an_explicit_noop(self):
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.exec_driver_sql('VACUUM main')
            connection.exec_driver_sql('PRAGMA main.wal_checkpoint(TRUNCATE)')
        before = self._snapshot()
        self.assertFalse(database_space.database_stats()['can_compact'])
        result = database_space.compact_database()
        self.assertEqual((result['state'], result['reclaimed_bytes'], result['vacuumed']), ('unchanged', 0, False))
        self.assertEqual(result['before_bytes'], result['after_bytes'])
        self.assertEqual(self._snapshot(), before)

    def test_unsupported_dialect_never_opens_a_connection_or_a_lock_file(self):
        with patch.object(db.engine.dialect, 'name', 'postgresql'), \
                patch.object(db.engine, 'connect', side_effect=AssertionError('No SQLite connection')), \
                patch('backend.services.database_space.FileLock', side_effect=AssertionError('No local file lock')):
            self.assertFalse(database_space.database_stats()['supported'])
            with database_space.storage_maintenance_lock() as path:
                self.assertIsNone(path)
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
            self.assertEqual(caught.exception.status_code, 400)

    def test_memory_database_is_unsupported_and_never_interpreted_as_a_path(self):
        from flask import Flask
        app = Flask('memory-database-space-test')
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        with app.app_context(), patch('backend.services.database_space.FileLock', side_effect=AssertionError('No lock path')):
            self.assertFalse(database_space.database_stats()['supported'])
            with database_space.storage_maintenance_lock() as path:
                self.assertIsNone(path)
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
            self.assertEqual(caught.exception.status_code, 400)
            db.session.remove(); db.engine.dispose()

    def test_uncommitted_application_change_is_not_silently_rolled_back(self):
        ann = db.session.get(Announcement, self.ann_id)
        ann.title = '尚未保存的用户修改'
        with self.assertRaises(database_space.DatabaseSpaceError) as caught:
            database_space.compact_database()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(ann, db.session.dirty)
        self.assertEqual(ann.title, '尚未保存的用户修改')

    def test_missing_database_file_is_not_recreated(self):
        from flask import Flask
        missing = self.root / 'must-not-create.db'
        app = Flask('missing-database-space-test')
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(missing),
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        with app.app_context(), patch.object(db.engine, 'connect', side_effect=AssertionError('No missing database open')):
            with self.assertRaises(database_space.DatabaseSpaceError):
                database_space.database_stats()
            with self.assertRaises(database_space.DatabaseSpaceError):
                database_space.compact_database()
            self.assertFalse(missing.exists())
            db.session.remove(); db.engine.dispose()

    def test_existing_delete_journal_mode_is_preserved_without_global_engine_hooks(self):
        self._empty_pages()
        before = self._snapshot()
        db.engine.dispose()
        with closing(sqlite3.connect(str(self.path), isolation_level=None)) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
        with patch.object(db.engine, 'connect', side_effect=AssertionError('No journal-changing engine hook')):
            self.assertGreater(database_space.database_stats()['free_pages'], 0)
            self.assertGreater(database_space.compact_database()['reclaimed_bytes'], 0)
        with closing(sqlite3.connect(str(self.path), isolation_level=None)) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(self._snapshot(), before)

    def test_exclusive_legacy_writer_is_bounded_without_engine_initialization_wait(self):
        self._empty_pages()
        before = self._snapshot()
        db.engine.dispose()
        writer = sqlite3.connect(str(self.path), timeout=0, isolation_level=None)
        try:
            self.assertEqual(writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
            writer.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertRaises(database_space.DatabaseSpaceError) as caught:
                database_space.compact_database()
            self.assertEqual(caught.exception.status_code, 409)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            writer.rollback(); writer.close()
        self.assertEqual(self._snapshot(), before)


if __name__ == '__main__':
    unittest.main()
