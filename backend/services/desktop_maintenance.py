"""Cooperative desktop pause at fetch boundaries, with durable resume metadata."""
from datetime import datetime, timedelta
import threading

from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask
from backend.services import tasks


MARKER = 'desktop_pause_started_at'


def resume_tasks():
    marker = AppConfig.query.filter_by(key=MARKER).first()
    if marker is None:
        return
    started = datetime.fromisoformat(marker.value)
    now = datetime.utcnow()
    duration = max(timedelta(), now - started)
    # Waiting for human verification must remain waiting. Preserve retry delays,
    # checkpoints, attempts and generations; only the paused clock moves forward.
    for task in BackgroundTask.query.filter(BackgroundTask.state == 'pending').all():
        if task.deadline_at and task.queued_at and task.queued_at <= started:
            task.deadline_at += duration
        if task.error_code == 'desktop_paused':
            task.available_at = now
            task.error = task.error_code = ''
    db.session.delete(marker)
    db.session.commit()


class WorkerMaintenance:
    def __init__(self, app):
        self.app = app
        self.pause = threading.Event()
        self.current = None
        self.control = None
        if app.config.get('DESKTOP_MODE'):
            from pathlib import Path
            from desktop.maintenance import Maintenance
            self.control = Maintenance(Path(app.config['SOURCE_CATALOG_PATH']).parent)
            with app.app_context():
                resume_tasks()

    def poll(self, active):
        if not self.control:
            return False
        request = self.control.request()
        if request:
            if self.current is None:
                with self.app.app_context():
                    AppConfig.set(MARKER, datetime.utcnow().isoformat())
            self.current = request
            self.pause.set()
            self.control.report(request, 'pausing' if active else 'paused', active)
            return request['action'] == 'stop' and not active
        if self.current:
            with self.app.app_context():
                resume_tasks()
            self.pause.clear()
            self.control.report(self.current, 'running', active)
            self.current = None
        return False


def before_fetch(pause):
    tasks.assert_owned()
    if pause is not None and pause.is_set():
        handle = tasks.current_execution()
        tasks.defer(capability=handle['capability'], phase=handle['phase'],
                    checkpoint=handle.get('checkpoint'), reason='更改文件位置，抓取已暂停',
                    error_code='desktop_paused', keep_place=True)
