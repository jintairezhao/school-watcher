"""Profile-local, expiring maintenance handshake between desktop and services."""
import json
import os
from pathlib import Path
import secrets
import time
import threading
from uuid import uuid4


REQUEST_FILE = 'desktop-maintenance.json'
STATUS_FILE = 'desktop-worker-state.json'


def _write(path, value):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value), encoding='utf-8')
        # Windows readers briefly deny replacement while their file is open.
        # Keep the old complete JSON until the atomic replacement succeeds.
        for attempt in range(40):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(.025)
    finally:
        temporary.unlink(missing_ok=True)


class Maintenance:
    def __init__(self, data, token=None):
        self.data = Path(data)
        self.token = token if token is not None else os.environ.get('WATCHER_DESKTOP_TOKEN', '')
        self.identity = uuid4().hex

    def _read(self, filename):
        try:
            value = json.loads((self.data / filename).read_text(encoding='utf-8'))
            if (isinstance(value, dict) and self.token and
                    isinstance(value.get('token'), str) and
                    secrets.compare_digest(value['token'], self.token) and
                    float(value.get('expires', 0)) > time.time()):
                return value
        except (OSError, ValueError, TypeError):
            pass
        return None

    def request(self):
        value = self._read(REQUEST_FILE)
        return value if value and value.get('action') in ('pause', 'stop') else None

    def send(self, action):
        _write(self.data / REQUEST_FILE, dict(id=self.identity, token=self.token,
            action=action, expires=time.time() + 90))

    def report(self, request, phase, active):
        _write(self.data / STATUS_FILE, dict(id=request['id'], token=self.token,
            phase=phase, active=active, pid=os.getpid(), expires=time.time() + 10))

    def status(self):
        value = self._read(STATUS_FILE)
        return value if value and value.get('id') == self.identity else None

    def clear(self):
        # A fresh desktop launch uses a fresh token; old requests cannot pause it.
        for filename in (REQUEST_FILE, STATUS_FILE):
            value = self._read(filename)
            if value and value.get('id') == self.identity:
                try:
                    (self.data / filename).unlink(missing_ok=True)
                except OSError:
                    # A resume request is already effective; a stale file expires
                    # and cannot control the next launch's token.
                    pass


class WebGate:
    """Reject new UI work during maintenance and count in-flight requests."""
    def __init__(self, application, control):
        self.application, self.control = application, control
        self.guard = threading.Lock()
        self.active = 0

    def state(self):
        with self.guard:
            return dict(maintenance=bool(self.control.request()), active_requests=self.active)

    def __call__(self, environ, start_response):
        with self.guard:
            # Existing browser reads still need their origin permits to finish.
            if self.control.request() and not environ.get('PATH_INFO', '').startswith('/internal/'):
                start_response('503 Service Unavailable', [('Content-Type', 'text/plain; charset=utf-8'),
                    ('Retry-After', '2'), ('Cache-Control', 'no-store')])
                return ['正在更改文件位置，请稍候。'.encode()]
            self.active += 1

        def response():
            iterable = None
            try:
                iterable = self.application(environ, start_response)
                yield from iterable
            finally:
                try:
                    if iterable is not None and hasattr(iterable, 'close'):
                        iterable.close()
                finally:
                    with self.guard:
                        self.active -= 1
        return response()
