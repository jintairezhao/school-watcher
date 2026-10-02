"""HTTP, task-continuation and frozen-denominator integration tests, no external calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, User, Subscription, BackgroundTask, Announcement, Department, AppConfig
from backend.services import tasks
from backend.services.onboarding_acceptance import freeze_scope, acceptance_report, enqueue_scope
from backend.services.school_registry import ensure_school
from backend.routes.subscriptions import subscribe_school
from backend.worker import dispatch


class OnboardingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watcher-onboarding-')
        path = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'integration-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + (path / 'db.sqlite3').as_posix(),
            'SOURCE_CATALOG_PATH': str(path / 'catalog.sqlite3'),
            'DISCOVERY_CACHE_PATH': str(path / 'scratch.sqlite3')})
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(username='admin', password_hash='unused', role='admin'),
                                User(username='reader', password_hash='unused', role='user')])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove(); db.engine.dispose()
        self.temp.cleanup()

    def client(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=uid, _csrf_token='token')
        return client

    def test_one_hundred_subscribers_share_school_and_activity(self):
        with self.app.app_context():
            db.session.add_all(User(username=f'user{i}', password_hash='unused') for i in range(100))
            db.session.commit()
            ids = [u.id for u in User.query.filter(User.username.like('user%')).all()]
        def work(uid):
            with self.app.app_context():
                school, _ = ensure_school('并发学校', 'https://parallel.edu.cn/')
                subscribe_school(school, uid)
                ident = school.id; db.session.remove()
                return ident
        with ThreadPoolExecutor(max_workers=10) as pool:
            school_ids = list(pool.map(work, ids))
        self.assertEqual(len(set(school_ids)), 1)
        with self.app.app_context():
            self.assertEqual(Subscription.query.count(), 100)
            self.assertEqual(School.query.one().subscriber_count, 100)
            self.assertEqual(BackgroundTask.query.filter_by(kind='scrape', state='pending').count(), 1)
            self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 1)
            self.assertEqual(AppConfig.query.filter_by(key='discovery_started_1').count(), 1)

    def test_concurrent_initial_discovery_markers_are_idempotent(self):
        from sqlalchemy import event
        from backend.services.discovery_changes import ensure_initial
        with self.app.app_context():
            school, _ = ensure_school('并发学校', 'https://parallel.edu.cn/')
            school_id = school.id
            tasks.enqueue('discover', school_id, {'school_id': school_id})
            engine = db.engine
        inserting = Barrier(2)

        def synchronize_inserts(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO app_config '):
                inserting.wait(timeout=10)

        def work(_):
            with self.app.app_context():
                return ensure_initial(db.session.get(School, school_id)).id

        event.listen(engine, 'before_cursor_execute', synchronize_inserts)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                task_ids = list(pool.map(work, range(2)))
        finally:
            event.remove(engine, 'before_cursor_execute', synchronize_inserts)
        self.assertEqual(len(set(task_ids)), 1)
        with self.app.app_context():
            self.assertEqual(AppConfig.query.filter_by(key=f'discovery_started_{school_id}').one().value, '1')
            self.assertEqual(BackgroundTask.query.filter_by(kind='discover').one().generation, 1)

    def test_continuation_yields_same_directory_task_without_retry(self):
        with self.app.app_context():
            school, _ = ensure_school('例校', 'https://example.edu.cn/')
            payload = {'name': school.name, 'root_url': school.url, 'refresh': True, 'trigger': 'manual_changes'}
            tasks.enqueue('directory', 'fixture', payload)
            handle = tasks.claim(worker_id='integration-worker', capabilities=['directory'])
            fake = {'continuation_required': True, 'pending_pages': 17, 'activated_ids': []}
            with tasks.execution_scope(handle), patch('backend.services.discovery_cache.adapt_site', return_value=fake), \
                    patch('backend.services.directory_options.sync_directory_options'):
                with self.assertRaises(tasks.TaskDeferred) as raised:
                    dispatch('directory', payload)
            deferred = raised.exception
            self.assertEqual(deferred.checkpoint['pending_pages'], 17)
            self.assertEqual(deferred.phase, 'directory_slice')
            tasks.handoff(handle, deferred)
            row = BackgroundTask.query.one()
            self.assertEqual(row.state, 'pending')
            self.assertEqual(row.attempts, 0)
            self.assertTrue(row.checkpoint['directory_refresh_started'])

    def test_frozen_report_keeps_unregistered_and_unsubscribed_schools(self):
        entries = [{'name': '未注册学校', 'url': 'https://one.edu.cn/'},
                   {'name': '零订阅学校', 'url': 'https://two.edu.cn/'}]
        with self.app.app_context(), patch('backend.services.onboarding_acceptance.catalog_entries', return_value=entries):
            school, _ = ensure_school('零订阅学校', entries[1]['url'])
            snapshot = freeze_scope()
            report = acceptance_report(snapshot)
            self.assertEqual(report['total_schools'], 2)
            self.assertEqual(report['ready_schools'], 0)
            self.assertFalse(report['all_schools_ready'])
            self.assertEqual({r['state'] for r in report['schools']}, {'not_registered', 'not_checked'})
            enqueue_scope(snapshot); enqueue_scope(snapshot)
            self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 2)
            self.assertEqual(Announcement.query.count(), 0)
            self.assertEqual(Department.query.count(), 0)
            snapshot['schools'].pop()
            with self.assertRaisesRegex(ValueError, '固定版本'):
                acceptance_report(snapshot)

    def test_management_requires_admin_and_batch_requires_explicit_ids(self):
        headers = {'X-CSRF-Token': 'token'}
        reader, admin = self.client(2), self.client(1)
        self.assertEqual(reader.get('/api/admin/source-proposals').status_code, 403)
        self.assertEqual(reader.post('/api/admin/source-proposals/1/review', json={'action': 'recheck'}, headers=headers).status_code, 403)
        self.assertEqual(admin.post('/api/summarize', json={}, headers=headers).status_code, 400)
        self.assertEqual(admin.get('/admin/sources').status_code, 200)
        self.assertEqual(admin.get('/api/admin/source-proposals').get_json()['total'], 0)

    def test_shared_site_url_does_not_merge_campus_onboarding_jobs(self):
        entries = [{'name': '学校甲校区', 'url': 'https://shared.edu.cn/'},
                   {'name': '学校乙校区', 'url': 'https://shared.edu.cn/'}]
        with self.app.app_context(), patch('backend.services.onboarding_acceptance.catalog_entries', return_value=entries):
            snapshot = freeze_scope()
            first = enqueue_scope(snapshot)
            second = enqueue_scope(snapshot)
            self.assertEqual(first, second)
            self.assertEqual(len({row['school_id'] for row in first}), 2)
            self.assertEqual(len({row['task_id'] for row in first}), 2)


if __name__ == '__main__':
    unittest.main()
