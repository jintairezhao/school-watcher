"""Migration races use temporary profiles, never installed application data."""
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from desktop.maintenance import Maintenance, REQUEST_FILE, WebGate
from desktop.locations import effective_locations, save_locations
from desktop.migration import Migration


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out waiting for isolated test state')


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'old'
        self.data.mkdir()
        self.env = patch.dict(os.environ, {'WATCHER_LOCATION_SETTINGS': str(self.root / 'locations.json'),
            'WATCHER_DESKTOP_TOKEN': 'isolated-migration-token'})
        self.env.start()
        self.addCleanup(self.env.stop)
        save_locations(data_dir=self.data)


class ProtocolTests(ProfileTest):
    def test_atomic_status_write_retries_temporary_windows_read_lock(self):
        original = os.replace
        calls = []
        def replace(source, destination):
            calls.append(destination)
            if len(calls) == 1:
                raise PermissionError('reader still open')
            return original(source, destination)
        control = Maintenance(self.data)
        with patch('desktop.maintenance.os.replace', side_effect=replace):
            control.send('pause')
        self.assertEqual(control.request()['action'], 'pause')
        self.assertEqual(len(calls), 2)

    def test_expired_foreign_and_invalid_requests_do_not_pause(self):
        control = Maintenance(self.data)
        control.send('pause')
        self.assertEqual(control.request()['action'], 'pause')
        self.assertIsNone(Maintenance(self.data, token='another-launch').request())
        request = control.request()
        request['expires'] = time.time() - 1
        (self.data / REQUEST_FILE).write_text(json.dumps(request))
        self.assertIsNone(control.request())
        (self.data / REQUEST_FILE).write_text('[]')
        self.assertIsNone(control.request())

    def test_web_gate_drains_existing_requests_and_keeps_browser_broker_available(self):
        control = Maintenance(self.data)
        def app(env, respond):
            respond('200 OK', [])
            yield b'result'
        gate = WebGate(app, control)
        response = gate({'PATH_INFO': '/admin'}, Mock())
        self.assertEqual(next(response), b'result')
        control.send('pause')
        rejected = Mock()
        list(gate({'PATH_INFO': '/admin'}, rejected))
        self.assertEqual(rejected.call_args.args[0], '503 Service Unavailable')
        self.assertEqual(gate.state()['active_requests'], 1)
        self.assertEqual(list(gate({'PATH_INFO': '/internal/browser-origin'}, Mock())), [b'result'])
        response.close()
        self.assertEqual(gate.state(), dict(maintenance=True, active_requests=0))
        control.send('resume')
        self.assertEqual(list(gate({'PATH_INFO': '/admin'}, Mock())), [b'result'])


