"""Reader visits share a timed collection gate; only admins can force updates."""
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask, Department, School, Subscription, User
from backend.services import tasks
from backend.services.inbox_refresh import queue_sources


class CollectionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='collection-admission-')
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'admission-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(self.temp.name) / 'test.db'),
            'SOURCE_CATALOG_PATH': str(Path(self.temp.name) / 'catalog.db')})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        school = School(name='测试学校', url='https://example.edu.cn/', subscriber_count=2)
        self.reader = User(username='reader', password_hash='unused')
        self.admin = User(username='admin', password_hash='unused', role='admin')
        db.session.add_all([school, self.reader, self.admin]); db.session.flush()
        self.dept = Department(school_id=school.id, name='教务通知',
                               list_url='https://example.edu.cn/notice/', list_selector='ul li')
        self.other = Department(school_id=school.id, name='其他栏目', list_url='https://example.edu.cn/other/')
        db.session.add_all([self.dept, self.other]); db.session.flush()
        for user in (self.reader, self.admin):
            db.session.add(Subscription(user_id=user.id, school_id=school.id, department_ids=[self.dept.id]))
        db.session.commit()
        self.reader_id, self.admin_id = self.reader.id, self.admin.id
        self.school_id, self.dept_id = school.id, self.dept.id
        self.client = self.client_as(self.reader_id)

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def client_as(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=user_id, _csrf_token='token')
        return client

    def sync(self, payload=None, client=None):
        return (client or self.client).post('/api/inbox/sync', json=payload if payload is not None else {},
                                         headers={'X-CSRF-Token': 'token'})

    def completed_task(self, minutes=10, state='done'):
        row = tasks.enqueue('collect', self.dept_id, {'school_id': self.school_id, 'department_id': self.dept_id})
        row.state = state
        row.updated_at = row.finished_at = datetime.utcnow() - timedelta(minutes=minutes)
        row.result = {'new_count': 3}
        row.error = '官网暂不可用' if state == 'failed' else ''
        db.session.commit()
        return row

    def test_readers_cannot_force_collection_but_admins_can(self):
        self.dept.last_scraped_at = datetime.utcnow(); db.session.commit()
        for endpoint in ('/api/inbox/refresh', f'/api/scrape/{self.school_id}', '/api/scrape/all'):
            self.assertEqual(self.client.post(endpoint, json={'scope': 'all', 'manual': True},
                headers={'X-CSRF-Token': 'token'}).status_code, 403)
        self.assertEqual(BackgroundTask.query.count(), 0)
        response = self.client_as(self.admin_id).post('/api/inbox/refresh', json={'scope': 'all'},
                                                    headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(BackgroundTask.query.one().payload['department_id'], self.dept_id)

    def test_fresh_source_without_historical_task_needs_no_collection(self):
        self.dept.last_scraped_at = datetime.utcnow() - timedelta(minutes=10); db.session.commit()
        with patch('backend.scraper.engine._fetch_html', side_effect=AssertionError('No website fetch in request')):
            response = self.sync({'manual': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['scheduled'], 0)
        self.assertFalse(response.get_json()['tracked'])
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_expired_or_never_checked_sources_are_queued_with_subscription_isolation(self):
        self.dept.last_scraped_at = datetime.utcnow() - timedelta(minutes=31); db.session.commit()
        response = self.sync()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()['active'], 1)
        self.assertEqual([t.payload['department_id'] for t in BackgroundTask.query.all()], [self.dept_id])
        self.assertEqual(self.sync({'scope': 'current', 'school_id': self.school_id,
                                    'department_ids': [self.other.id]}).status_code, 403)

    def test_recent_failed_attempt_also_respects_interval_and_keeps_result(self):
        task = self.completed_task(state='failed')
        response = self.sync()
        self.assertEqual(response.status_code, 200)
        db.session.refresh(task)
        self.assertEqual(task.state, 'failed')
        self.assertEqual(task.error, '官网暂不可用')
        task.updated_at = datetime.utcnow() - timedelta(minutes=31); db.session.commit()
        self.assertEqual(self.sync().status_code, 202)
        db.session.refresh(task)
        self.assertEqual(task.state, 'pending')

    def test_latest_success_or_attempt_wins_over_older_timestamp(self):
        task = self.completed_task(minutes=40)
        self.dept.last_scraped_at = datetime.utcnow() - timedelta(minutes=2); db.session.commit()
        self.assertEqual(self.sync().get_json()['scheduled'], 0)
        self.dept.last_scraped_at = datetime.utcnow() - timedelta(minutes=40)
        task.updated_at = datetime.utcnow() - timedelta(minutes=2); db.session.commit()
        self.assertEqual(self.sync().get_json()['scheduled'], 0)

    def test_settings_take_effect_without_restarting_web_or_worker(self):
        self.completed_task(minutes=10)
        self.assertEqual(self.sync().get_json()['scheduled'], 0)
        response = self.client_as(self.admin_id).post('/api/settings', json={'interval': 5},
                                                    headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sync().get_json()['scheduled'], 1)

    def test_fresh_visit_does_not_replace_finished_tracking_pointer(self):
        self.sync()
        pointer = AppConfig.query.filter_by(key=f'inbox_refresh:user:{self.reader_id}').one()
        before = pointer.value
        task = BackgroundTask.query.one(); task.state = 'done'; task.updated_at = datetime.utcnow()
        task.result = {'new_count': 2}; db.session.commit()
        data = self.sync().get_json()
        db.session.refresh(pointer)
        self.assertEqual(pointer.value, before)
        self.assertEqual((data['scheduled'], data['done'], data['new_count']), (0, 1, 2))

    def test_active_shared_job_is_not_reordered_or_retried_by_reader(self):
        task = tasks.enqueue('collect', self.dept_id, {'school_id': self.school_id,
                                                     'department_id': self.dept_id}, delay=120)
        before = (task.available_at, task.updated_at, task.attempts)
        a = self.sync(); b = self.sync(client=self.client_as(self.admin_id))
        self.assertEqual((a.get_json()['active'], b.get_json()['active']), (1, 1))
        db.session.refresh(task)
        self.assertEqual((task.available_at, task.updated_at, task.attempts), before)
        self.assertEqual(BackgroundTask.query.count(), 1)

    def test_sync_requires_login_csrf_and_valid_scope(self):
        self.assertEqual(self.app.test_client().post('/api/inbox/sync', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/inbox/sync', json={}).status_code, 403)
        for payload in ({'scope': 'bad'}, {'scope': 'current', 'department_ids': [self.dept_id]},
                        {'school_id': 'bad'}, []):
            self.assertEqual(self.sync(payload).status_code, 400)
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_page_navigation_and_status_reads_never_enqueue(self):
        with patch('backend.services.inbox_refresh.queue_sources', side_effect=AssertionError('GET is read only')):
            for path in ('/', '/api/inbox/refresh', '/api/inbox/refresh?resume=1'):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_worker_uses_same_interval_but_admin_school_action_can_force(self):
        from backend.worker import dispatch
        self.dept.last_scraped_at = datetime.utcnow(); db.session.commit()
        self.assertEqual(dispatch('scrape', {'school_id': self.school_id})['queued'], 0)
        self.assertEqual(dispatch('scrape', {'school_id': self.school_id, 'manual': True})['queued'], 1)

    def test_many_visitors_share_one_task_and_do_not_reopen_recent_completion(self):
        reader_id, admin_id = self.reader_id, self.admin_id
        db.session.remove()

        def visit(user_id):
            with self.app.app_context():
                response = self.sync(client=self.client_as(user_id))
                db.session.remove()
                return response.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(status == 202 for status in pool.map(visit, [reader_id, admin_id] * 8)))
        self.assertEqual(BackgroundTask.query.count(), 1)
        task = BackgroundTask.query.one(); task.state = 'done'; task.updated_at = datetime.utcnow()
        task.result = {'new_count': 4}; db.session.commit(); db.session.remove()
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(status == 200 for status in pool.map(visit, [reader_id, admin_id] * 8)))
        self.assertEqual(BackgroundTask.query.one().state, 'done')
        self.assertEqual(BackgroundTask.query.one().result, {'new_count': 4})

    def test_enqueue_atomic_interval_gate_rechecks_a_concurrent_completion(self):
        task = self.completed_task(minutes=40)
        original = db.session.execute
        raced = False

        def finish_before_update(statement, *args, **kwargs):
            nonlocal raced
            if not raced and getattr(statement, 'is_update', False) and statement.table.name == 'background_tasks':
                raced = True
                # Simulate a worker completing after enqueue SELECT but before
                # its UPDATE. A state-only WHERE would wrongly reopen this job.
                original(db.update(BackgroundTask).where(BackgroundTask.id == task.id).values(
                    state='done', updated_at=datetime.utcnow(), result={'new_count': 9}))
            return original(statement, *args, **kwargs)

        with patch.object(db.session, 'execute', side_effect=finish_before_update):
            returned = tasks.enqueue('collect', self.dept_id, min_interval=1800)
        self.assertTrue(raced)
        self.assertEqual(returned.state, 'done')
        self.assertEqual(returned.result, {'new_count': 9})


if __name__ == '__main__':
    unittest.main()
