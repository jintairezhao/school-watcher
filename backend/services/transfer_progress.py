"""Bounded progress streaming, independent of the import's atomic DB transaction."""
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
import time
import uuid

from filelock import FileLock, Timeout
from flask import Response, current_app, stream_with_context
from itsdangerous import URLSafeTimedSerializer

from backend.database.db import db


class TransferDisconnected(Exception):
    pass


def download_folder():
    folder = Path(current_app.config['BACKUP_DIR']) / '.transfers'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='storage-transfer-download')


class Progress:
    def __init__(self, queue, stopped):
        self.queue, self.stopped = queue, stopped
        self.phase, self.last_emit = None, 0
        self.samples = deque(maxlen=40)

    def send(self, event):
        while not self.stopped.is_set():
            try:
                self.queue.put(event, timeout=.2)
                return
            except Full:
                continue
        raise TransferDisconnected()

    def __call__(self, phase, done=0, total=0, processed_bytes=None):
        if self.stopped.is_set():
            raise TransferDisconnected()
        now = time.monotonic()
        changed = phase != self.phase
        if changed:
            self.samples.clear()
            self.samples.append((now, 0))
            self.phase = phase
        if not changed and now - self.last_emit < .15 and done != total:
            return
        self.last_emit = now
        value = processed_bytes if processed_bytes is not None else done
        while len(self.samples) > 1 and now - self.samples[1][0] > 2:
            self.samples.popleft()
        duration = now - self.samples[0][0]
        speed = max(0, (value - self.samples[0][1]) / duration) if duration >= .05 else None
        self.samples.append((now, value))
        self.send(dict(type='progress', phase=phase, done=done, total=total,
                       processed_bytes=processed_bytes, speed=speed))


def streamed(operation, cleanup=None):
    """One bounded worker per profile; disconnects stop before the next DB commit."""
    app = current_app._get_current_object()
    events, stopped = Queue(maxsize=64), threading.Event()
    progress = Progress(events, stopped)

    def work():
        with app.app_context():
            try:
                folder = download_folder()
                with FileLock(str(folder / 'operation.lock'), timeout=0):
                    # Only generated files in our private scratch directory expire.
                    for item in folder.iterdir():
                        if re.fullmatch(r'[0-9a-f]{32}\.(zip|part)', item.name) and not item.is_symlink():
                            if time.time() - item.stat().st_mtime > 3600:
                                try:
                                    item.unlink(missing_ok=True)
                                except OSError:
                                    pass  # An in-flight Windows download may hold it open.
                    progress.send({'type': 'result', 'result': operation(progress)})
            except TransferDisconnected:
                db.session.rollback()
            except Timeout:
                progress.send({'type': 'error', 'error': '已有导入或导出正在进行，请稍后重试'})
            except ValueError as exc:
                progress.send({'type': 'error', 'error': str(exc)})
            except Exception:
                db.session.rollback()
                app.logger.exception('Storage transfer failed')
                if not stopped.is_set():
                    progress.send({'type': 'error', 'error': '处理未完成，请检查剩余空间后重试；未完成的导入已撤销'})
            finally:
                db.session.remove()
                if cleanup:
                    cleanup()

    @stream_with_context
    def generate():
        worker = threading.Thread(target=work, name='storage-transfer', daemon=True)
        worker.start()
        try:
            yield json.dumps({'type': 'progress', 'phase': 'preparing', 'done': 0, 'total': 0}) + '\n'
            while True:
                try:
                    event = events.get(timeout=1)
                except Empty:
                    event = {'type': 'heartbeat'}
                yield json.dumps(event, ensure_ascii=False) + '\n'
                if event['type'] in ('result', 'error'):
                    break
        finally:
            stopped.set()
            worker.join(timeout=2)

    return Response(generate(), mimetype='application/x-ndjson', headers={
        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


def export_with_progress(progress, save, user_id):
    from backend.services.data_transfer import export_data
    folder = Path(current_app.config['BACKUP_DIR']) if save else download_folder()
    folder.mkdir(parents=True, exist_ok=True)
    name = 'school-watcher-data-' + datetime.utcnow().strftime('%Y%m%dT%H%M%S%f') + '.zip'
    ident = uuid.uuid4().hex
    target = folder / (name if save else ident + '.zip')
    pending = folder / (ident + '.part')
    try:
        with export_data(progress) as archive:
            total = archive.seek(0, 2)
            archive.seek(0)
            done = 0
            progress('writing', done, total, done)
            with pending.open('xb') as output:
                while chunk := archive.read(1024 * 1024):
                    output.write(chunk)
                    done += len(chunk)
                    progress('writing', done, total, done)
                progress('flushing')
                output.flush()
                os.fsync(output.fileno())
        progress('finalizing')
        pending.replace(target)
        result = dict(saved=save, name=name, bytes=total)
        if not save:
            token = signer().dumps(dict(id=ident, user=user_id, name=name))
            result['download_url'] = '/api/storage/export/download/' + token
        return result
    finally:
        pending.unlink(missing_ok=True)
