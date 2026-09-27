"""Real provider migration/restore drills; never use the application's database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from flask_migrate import upgrade
import sqlalchemy as sa

from backend import create_app
from backend.core.config import ROOT_DIR
from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementSource, AnnouncementMerge, AppConfig,
    BackgroundTask, Department, School, Subscription, User, UserRead, UserAnnouncementState,
    VerificationSession, VerificationWaiter)
from backend.services.announcement_identity import upsert_listing
from backend.services.announcement_sources import record_source
from backend.services.backups import create_backup, restore_backup
from backend.services.database_transfer import sqlite_to_postgres


@contextmanager
def postgres_database():
    import psycopg
    from psycopg import sql
    admin_url = sa.engine.make_url(os.environ['WATCHER_TEST_MIGRATION_ADMIN_URI'])
    name = 'watcher_drill_' + uuid4().hex
    connection = psycopg.connect(admin_url.set(drivername='postgresql').render_as_string(hide_password=False), autocommit=True)
    connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        yield admin_url.set(database=name, drivername='postgresql+psycopg').render_as_string(hide_password=False)
    finally:
        connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        connection.close()


class DatabasePortabilityTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='watcher-portability-')
        self.root = Path(self.scratch.name)
        self.addCleanup(self.scratch.cleanup)

    def app(self, uri=None, revision='head'):
        app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-test',
            'SQLALCHEMY_DATABASE_URI': uri or 'sqlite:///' + str(self.root / 'source.db'),
            'SOURCE_CATALOG_PATH': str(self.root / 'source_catalog.sqlite3'),
            'BACKUP_DIR': str(self.root / 'backups'), 'BACKUP_COPY_DIR': ''})
        with app.app_context():
            upgrade(directory=str(ROOT_DIR / 'migrations'), revision=revision)
            db.session.remove()
        self.addCleanup(lambda: self.dispose(app))
        return app

    @staticmethod
    def dispose(app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def seed(self):
        user = User(username='drill-reader', password_hash='preserved-password-hash', role='admin')
        school = School(name='迁移演练学校', url='https://fixture.example.edu/', subscriber_count=1)
        db.session.add_all([user, school]); db.session.flush()
        a = Department(school_id=school.id, name='栏目 A', list_url=school.url + 'a')
        b = Department(school_id=school.id, name='栏目 B', list_url=school.url + 'b')
        db.session.add_all([a, b]); db.session.flush()
        notice, _ = upsert_listing(a, '只存在于旧备份的通知', school.url + '#/notice/1')
        notice.content_text = '历史正文'
        record_source(notice, a)
        record_source(notice, b)
        db.session.add_all([Subscription(user_id=user.id, school_id=school.id, department_ids=[a.id, b.id]),
            UserRead(user_id=user.id, announcement_id=notice.id),
            UserAnnouncementState(user_id=user.id, announcement_id=notice.id, starred=True, archived=True),
            AppConfig(key='check_interval', value='30')])
        task = BackgroundTask(identity=f'collect:{a.id}', kind='collect', payload={'department_id': a.id},
                              state='waiting', capability='browser', phase='verification', generation=3)
        db.session.add(task); db.session.flush()
        session = VerificationSession(id='drill-verification', source_id=str(a.id), origin=school.url,
            url=school.url, status='active', task_id=task.id, generation='old-secret', runtime_id='old-runtime')
        db.session.add(session); db.session.flush()
        db.session.add(VerificationWaiter(task_id=task.id, session_id=session.id)); db.session.commit()
        return school.id, a.id, b.id, user.id, notice.id

    def verify_business(self, ids):
        school, a, b, user, notice = ids
        self.assertEqual(db.session.get(User, user).password_hash, 'preserved-password-hash')
        self.assertEqual(db.session.get(Announcement, notice).content_text, '历史正文')
        self.assertEqual(Subscription.query.filter_by(user_id=user).one().department_ids, [a, b])
        self.assertEqual(UserRead.query.filter_by(user_id=user, announcement_id=notice).count(), 1)
        state = db.session.get(UserAnnouncementState, (user, notice))
        self.assertTrue(state.starred and state.archived)
        self.assertEqual(AnnouncementSource.query.filter_by(announcement_id=notice).count(), 2)

    def test_old_duplicates_merge_every_relation_and_personal_state(self):
        app = self.app(revision='12a6c93f4e80')
        with app.app_context():
            metadata = sa.MetaData(); metadata.reflect(db.engine)
            with db.engine.begin() as c:
                users, schools, depts, articles = [metadata.tables[n] for n in ('users', 'schools', 'departments', 'announcements')]
                c.execute(users.insert(), {'id': 99, 'username': 'old-reader', 'password_hash': 'hash', 'role': 'user'})
                c.execute(schools.insert(), {'id': 99, 'name': 'old-school', 'url': 'https://example.edu'})
                c.execute(depts.insert(), [{'id': 98, 'school_id': 99, 'name': 'A'}, {'id': 99, 'school_id': 99, 'name': 'B'}])
                for ident, department in ((98, 98), (99, 99)):
                    c.execute(articles.insert(), {'id': ident, 'school_id': 99, 'department_id': department,
                        'title': 'same', 'url': 'https://example.edu/notice', 'content_text': 'preserved' if ident == 98 else None})
                c.execute(metadata.tables['user_reads'].insert(), {'user_id': 99, 'announcement_id': 98})
                c.execute(metadata.tables['user_announcement_states'].insert(), [
                    {'user_id': 99, 'announcement_id': 98, 'starred': True, 'archived': False},
                    {'user_id': 99, 'announcement_id': 99, 'starred': False, 'archived': True}])
            upgrade(directory=str(ROOT_DIR / 'migrations'))
            self.assertEqual(Announcement.query.count(), 1)
            self.assertEqual(db.session.get(AnnouncementMerge, 98).survivor_id, 99)
            self.assertEqual(db.session.get(Announcement, 99).content_text, 'preserved')
            self.assertEqual(AnnouncementSource.query.filter_by(announcement_id=99).count(), 2)
            self.assertEqual(UserRead.query.one().announcement_id, 99)
            self.assertTrue(db.session.get(UserAnnouncementState, (99, 99)).starred)
            self.assertTrue(db.session.get(UserAnnouncementState, (99, 99)).archived)

    def test_sqlite_backup_restore_preserves_business_state(self):
        app = self.app()
        with app.app_context():
            ids = self.seed()
            backup = create_backup()
            from backend.services.data_transfer import read_backup, merge_data
            with (self.root / 'backups' / backup['backup']).open('rb') as stream:
                portable = read_backup(stream)
            self.assertEqual(merge_data(portable)['added'], 0)
        restored = self.root / 'restored'
        restore_backup(self.root / 'backups' / backup['backup'], restored)
        restored_app = self.app('sqlite:///' + str(restored / 'source.db'))
        with restored_app.app_context():
            self.verify_business(ids)

    @unittest.skipUnless(os.environ.get('WATCHER_TEST_MIGRATION_ADMIN_URI'), 'requires isolated PostgreSQL database creation')
    def test_whole_migration_and_native_postgres_restore(self):
        source_app = self.app()
        with source_app.app_context():
            ids = self.seed()
        self.dispose(source_app)
        before = hashlib.sha256((self.root / 'source.db').read_bytes()).digest()
        with postgres_database() as uri:
            report = sqlite_to_postgres(self.root / 'source.db', uri, self.root / 'moved', stopped=True)
            self.assertTrue(report['source_unchanged'])
            self.assertEqual(before, hashlib.sha256((self.root / 'source.db').read_bytes()).digest())
            app = self.app(uri)
            with app.app_context():
                self.verify_business(ids)
                from backend.services.data_transfer import export_data, read_backup, merge_data
                portable = export_data()
                try:
                    recovered = read_backup(portable)
                finally:
                    portable.close()
                department = db.session.get(Department, ids[1])
                extra, _ = upsert_listing(department, '新库独有通知', 'https://fixture.example.edu/newer-only')
                record_source(extra, department); db.session.commit()
                merge_data(recovered)
                self.assertEqual(Announcement.query.count(), 2)
                self.verify_business(ids)
                task = BackgroundTask.query.one()
                self.assertEqual((task.state, task.capability, task.phase), ('pending', 'http', 'fetch'))
                self.assertEqual(VerificationWaiter.query.count(), 0)
                self.assertEqual(VerificationSession.query.one().status, 'expired')
                # Reset sequences: a new ORM row cannot collide with a copied ID.
                school = School(name='new school', url='https://other.example.edu')
                db.session.add(school); db.session.commit()
                self.assertGreater(school.id, ids[0])
                backup = create_backup()
            with postgres_database() as restore_uri:
                restore_backup(self.root / 'backups' / backup['backup'], self.root / 'restored-pg', postgres_url=restore_uri)
                restored = self.app(restore_uri)
                with restored.app_context():
                    self.verify_business(ids)
                self.dispose(restored)
            self.dispose(app)

    @unittest.skipUnless(os.environ.get('WATCHER_TEST_MIGRATION_ADMIN_URI'), 'requires isolated PostgreSQL database creation')
    def test_fresh_postgres_migrations_and_concurrent_article_identity(self):
        with postgres_database() as uri:
            app = self.app(uri)
            with app.app_context():
                ids = self.seed()
            def write(index):
                with app.app_context():
                    department = db.session.get(Department, ids[1 + index % 2])
                    article, created = upsert_listing(department, 'same title', 'https://fixture.example.edu/new')
                    record_source(article, department)
                    db.session.commit()
                    result = article.id, created
                    db.session.remove()
                    return result
            with ThreadPoolExecutor(max_workers=12) as pool:
                rows = list(pool.map(write, range(100)))
            self.assertEqual(len({row[0] for row in rows}), 1)
            self.assertEqual(sum(row[1] for row in rows), 1)
            with app.app_context():
                self.assertEqual(AnnouncementSource.query.filter_by(announcement_id=rows[0][0]).count(), 2)
                department = db.session.get(Department, ids[1])
                _, created = upsert_listing(department, 'same title', 'https://fixture.example.edu/#/notice/2')
                self.assertTrue(created)
                db.session.commit()
            self.dispose(app)
