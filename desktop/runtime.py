"""Own the local services for exactly the lifetime of the desktop application."""
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from desktop import VERSION

log = logging.getLogger(__name__)


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))


def user_data_dir():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'SchoolWatcher'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'School Watcher'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'school-watcher'


def configure(data_dir):
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    env_file = data_dir / '.env'
    from dotenv import load_dotenv
    load_dotenv(env_file)
    os.environ.update(WATCHER_DATA_DIR=str(data_dir), WATCHER_ENV_FILE=str(env_file),
                      WATCHER_DESKTOP='1', WATCHER_ENV='desktop', WATCHER_HOST='127.0.0.1',
                      WATCHER_BROWSER_HOST='127.0.0.1', WATCHER_BROWSER='1',
                      DATABASE_URL=f'sqlite:///{data_dir / "school_watcher.db"}')
    os.environ.pop('WATCHER_TRUST_PROXY', None)
    os.environ.setdefault('WATCHER_DESKTOP_TOKEN', secrets.token_hex(32))
    os.environ.setdefault('WATCHER_BROWSER_TOKEN', secrets.token_hex(32))
    bundled = resource_root() / 'browser-runtime'
    if bundled.is_dir():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled)
    return data_dir


def service_command(role, data_dir):
    prefix = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(resource_root() / 'desktop' / 'entry.py')]
    return [*prefix, '--service', role, '--data-dir', str(data_dir)]


def free_port(preferred=0):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', preferred))
        except OSError:
            sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def local_json(url, token, timeout=1):
    request = urllib.request.Request(url, headers={'X-Watcher-Token': token})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.loads(response.read(65536))


def stop_process(process):
    """Only stop the process tree we spawned, including its Chromium children."""
    import psutil
    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in [root, *children]:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([root, *children], timeout=5)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)
    process.wait(timeout=5)


class DesktopRuntime:
    def __init__(self, data_dir):
        self.data_dir = configure(data_dir)
        self.processes = {}
        self.stopped = threading.Event()
        self.guard = threading.RLock()
        self.address = None
        self.failure = None

    def spawn(self, role):
        with self.guard:
            if self.stopped.is_set():
                raise RuntimeError('应用正在退出。')
            with (self.data_dir / f'desktop-{role}.log').open('ab') as output:
                process = subprocess.Popen(service_command(role, self.data_dir), cwd=self.data_dir,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.processes[role] = process
            return process

    def wait_ready(self, role, url, token, timeout=45):
        deadline = time.monotonic() + timeout
        while not self.stopped.is_set() and time.monotonic() < deadline:
            process = self.processes[role]
            if process.poll() is not None:
                raise RuntimeError(f'{role} 服务未能启动，请查看数据目录中的日志。')
            try:
                info = local_json(url, token)
                if info.get('status') == 'ok':
                    return
            except (OSError, ValueError):
                pass
            self.stopped.wait(.2)
        raise RuntimeError(f'{role} 服务启动超时，请重启应用。')

    def start(self):
        settings = self.data_dir / 'desktop-settings.json'
        try:
            saved = json.loads(settings.read_text(encoding='utf-8'))
            preferred = int(saved.get('port', 0))
            if not 1024 <= preferred <= 65535:
                preferred = 0
        except (OSError, ValueError, TypeError):
            preferred = 0
        port = free_port(preferred)
        browser_port = free_port()
        while browser_port == port:
            browser_port = free_port()
        self.address = f'http://127.0.0.1:{port}'
        os.environ.update(WATCHER_PORT=str(port), WATCHER_PUBLIC_ORIGIN=self.address,
            WATCHER_BROWSER_PORT=str(browser_port), WATCHER_BROWSER_URL=f'http://127.0.0.1:{browser_port}',
            WATCHER_ORIGIN_BROKER_URL=self.address + '/internal/browser-origin')
        process = self.spawn('migrate')
        try:
            code = process.wait(timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('数据库准备超时，请查看 desktop-migrate.log。') from exc
        if code:
            raise RuntimeError('数据库准备失败，已有数据未被清空。请查看 desktop-migrate.log。')
        self.spawn('web')
        self.wait_ready('web', self.address + '/_desktop/health', os.environ['WATCHER_DESKTOP_TOKEN'])
        self.spawn('browser')
        self.wait_ready('browser', os.environ['WATCHER_BROWSER_URL'] + '/health', os.environ['WATCHER_BROWSER_TOKEN'])
        self.spawn('worker')
        settings.write_text(json.dumps({'port': port}), encoding='utf-8')
        threading.Thread(target=self.monitor, daemon=True).start()
        return self.address

    def monitor(self):
        restarts = []
        while not self.stopped.wait(1):
            for role in ('web', 'worker', 'browser'):
                with self.guard:
                    process = self.processes.get(role)
                    if self.stopped.is_set() or process is None or process.poll() is None:
                        continue
                restarts = [stamp for stamp in restarts if time.monotonic() - stamp < 60]
                if role == 'browser' and len(restarts) < 3:
                    restarts.append(time.monotonic())
                    try:
                        self.spawn(role)
                    except RuntimeError:
                        return
                else:
                    self.failure = f'{role} 服务意外停止，请重新打开应用。'
                    return

    def close(self):
        self.stopped.set()
        with self.guard:
            for role in ('probe', 'worker', 'browser', 'web', 'migrate'):
                process = self.processes.get(role)
                if process and process.poll() is None:
                    try:
                        stop_process(process)
                    except Exception:
                        log.exception('Could not stop owned %s process', role)


def run_service(role):
    if role == 'migrate':
        from scripts.maintenance.migrate_safely import migrate
        migrate()
        return
    if role == 'browser':
        from backend.browser_service.server import main
        main()
        return
    if role == 'probe':
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True)
            page = browser.new_page()
            page.set_content('<title>School Watcher desktop check</title>')
            assert page.title() == 'School Watcher desktop check'
            browser.close()
        return
    from backend import create_app
    app = create_app()
    if role == 'worker':
        from backend.worker import run
        run(app)
    elif role == 'web':
        from waitress import serve
        # A private ownership/readiness check, independent of account login.
        original = app.wsgi_app
        def with_health(environ, start_response):
            if environ.get('PATH_INFO') == '/_desktop/health':
                if not secrets.compare_digest(environ.get('HTTP_X_WATCHER_TOKEN', ''), os.environ['WATCHER_DESKTOP_TOKEN']):
                    start_response('403 Forbidden', [('Content-Type', 'text/plain')])
                    return [b'Forbidden']
                start_response('200 OK', [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')])
                return [json.dumps({'status': 'ok', 'version': VERSION}).encode()]
            return original(environ, start_response)
        app.wsgi_app = with_health
        serve(app, host='127.0.0.1', port=int(os.environ['WATCHER_PORT']), threads=6)