class MigrationTests(ProfileTest):
    def setUp(self):
        super().setUp()
        self.target = self.root / 'new'
        self.values = {key: str(self.target / path.relative_to(self.data))
                       for key, path in effective_locations(self.data).items()}
        self.runtime = Mock(data_dir=self.data, address='http://127.0.0.1:12345')
        self.worker = Mock(pid=os.getpid(), returncode=0)
        self.worker.poll.return_value = None
        self.runtime.processes = {'worker': self.worker}
        self.migration = Migration(self.runtime, self.values, timeout=.1)

    def test_pause_acknowledgement_accepts_owned_interpreter_not_foreign_process(self):
        interpreter = Mock(pid=99901)
        root = Mock()
        root.children.return_value = [interpreter]
        with patch.object(self.migration.control, 'send'), \
                patch.object(self.migration.control, 'status', return_value={
                    'phase':'paused', 'active':0, 'pid':interpreter.pid}), \
                patch('psutil.Process', return_value=root):
            self.assertTrue(self.migration._paused())
            root.children.return_value = []
            self.assertFalse(self.migration._paused())

    def test_never_copies_until_worker_and_web_are_idle_then_preserves_sqlite(self):
        with closing(sqlite3.connect(self.data / 'school_watcher.db')) as conn:
            conn.execute('CREATE TABLE notice (title TEXT)')
            conn.execute("INSERT INTO notice VALUES ('保存的通知')")
            conn.commit()
        events = []
        def paused():
            events.append('pause')
            return True
        def idle():
            events.append('drain')
            return True
        self.worker.wait.side_effect = lambda **kw: events.append('worker-exit')
        self.runtime.close.side_effect = lambda **kw: events.append('closed')
        from desktop.locations import apply_changes
        def copy(*args, **kwargs):
            self.assertEqual(events, ['pause', 'drain', 'worker-exit', 'closed'])
            return apply_changes(*args, **kwargs)
        with patch.object(self.migration, '_paused', side_effect=paused), \
                patch.object(self.migration, '_web_idle', side_effect=idle), \
                patch('desktop.migration.apply_changes', side_effect=copy):
            self.migration.run()
        self.assertEqual(self.migration.state()['phase'], 'complete')
        self.assertEqual(effective_locations()['data'], self.target)
        for folder in (self.data, self.target):
            with closing(sqlite3.connect(folder / 'school_watcher.db')) as conn:
                self.assertEqual(conn.execute('SELECT title FROM notice').fetchone(), ('保存的通知',))

    def test_pause_timeout_resumes_without_stopping_or_copying(self):
        with patch.object(self.migration, '_paused', return_value=False), \
                patch('desktop.migration.apply_changes') as copying:
            self.migration.run()
        self.assertEqual(self.migration.state()['phase'], 'error')
        self.runtime.resume_worker.assert_called_once()
        self.runtime.close.assert_not_called()
        copying.assert_not_called()
        self.assertEqual(effective_locations()['data'], self.data)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.migration.restart)

    def test_cancel_waiting_for_active_fetch_resumes_old_profile(self):
        def pausing():
            self.migration.cancel()
            return False
        with patch.object(self.migration, '_paused', side_effect=pausing), \
                patch('desktop.migration.apply_changes') as copying:
            self.migration.run()
        self.assertEqual(self.migration.state()['phase'], 'cancelled')
        self.runtime.resume_worker.assert_called_once()
        copying.assert_not_called()
        self.runtime.close.assert_not_called()

    def test_failed_service_stop_never_starts_copy(self):
        self.runtime.close.side_effect = RuntimeError('后台进程未能全部退出')
        with patch.object(self.migration, '_paused', return_value=True), \
                patch.object(self.migration, '_web_idle', return_value=True), \
                patch('desktop.migration.apply_changes') as copying:
            self.migration.run()
        copying.assert_not_called()
        self.assertTrue(self.migration.restart)
        self.assertEqual(effective_locations()['data'], self.data)

    def test_failed_copy_keeps_original_and_requests_restart(self):
        (self.data / 'saved.txt').write_text('keep')
        with patch.object(self.migration, '_paused', return_value=True), \
                patch.object(self.migration, '_web_idle', return_value=True), \
                patch('desktop.locations.shutil.copy2', side_effect=OSError('disk full')):
            self.migration.run()
        self.assertEqual(self.migration.state()['phase'], 'error')
        self.assertTrue(self.migration.restart)
        self.assertEqual(effective_locations()['data'], self.data)
        self.assertEqual((self.data / 'saved.txt').read_text(), 'keep')
        self.assertFalse((self.target / 'saved.txt').exists())

    def test_inflight_web_write_timeout_prevents_service_stop_and_copy(self):
        with patch.object(self.migration, '_paused', return_value=True), \
                patch.object(self.migration, '_web_idle', return_value=False), \
                patch('desktop.migration.apply_changes') as copying:
            self.migration.run()
        self.runtime.resume_worker.assert_called_once()
        self.runtime.close.assert_not_called()
        copying.assert_not_called()

    def test_import_export_lock_prevents_pause_or_copy(self):
        from filelock import FileLock
        folder = self.data / 'backups' / '.transfers'
        folder.mkdir(parents=True)
        with FileLock(str(folder / 'operation.lock')), patch('desktop.migration.apply_changes') as copying:
            self.migration.run()
        copying.assert_not_called()
        self.runtime.begin_maintenance.assert_not_called()
        self.assertIn('导入或导出', self.migration.state()['detail'])


