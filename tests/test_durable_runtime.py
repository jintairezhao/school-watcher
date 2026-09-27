from contextlib import closing
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import (School, Department, Announcement, User, UserAnnouncementState,
                                     BackgroundTask, Subscription)
from backend.services import tasks
from backend.services.content_cache import prune_content, fetch_content, request_content


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(self.root / 'main.db'),
                              'SOURCE_CATALOG_PATH': str(self.root / 'catalog.db'),
                              'BACKUP_DIR': str(self.root / 'backups'), 'BACKUP_COPY_DIR': ''})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        school = School(name='例校', url='https://example.edu.cn', enabled=True, subscriber_count=1)
        db.session.add(school); db.session.flush()
        dept = Department(school_id=school.id, name='教务通知', list_url=school.url, content_selector='article')
        db.session.add(dept); db.session.flush()
        self.dept_id, self.school_id = dept.id, school.id
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def article(self, **kwargs):
        ann = Announcement(school_id=self.school_id, department_id=self.dept_id, title='选课通知',
                           url='https://example.edu.cn/info/1001/1234.htm', **kwargs)
        db.session.add(ann); db.session.commit()
        return ann

    def test_atomic_deduplication_and_claim_across_connections(self):
        def enqueue():
            with self.app.app_context():
                return tasks.enqueue('scrape', 1, {'school_id': 1}).id
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(len(set(pool.map(lambda _: enqueue(), range(16)))), 1)
        def claim():
            with self.app.app_context():
                return tasks.claim()
        with ThreadPoolExecutor(max_workers=8) as pool:
            handles = [h for h in pool.map(lambda _: claim(), range(16)) if h]
        self.assertEqual(len(handles), 1)

    def test_expired_lease_recovered_and_old_worker_cannot_complete(self):
        tasks.enqueue('content', 4)
        old = tasks.claim()
        row = db.session.get(BackgroundTask, old['id'])
        row.lease_until = datetime.utcnow() - timedelta(seconds=1); db.session.commit()
        new = tasks.claim()
        self.assertNotEqual(old['token'], new['token'])
        self.assertFalse(tasks.finish(old, {'stale': True}))
        self.assertTrue(tasks.finish(new, {'correct': True}))

    def test_retry_exhaustion_and_manual_retry(self):
        row = tasks.enqueue('content', 4)
        for i in range(3):
            db.session.execute(db.update(BackgroundTask).values(available_at=datetime.utcnow()))
            db.session.commit()
            handle = tasks.claim(); tasks.finish(handle, error='offline')
        db.session.refresh(row); self.assertEqual(row.state, 'failed')
        self.assertEqual(tasks.enqueue('content', 4).state, 'pending')

    def test_cached_and_saved_content_retention(self):
        ann = self.article(content_html='<p>旧正文</p>', content_text='旧正文', content_bytes=100,
                           content_cached_at=datetime.utcnow() - timedelta(days=60),
                           content_accessed_at=datetime.utcnow() - timedelta(days=60))
        user = User(username='reader', password_hash='unused', security_answer_hash='unused')
        db.session.add(user); db.session.flush()
        pin = UserAnnouncementState(user_id=user.id, announcement_id=ann.id, starred=True)
        db.session.add(pin); db.session.commit()
        self.app.config['BODY_CACHE_BYTES'] = 1
        self.assertEqual(prune_content()['evicted'], 0)
        pin.starred = False; db.session.commit()
        self.assertEqual(prune_content()['evicted'], 1)
        db.session.refresh(ann)
        self.assertFalse(ann.content_html)
        self.assertIsNotNone(db.session.get(UserAnnouncementState, (user.id, ann.id)))

    def test_fetch_sanitizes_body_and_preserves_metadata(self):
        ann = self.article()
        with patch('backend.scraper.engine._fetch_html', return_value='<article><script>bad()</script><p>选课说明</p><a href="/files/a.pdf">附件</a></article>'):
            fetch_content(ann.id)
        db.session.refresh(ann)
        self.assertIn('选课说明', ann.content_html)
        self.assertNotIn('<script', ann.content_html)
        self.assertIn('https://example.edu.cn/files/a.pdf', ann.content_html)
        self.assertGreater(ann.content_bytes, 0)
        self.assertEqual(ann.title, '选课通知')

    def test_http_request_only_enqueues_and_respects_visibility(self):
        ann = self.article(); client = self.app.test_client()
        with client.session_transaction() as session:
            session['_csrf_token'] = 'token'
        with patch('backend.scraper.engine._fetch_html', side_effect=AssertionError('Request must not fetch')):
            response = client.post(f'/api/announcements/{ann.id}/content', headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(BackgroundTask.query.count(), 1)
        school = db.session.get(School, self.school_id); school.enabled = False; db.session.commit()
        self.assertEqual(client.get(f'/api/announcements/{ann.id}/content').status_code, 404)

    def test_backup_restores_complete_database(self):
        from backend.services.backups import create_backup, restore_backup
        import sqlite3
        self.article()
        result = create_backup()
        destination = self.root / 'restored'
        restore_backup(self.root / 'backups' / result['backup'], destination)
        with closing(sqlite3.connect(destination / 'main.db')) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM announcements').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            restore_backup(self.root / 'backups' / result['backup'], destination)

    def test_health_probes_survive_database_failure_and_login_cookie(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 123
        with patch.object(db.session, 'execute', side_effect=RuntimeError('database unavailable')):
            self.assertEqual(client.get('/health/live').status_code, 200)
            response = client.get('/health/ready')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()['status'], 'unavailable')

    def test_daily_health_checks_all_selected_columns_and_queues_one_adaptation(self):
        from backend.worker import dispatch
        user = User(username='subscriber', password_hash='unused', security_answer_hash='unused')
        db.session.add(user); db.session.flush()
        selected = [self.dept_id]
        for i in range(9):
            dept = Department(school_id=self.school_id, name=f'栏目{i}',
                              list_url=f'https://example.edu.cn/list/{i}')
            db.session.add(dept); db.session.flush()
            selected.append(dept.id)
        db.session.add(Department(school_id=self.school_id, name='未订阅栏目',
                                  list_url='https://example.edu.cn/unselected'))
        db.session.add(Subscription(user_id=user.id, school_id=self.school_id, department_ids=selected))
        db.session.commit()
        with patch('backend.scraper.engine._fetch_html', return_value='<html>list</html>') as fetch, \
             patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'needs_review'}):
            result = dispatch('health', {'school_id': self.school_id})
            self.assertEqual(fetch.call_count, 0)
            for task in BackgroundTask.query.filter_by(kind='source_health').all():
                dispatch(task.kind, task.payload)
        self.assertEqual(result['queued'], 10)
        self.assertEqual(fetch.call_count, 10)
        self.assertNotIn('https://example.edu.cn/unselected', [c.args[0] for c in fetch.call_args_list])
        self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 1)


if __name__ == '__main__':
    unittest.main()
