"""Ownership, cross-transport admission and browser handoff integration tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import (BackgroundTask, School, Department, RuntimeLease,
    ScrapeLog, VerificationSession)
from backend.services import tasks
from backend.services import runtime_leases


class SharedRuntimeTests(unittest.TestCase):
    database_uri = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared-fetch-')
        self.root = Path(self.temp.name)
        uri = self.database_uri or 'sqlite:///' + str(self.root / 'main.db')
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': uri,
            'SOURCE_CATALOG_PATH': str(self.root / 'catalog.db'),
            'FETCH_EVIDENCE_DIR': str(self.root / 'evidence')})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        school = School(name='测试学校', url='https://example.edu.cn', subscriber_count=1)
        db.session.add(school)
        db.session.flush()
        department = Department(school_id=school.id, name='通知', list_url='https://example.edu.cn/notices')
        db.session.add(department)
        db.session.commit()
        self.department_id, self.school_id = department.id, school.id

    def tearDown(self):
        db.session.remove()
        if self.database_uri:
            db.drop_all()
        db.engine.dispose()
        self.ctx.pop()
        self.temp.cleanup()

    def job(self, kind='collect'):
        return tasks.enqueue(kind, self.department_id,
            {'school_id': self.school_id, 'department_id': self.department_id})

    def expire(self, handle):
        before = datetime.utcnow() - timedelta(seconds=1)
        db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == handle['id']).values(lease_until=before))
        if handle.get('source_key'):
            db.session.execute(db.update(RuntimeLease).where(RuntimeLease.key == handle['source_key']).values(expires_at=before))
        db.session.commit()

    def test_one_hundred_clients_share_identity_and_only_one_worker_claims(self):
        department_id, school_id = self.department_id, self.school_id
        def enqueue(_):
            with self.app.app_context():
                result = tasks.enqueue('collect', department_id,
                    {'department_id': department_id, 'school_id': school_id}).id
                db.session.remove()
                return result
        with ThreadPoolExecutor(max_workers=10) as pool:
            ids = list(pool.map(enqueue, range(100)))
        self.assertEqual(len(set(ids)), 1)
        def claim(index):
            with self.app.app_context():
                handle = tasks.claim(worker_id=f'worker-{index}')
                db.session.remove()
                return handle
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = [handle for handle in pool.map(claim, range(16)) if handle]
        self.assertEqual(len(claims), 1)
        row = db.session.get(BackgroundTask, ids[0])
        self.assertEqual((row.claim_count, row.attempts), (1, 0))

    def test_handoffs_keep_generation_and_do_not_consume_failure_budget(self):
        row = self.job()
        first = tasks.claim(capabilities=['http'])
        deadline = row.deadline_at
        tasks.handoff(first, tasks.TaskDeferred(capability='browser', phase='render', checkpoint={'page': 2}))
        self.assertIsNone(tasks.claim(capabilities=['http']))
        second = tasks.claim(capabilities=['browser'])
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(second['checkpoint'], {'page': 2})
        tasks.finish(second, result={'new_count': 3})
        db.session.refresh(row)
        self.assertEqual((row.claim_count, row.attempts, row.deadline_at), (2, 0, deadline))

    def test_heartbeat_cannot_revive_expired_lease_or_make_completed_data_fresh(self):
        row = self.job()
        handle = tasks.claim()
        stamp = row.updated_at
        self.assertTrue(tasks.heartbeat(handle))
        db.session.refresh(row)
        self.assertEqual(row.updated_at, stamp)
        self.expire(handle)
        self.assertFalse(tasks.heartbeat(handle))
        replacement = tasks.claim()
        self.assertNotEqual(handle['token'], replacement['token'])

    def test_lost_owner_cannot_commit_business_rows(self):
        self.job()
        old = tasks.claim()
        self.expire(old)
        replacement = tasks.claim()
        with self.assertRaises(tasks.LeaseLost), tasks.execution_scope(old):
            department = db.session.get(Department, self.department_id)
            department.name = 'stale mutation'
            db.session.commit()
        db.session.rollback()
        self.assertEqual(db.session.get(Department, self.department_id).name, '通知')
        with tasks.execution_scope(replacement):
            db.session.get(Department, self.department_id).name = '有效结果'
            db.session.commit()
        self.assertEqual(db.session.get(Department, self.department_id).name, '有效结果')

    def test_configuration_change_invalidates_old_generation(self):
        self.job()
        old = tasks.claim()
        tasks.invalidate_source(self.department_id, 'new-profile')
        db.session.commit()
        with tasks.execution_scope(old), self.assertRaises(tasks.LeaseLost):
            tasks.assert_owned()
        new = tasks.claim()
        self.assertGreater(new['generation'], old['generation'])
        self.assertEqual(new['policy_version'], 'new-profile')

    def test_source_collection_and_health_cannot_run_together(self):
        self.job()
        self.job('source_health')
        handle = tasks.claim(worker_id='one')
        self.assertIsNone(tasks.claim(worker_id='two'))
        tasks.finish(handle)
        self.assertIsNotNone(tasks.claim(worker_id='two'))

    def test_old_jobs_age_ahead_of_new_body_reads(self):
        old = self.job()
        old.queued_at = datetime.utcnow() - timedelta(minutes=6)
        db.session.commit()
        tasks.enqueue('content', 9)
        self.assertEqual(tasks.claim()['id'], old.id)

    def test_checkpoint_and_manual_wait_resume_keep_original_identity(self):
        row = self.job()
        handle = tasks.claim()
        with tasks.execution_scope(handle):
            tasks.checkpoint({'page': 2})
            try:
                tasks.require_verification(str(self.department_id), 'https://example.edu.cn/notices',
                    'https://example.edu.cn', request_payload={'purpose': 'list'})
            except tasks.TaskDeferred as deferred:
                tasks.handoff(handle, deferred)
        db.session.refresh(row)
        self.assertEqual((row.state, row.attempts), ('waiting', 0))
        session = VerificationSession.query.one()
        self.assertEqual(session.request_payload['purpose'], 'list')
        self.assertEqual(tasks.resume_verification(str(self.department_id)), 0)
        session.status = 'verified'
        db.session.commit()
        self.assertEqual(tasks.resume_verification(str(self.department_id)), 1)
        db.session.refresh(row)
        self.assertEqual((row.identity, row.state, row.capability), (f'collect:{self.department_id}', 'pending', 'browser'))
        self.assertEqual(row.checkpoint['page'], 2)
        self.assertEqual(row.checkpoint['verification_id'], session.id)

    def test_host_permits_are_shared_and_cooldown_survives_release(self):
        permit, _ = runtime_leases.reserve_origin('https://example.edu.cn/a', 'http')
        blocked, wait = runtime_leases.reserve_origin('http://example.edu.cn/b', 'browser')
        self.assertIsNone(blocked)
        self.assertGreater(wait, 0)
        runtime_leases.cool_origin('https://example.edu.cn/', 60)
        runtime_leases.release_origin(permit['token'])
        blocked, wait = runtime_leases.reserve_origin('https://example.edu.cn/c', 'browser')
        self.assertIsNone(blocked)
        self.assertGreater(wait, 50)
        other, _ = runtime_leases.reserve_origin('https://other.edu.cn/a', 'browser')
        self.assertIsNotNone(other)

    def test_scheduler_lease_is_single_owner_and_old_release_is_harmless(self):
        first = runtime_leases.acquire('scheduler', 'one')
        self.assertIsNone(runtime_leases.acquire('scheduler', 'two'))
        db.session.execute(db.update(RuntimeLease).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        db.session.commit()
        second = runtime_leases.acquire('scheduler', 'two')
        runtime_leases.release(first)
        self.assertTrue(runtime_leases.renew(second))

    def test_recovery_does_not_interrupt_another_live_worker(self):
        from backend.services.scrape_logs import recover_interrupted_logs
        self.job()
        handle = tasks.claim()
        log = ScrapeLog(school_id=self.school_id, status='running', task_id=handle['id'],
                       task_generation=handle['generation'], task_token=handle['token'])
        db.session.add(log)
        db.session.commit()
        self.assertEqual(recover_interrupted_logs(), 0)
        self.expire(handle)
        tasks.claim()
        self.assertEqual(recover_interrupted_logs(), 1)
        self.assertEqual(log.status, 'interrupted')

    def test_browser_poll_handoff_does_not_repeat_http_or_submit(self):
        from backend.scraper.acquisition import FetchRequest, FetchResult, fetch_or_raise
        from backend.worker import execute
        row = self.job()
        request = FetchRequest('https://example.edu.cn/notices', purpose='list', source_id=str(self.department_id))
        shell = FetchResult(request.url, 200, '<html><body><div id="app"></div><script src="app.js"></script></body></html>')
        body = '<ul><li><a href="/notice/1.htm">关于开展本学期学生课程报名工作的通知</a><span>2026-09-22</span></li></ul>'
        rendered = FetchResult(request.url, 200, body, transport='browser')
        def dispatch(kind, payload):
            return {'url': fetch_or_raise(request).final_url}
        with patch('backend.worker.dispatch', side_effect=dispatch), \
             patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=shell) as http, \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.submit', return_value=('queued', None)) as submit, \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.poll', return_value=('done', rendered)) as poll:
            execute(self.app, tasks.claim(capabilities=['http']))
            execute(self.app, tasks.claim(capabilities=['browser']))
            db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == row.id).values(available_at=datetime.utcnow()))
            db.session.commit()
            execute(self.app, tasks.claim(capabilities=['browser']))
        db.session.refresh(row)
        self.assertEqual(row.state, 'done', row.error)
        self.assertEqual((http.call_count, submit.call_count, poll.call_count), (1, 1, 1))
        self.assertEqual(row.attempts, 0)


@unittest.skipUnless(os.environ.get('WATCHER_TEST_POSTGRES_URI'), 'set a dedicated PostgreSQL test database URI')
class PostgresSharedRuntimeTests(SharedRuntimeTests):
    database_uri = os.environ.get('WATCHER_TEST_POSTGRES_URI')


if __name__ == '__main__':
    unittest.main()
