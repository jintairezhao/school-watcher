"""Cache ownership waits never consume retries or restart paid discovery work."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from filelock import FileLock, Timeout

from backend import create_app
from backend.database.db import db
from backend.database.models import BackgroundTask, Department, RuntimeLease, School
from backend.services import tasks


class DiscoveryCacheContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'cache-contention-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(folder / 'main.db'),
            'SOURCE_CATALOG_PATH': str(folder / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(folder / 'cache.db')})
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.school = School(name='例校', url='https://example.edu.cn/', subscriber_count=1)
        db.session.add(self.school); db.session.flush()
        db.session.add(Department(school_id=self.school.id, name='学院通知', group_name='',
            list_url='https://example.edu.cn/notices/', list_selector='li.notice'))
        db.session.commit()
        self.cache_lock = self.app.config['DISCOVERY_CACHE_PATH'] + '.worker.lock'

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.context.pop(); self.temp.cleanup()

    def test_grouping_and_maintenance_share_the_discovery_writer_lease(self):
        for kind in ('source_grouping', 'maintenance'):
            with self.subTest(kind=kind):
                tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
                handle = tasks.claim(capabilities=['directory'])
                other = tasks.enqueue(kind, self.school.id, {'school_id': self.school.id})
                self.assertIsNone(tasks.claim(capabilities=['directory', 'http']))
                tasks.finish(handle, {'completed': True})
                resumed = tasks.claim(capabilities=['directory', 'http'])
                self.assertEqual(resumed['id'], other.id)
                self.assertEqual(resumed['source_key'], 'directory:writer')
                tasks.finish(resumed, {'completed': True})

    def test_real_cache_lock_wait_preserves_progress_attempts_and_budget(self):
        from backend.worker import execute
        row = tasks.enqueue('source_grouping', self.school.id, {'school_id': self.school.id})
        saved = {'grouping_targets': [], 'paid_result': {'execution_id': 'already-paid'}}
        row.checkpoint = saved; row.attempts = 2
        deadline = row.deadline_at
        db.session.commit()
        handle = tasks.claim(capabilities=['directory'])
        with FileLock(self.cache_lock, timeout=0), \
                patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No paid replay')):
            execute(self.app, handle)
        db.session.refresh(row)
        self.assertEqual((row.state, row.phase, row.error_code), ('pending', 'cache_wait', 'discovery_cache_busy'))
        self.assertEqual((row.attempts, row.generation, row.checkpoint), (2, handle['generation'], saved))
        self.assertEqual(row.deadline_at, deadline)
        self.assertIsNone(row.finished_at)
        self.assertIsNone(db.session.get(RuntimeLease, 'directory:writer'))
        row.available_at = datetime.utcnow() - timedelta(seconds=1); db.session.commit()
        resumed = tasks.claim(capabilities=['directory'])
        with patch('backend.services.source_grouping.run_grouping', return_value={'continuation_required': False}) as run:
            execute(self.app, resumed)
        db.session.refresh(row)
        self.assertEqual(row.state, 'done')
        self.assertEqual(resumed['checkpoint'], saved)
        self.assertEqual(run.call_count, 1)

    def test_cache_handoff_honors_a_pause_arriving_during_dispatch(self):
        from backend.worker import execute
        from backend.services.discovery_control import PAUSE_KEY, RESUME_KEY
        row = tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
        handle = tasks.claim(capabilities=['directory'])
        def interrupted(*args):
            current = db.session.get(BackgroundTask, row.id)
            current.payload = dict(current.payload, **{PAUSE_KEY: True})
            db.session.commit()
            raise Timeout(self.cache_lock)
        with patch('backend.worker.dispatch', side_effect=interrupted):
            execute(self.app, handle)
        db.session.refresh(row)
        self.assertEqual((row.state, row.phase, row.attempts), ('waiting', 'user_paused', 0))
        self.assertEqual(row.payload[RESUME_KEY]['phase'], 'cache_wait')

    def test_unrelated_file_lock_timeout_remains_a_failure(self):
        from backend.worker import execute
        from backend.worker import logger
        row = tasks.enqueue('source_grouping', self.school.id, {'school_id': self.school.id})
        handle = tasks.claim(capabilities=['directory'])
        # Migration configuration tests disable existing loggers globally.
        # This test owns its logger state rather than depending on test order.
        with patch.object(logger, 'disabled', False), self.assertLogs('backend.worker', level='ERROR'), \
                patch('backend.worker.dispatch', side_effect=Timeout(str(Path(self.temp.name) / 'unrelated.lock'))):
            execute(self.app, handle)
        db.session.refresh(row)
        self.assertEqual(row.attempts, 1)
        self.assertNotEqual(row.error_code, 'discovery_cache_busy')

    def test_upgrade_recovers_only_exact_cache_failures_in_the_same_generation(self):
        from backend.services.tasks import recover_discovery_cache_waiters
        row = tasks.enqueue('source_grouping', self.school.id, {'school_id': self.school.id})
        saved = {'grouping_sources': [{'id': 1}], 'paid_result': {'execution_id': 'already-paid'}}
        row.state = 'failed'; row.phase = 'complete'; row.error = str(Timeout(self.cache_lock))
        row.attempts = 3; row.claim_count = 8; row.generation = 4; row.checkpoint = saved
        row.result = {'retained': True}; row.finished_at = datetime.utcnow() - timedelta(days=1)
        row.deadline_at = row.finished_at + timedelta(minutes=30)
        queued = row.queued_at
        other = tasks.enqueue('navigation_review', 'other', {'school_id': self.school.id})
        other.state = 'failed'; other.error = str(Timeout(str(Path(self.temp.name) / 'other.worker.lock')))
        paused = tasks.enqueue('discover', self.school.id,
            {'school_id': self.school.id, 'discovery_pause_requested': True})
        paused.state = 'failed'; paused.error = str(Timeout(self.cache_lock))
        db.session.commit()
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No paid replay')):
            self.assertEqual(recover_discovery_cache_waiters(), 1)
            self.assertEqual(recover_discovery_cache_waiters(), 0)
        db.session.refresh(row); db.session.refresh(other); db.session.refresh(paused)
        self.assertEqual((row.state, row.phase, row.error_code), ('pending', 'cache_wait', 'discovery_cache_busy'))
        self.assertEqual((row.generation, row.attempts, row.claim_count), (4, 3, 8))
        self.assertEqual((row.checkpoint, row.result), (saved, {'retained': True}))
        self.assertEqual(row.deadline_at - row.updated_at, timedelta(minutes=30))
        self.assertEqual(row.queued_at, queued)
        self.assertIsNone(row.finished_at)
        self.assertEqual((other.state, paused.state), ('failed', 'failed'))

    def test_recovery_does_not_replenish_an_already_exhausted_budget(self):
        row = tasks.enqueue('source_grouping', self.school.id, {'school_id': self.school.id})
        row.state = 'failed'; row.error = str(Timeout(self.cache_lock))
        row.finished_at = datetime.utcnow() - timedelta(days=1)
        row.deadline_at = row.finished_at - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(tasks.recover_discovery_cache_waiters(), 1)
        self.assertIsNone(tasks.claim(capabilities=['directory']))
        db.session.refresh(row)
        self.assertEqual((row.state, row.error_code), ('failed', 'deadline_exceeded'))


if __name__ == '__main__':
    unittest.main()
