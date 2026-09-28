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
    from desktop.locations import configured_data_dir
    return configured_data_dir()


def configure(data_dir):
    data_dir = Path(data_dir).resolve()
    previous = os.environ.get('WATCHER_DATA_DIR')
    if previous and os.environ.get('PLAYWRIGHT_BROWSERS_PATH') == str(Path(previous) / 'browsers'):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(data_dir / 'browsers')
    data_dir.mkdir(parents=True, exist_ok=True)
    env_file = data_dir / '.env'
    from dotenv import load_dotenv
    load_dotenv(env_file)
    os.environ.update(WATCHER_DATA_DIR=str(data_dir), WATCHER_ENV_FILE=str(env_file),
                      WATCHER_DESKTOP='1', WATCHER_ENV='desktop', WATCHER_HOST='127.0.0.1',
                      WATCHER_BROWSER_HOST='127.0.0.1', WATCHER_BROWSER='1',
                      WATCHER_SEED_ON_START='0',
                      DATABASE_URL=f'sqlite:///{data_dir / "school_watcher.db"}')
    os.environ.pop('WATCHER_TRUST_PROXY', None)
    os.environ.setdefault('WATCHER_DESKTOP_TOKEN', secrets.token_hex(32))
    os.environ.setdefault('WATCHER_BROWSER_TOKEN', secrets.token_hex(32))
    # Keep downloaded components outside the installation so app updates retain them.
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(data_dir / 'browsers'))
    from desktop.locations import effective_locations
    locations = effective_locations(data_dir)
    os.environ['WATCHER_DISCOVERY_CACHE_PATH'] = str(locations['cache'] / 'discovery_cache.sqlite3')
    os.environ['WATCHER_FETCH_EVIDENCE_DIR'] = str(locations['cache'] / 'fetch-evidence')
    os.environ['WATCHER_BACKUP_DIR'] = str(locations['backups'])
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
        self.prepare_browser()
        self.spawn('worker')
        settings.write_text(json.dumps({'port': port}), encoding='utf-8')
        threading.Thread(target=self.monitor, daemon=True).start()
        return self.address

    def open_url(self):
        return self.address + '/_desktop/open?token=' + os.environ['WATCHER_DESKTOP_TOKEN']

    def prepare_browser(self, managed=False):
        with self.guard:
            for role in ('browser-setup', 'browser-download'):
                previous = self.processes.get(role)
                if previous and previous.poll() is None:
                    return False
            self.spawn('browser-download' if managed else 'browser-setup')
            return True

    def monitor(self):
        restarts = []
        while not self.stopped.wait(1):
            repair = self.data_dir / 'desktop-browser-repair.request'
            if repair.exists() and self.prepare_browser(managed=True):
                repair.unlink(missing_ok=True)
            retry = self.data_dir / 'desktop-browser-retry.request'
            if retry.exists() and self.prepare_browser():
                retry.unlink(missing_ok=True)
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
            for role in ('probe', 'worker', 'browser', 'browser-setup', 'browser-download', 'web', 'migrate'):
                process = self.processes.get(role)
                if process and process.poll() is None:
                    try:
                        stop_process(process)
                    except Exception:
                        log.exception('Could not stop owned %s process', role)


def run_service(role):
    if role in ('browser-setup', 'browser-download'):
        from desktop.browser import prepare
        state = prepare(Path(os.environ['WATCHER_DATA_DIR']), managed=role == 'browser-download')
        if state['phase'] == 'error':
            raise RuntimeError(state['message'])
        return
    if role == 'migrate':
        from scripts.maintenance.migrate_safely import migrate
        migrate()
        return
    if role == 'browser':
        from backend.browser_service.server import main
        main()
        return
    if role == 'probe':
        from backend.scraper.discovery.parser_revision import assert_current_parser
        from backend.ai.skill_loader import load_skill
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        import tempfile
        assert_current_parser()
        load_skill('university-source-onboarding', 'classify')
        load_skill('university-source-onboarding', 'extraction')
        load_skill('summarize-university-notice', 'summary')
        with tempfile.TemporaryDirectory(dir=os.environ['WATCHER_DATA_DIR']) as folder:
            inventory = Inventory(Path(folder) / 'discovery.sqlite3')
            key = inventory.ensure_site('安装包检查', 'https://example.edu.cn/')
            result = crawl_site(inventory, key, max_pages=1, workers=1,
                fetcher=lambda url: {'html': '<title>学校</title><a href="/units/">院系设置</a>', 'url': url, 'status': 200})
            if result['states'].get('fetched', 0) != 1:
                raise RuntimeError('Packaged discovery did not parse the first page')
        from playwright.sync_api import sync_playwright
        from backend.scraper.sanitizer import sanitize_html
        with sync_playwright() as playwright:
            from desktop.browser import launch_channel
            channel = launch_channel(os.environ['WATCHER_DATA_DIR'])
            if not channel:
                raise RuntimeError('采集组件尚未准备好')
            for headless in (True, False):
                browser = playwright.chromium.launch(channel=channel, headless=headless, chromium_sandbox=True)
                page = browser.new_page()
                page.set_content('<title>School Watcher desktop check</title><script>document.title += " ready"</script>')
                assert page.title() == 'School Watcher desktop check ready'
                # Exercise the sanitizer extension inside the frozen application,
                # not merely in the Python environment used to build it.
                dirty = '<a href="java&#9;script:window.__audit_marker=1">Audit link</a><img src=x onerror="window.__audit_marker=1"><svg onload="window.__audit_marker=1"></svg>'
                page.route('**/*', lambda route: route.fulfill(status=404, body=''))
                page.set_content(sanitize_html(dirty))
                page.locator('a').click()
                page.wait_for_timeout(100)
                assert not page.evaluate('Boolean(window.__audit_marker)')
                assert page.locator('svg,script').count() == 0
                browser.close()
        return
    from backend import create_app
    app = create_app()
    if role == 'worker':
        from backend.worker import run
        run(app)
    elif role == 'web':
        from backend.auth.desktop import ensure_local_owner
        with app.app_context():
            ensure_local_owner()
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
        import shutil
        import tempfile
        # Waitress otherwise rejects >1 GiB before the per-import allowance can
        # run. Bound its disk spool by available space instead of a fixed size.
        serve(app, host='127.0.0.1', port=int(os.environ['WATCHER_PORT']), threads=6,
              max_request_body_size=shutil.disk_usage(tempfile.gettempdir()).free // 4)