class WorkerPauseTests(ProfileTest):
    def setUp(self):
        super().setUp()
        from backend import create_app
        from backend.database.db import db
        self.app = create_app({'TESTING': True, 'DESKTOP_MODE': True,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(self.data / 'school_watcher.db'),
            'SOURCE_CATALOG_PATH': str(self.data / 'source_catalog.sqlite3'),
            'BACKUP_DIR': str(self.data / 'backups')})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        from backend.database.db import db
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()

    def test_active_task_checkpoints_pauses_and_new_worker_resumes_immediately(self):
        from backend.database.db import db
        from backend.database.models import AppConfig, BackgroundTask
        from backend.services import tasks
        from backend.worker import run
        from backend.scraper.acquisition.coordinator import _assert_execution
        first = tasks.enqueue('content', 'one').id
        second = tasks.enqueue('content', 'two').id
        db.session.remove()
        entered, release = threading.Event(), threading.Event()
        errors = []
        control = Maintenance(self.data)
        def dispatch(kind, payload):
            handle = tasks.current_execution()
            if handle['id'] == first and not handle.get('checkpoint'):
                # Commit a completed page, then pause before starting the next.
                AppConfig.set('fixture_completed_page', 'saved')
                tasks.checkpoint({'page': 1})
                entered.set()
                release.wait(8)
                _assert_execution()
            return {'resumed_page': handle.get('checkpoint', {}).get('page')}
        def work():
            try:
                run(self.app, roles=('http',), concurrency=1)
            except BaseException as exc:
                errors.append(exc)
        with patch('backend.worker.dispatch', side_effect=dispatch):
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                control.send('pause')
                wait_until(lambda: (control.status() or {}).get('phase') == 'pausing')
                release.set()
                wait_until(lambda: (control.status() or {}).get('phase') == 'paused')
                db.session.remove()
                row = db.session.get(BackgroundTask, first)
                self.assertEqual((row.state, row.attempts, row.checkpoint), ('pending', 0, {'page': 1}))
                self.assertEqual(row.error_code, 'desktop_paused')
                self.assertEqual(db.session.get(BackgroundTask, second).claim_count, 0)
                self.assertEqual(AppConfig.get('fixture_completed_page'), 'saved')
                db.session.remove()
            finally:
                release.set()
                control.send('stop')
                worker.join(10)
                control.clear()
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            run(self.app, once=True, roles=('http',), concurrency=1)
        db.session.remove()
        row = db.session.get(BackgroundTask, first)
        self.assertEqual((row.state, row.attempts, row.result), ('done', 0, {'resumed_page': 1}))

    def test_resume_extends_pause_budget_but_preserves_manual_waits(self):
        from backend.database.db import db
        from backend.database.models import AppConfig
        from backend.services import tasks
        from backend.services.desktop_maintenance import MARKER, resume_tasks
        task = tasks.enqueue('content', 'pending')
        waiting = tasks.enqueue('content', 'verification')
        started = datetime.utcnow() - timedelta(minutes=3)
        task.queued_at = started - timedelta(minutes=2)
        task.deadline_at = started + timedelta(minutes=2)
        task.checkpoint = {'page': 3}
        task.error_code = 'desktop_paused'
        waiting.state = 'waiting'
        waiting.error_code = 'needs_manual'
        old_deadline = task.deadline_at
        AppConfig.set(MARKER, started.isoformat())
        resume_tasks()
        db.session.refresh(task)
        db.session.refresh(waiting)
        self.assertGreater(task.deadline_at, old_deadline + timedelta(seconds=179))
        self.assertEqual((task.checkpoint, task.error_code, task.attempts), ({'page': 3}, '', 0))
        self.assertEqual((waiting.state, waiting.error_code), ('waiting', 'needs_manual'))
        self.assertIsNone(AppConfig.get(MARKER))


if __name__ == '__main__':
    unittest.main()
