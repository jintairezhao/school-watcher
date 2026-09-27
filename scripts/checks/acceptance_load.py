"""Repeatable shared-queue capacity fixture; never fetches a real university.

Static/dynamic/denied transport delays are simulated. This measures application
and database capacity, not browser throughput; report that distinction explicitly.
Use --url for a read-only HTTP load run against a separately provisioned fixture
deployment with real browser targets, and collect its runtime/browser metrics.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import platform
import sys
import socket
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def percentile(values, quantile=.95):
    return round(sorted(values)[min(len(values)-1, int(len(values)*quantile))] * 1000, 2) if values else None


def read_load(url, cookies, seconds, users, path='/?period=all'):
    import requests
    end = time.monotonic() + seconds
    readings, failures = [], []
    guard = threading.Lock()
    def reader(index):
        address = url[index % len(url)] if isinstance(url, list) else url
        client = requests.Session(); client.trust_env = False
        client.headers['Cookie'] = cookies[index % len(cookies)] if cookies else ''
        while time.monotonic() < end:
            started = time.monotonic()
            try:
                response = client.get(address + path, timeout=30, allow_redirects=False)
                with guard:
                    readings.append(time.monotonic() - started)
                    if response.status_code != 200:
                        failures.append(response.status_code)
            except requests.RequestException as exc:
                with guard:
                    failures.append(type(exc).__name__)
            time.sleep(max(.01, 1 - (time.monotonic() - started)))
        client.close()
    with ThreadPoolExecutor(max_workers=users) as pool:
        list(pool.map(reader, range(users)))
    return {'requests': len(readings), 'reading_p95_ms': percentile(readings),
            'http_errors': len(failures), 'error_samples': failures[:10]}


def synthetic(args):
    from backend import create_app
    from backend.database.db import db
    from backend.database.models import School, Department, Announcement, User, Subscription, BackgroundTask
    from backend.services import tasks
    from backend.worker import execute
    from backend.scraper.acquisition import FetchResult
    from waitress import create_server
    with tempfile.TemporaryDirectory(prefix='watcher-capacity-') as temp:
        root = Path(temp)
        uri = os.environ.get('WATCHER_LOAD_DATABASE_URI') or 'sqlite:///' + str(root / 'main.db')
        app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-capacity-only', 'SQLALCHEMY_DATABASE_URI': uri,
            'SOURCE_CATALOG_PATH': str(root/'catalog.db'), 'FETCH_EVIDENCE_DIR': str(root/'evidence'),
            'SQLALCHEMY_ENGINE_OPTIONS': {'pool_size': 16, 'max_overflow': 8,
                'connect_args': {'check_same_thread': False, 'timeout': 30} if uri.startswith('sqlite:') else {'options': '-c timezone=UTC'}}})
        from sqlalchemy import inspect
        with app.app_context():
            if inspect(db.engine).get_table_names():
                raise ValueError('Capacity fixtures require an EMPTY, dedicated database')
            db.create_all()
            db.session.add(School(id=1, name='容量验收样例学校', url='https://load.example.edu/', subscriber_count=args.users))
            db.session.flush()
            db.session.add_all([Department(id=i+1, school_id=1, name=f'来源 {i+1}',
                list_url=f'https://source{i+1}.example.edu/list', list_selector='li', title_selector='a',
                link_selector='a', date_selector='time') for i in range(args.sources)])
            db.session.add_all([User(id=i+1, username=f'fixture-{i+1}', password_hash='unused') for i in range(args.users)])
            db.session.flush()
            db.session.add_all([Subscription(user_id=i+1, school_id=1) for i in range(args.users)])
            db.session.add_all([Announcement(school_id=1, department_id=i % args.sources + 1,
                title=f'历史通知 {i+1}', url=f'https://archive.example.edu/{i+1}', published_at=datetime.utcnow())
                for i in range(args.sources * 10)])
            db.session.commit()
            cookies = ['session=' + app.session_interface.get_signing_serializer(app).dumps({'user_id': i+1}) for i in range(args.users)]
            for index in range(args.sources):
                tasks.enqueue('collect', index+1, {'school_id': 1, 'department_id': index+1})
        stop = threading.Event()
        waits = []
        failures = []
        def transport(request):
            index = int(request.source_id)
            if index % 10 == 0:
                return FetchResult(request.url, status=403, outcome='denied', error_code='fixture_denied', message='验收样例持续拒绝访问')
            if index % 5 == 0:
                return FetchResult(request.url, outcome='requires_render', html='<div id="app"></div><script src="/app.js"></script>')
            time.sleep(.02)
            return rendered(request)
        def rendered(request):
            return FetchResult(request.url, status=200, outcome='usable', transport='browser',
                html='<ul><li><a href="/notice/1">新学期课程安排的重要通知</a><time>2026-09-24</time></li></ul>')
        def submit(client, request):
            return 'running', None
        def poll(client, request_id):
            handle = tasks.current_execution()
            from backend.scraper.acquisition import FetchRequest
            request = FetchRequest.from_dict(handle['checkpoint']['browser']['request'])
            time.sleep(.2)
            return 'done', rendered(request)
        def collect(index):
            while not stop.is_set():
                with app.app_context():
                    handle = tasks.claim(worker_id=f'fixture-worker-{index}')
                    if handle:
                        task = db.session.get(BackgroundTask, handle['id'])
                        waits.append((datetime.utcnow() - task.queued_at).total_seconds())
                    db.session.remove()
                if handle:
                    try:
                        execute(app, handle)
                    except BaseException as exc:
                        failures.append(type(exc).__name__)
                else:
                    stop.wait(.1)
        # Web execution is separate from collection, matching the deployment.
        # A single Python process would make CPU contention an artificial limit.
        configuration = root / 'web.json'
        configuration.write_text(json.dumps({key: app.config[key] for key in
            ('TESTING', 'SECRET_KEY', 'SQLALCHEMY_DATABASE_URI', 'SOURCE_CATALOG_PATH', 'FETCH_EVIDENCE_DIR', 'SQLALCHEMY_ENGINE_OPTIONS')}), encoding='utf-8')
        configuration.chmod(0o600)
        processes, addresses = [], []
        for _ in range(args.web_processes):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve-config', str(configuration),
                '--serve-port', str(port), '--web-threads', str(args.web_threads)], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            processes.append(process); addresses.append(f'http://127.0.0.1:{port}')
        import requests
        probe = requests.Session(); probe.trust_env = False
        for address in addresses:
            for attempt in range(100):
                try:
                    if probe.get(address + '/health/live', timeout=1).status_code == 200: break
                except requests.RequestException:
                    time.sleep(.1)
            else:
                for process in processes: process.terminate(); process.wait(timeout=10)
                raise RuntimeError('Fixture web server did not start')
        probe.close()
        start = time.monotonic()
        workers = []
        try:
            with patch('backend.scraper.acquisition.coordinator.http_fetch', transport), \
                 patch('backend.scraper.acquisition.browser_client.BrowserClient.submit', submit), \
                 patch('backend.scraper.acquisition.browser_client.BrowserClient.poll', poll):
                workers = [threading.Thread(target=collect, args=(i,), daemon=True) for i in range(args.workers)]
                for worker in workers: worker.start()
                result = read_load(addresses, cookies, args.seconds, args.users)
                stop.set()
                for worker in workers: worker.join(timeout=30)
        finally:
            stop.set()
            for process in processes:
                process.terminate(); process.wait(timeout=10)
        with app.app_context():
            states = dict(Counter(row.state for row in BackgroundTask.query.all()))
            result.update(task_states=states, article_count=Announcement.query.count(), worker_errors=failures[:10],
                database=db.engine.dialect.name)
            db.session.remove(); db.engine.dispose()
        result.update(mode='simulated_transport', users=args.users, sources=args.sources,
            web_processes=args.web_processes, web_threads_per_process=args.web_threads,
            mix={'static': .8, 'dynamic': .1, 'denied': .1}, duration_seconds=round(time.monotonic()-start, 2),
            queue_wait_p95_ms=percentile(waits), browser_memory='not measured; transport is simulated',
            backlog_cleared=states.get('pending', 0) + states.get('running', 0) == 0,
            limitation='Does not establish real browser throughput, 30-minute sustained capacity, or an 8-core/16-GB server result')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--users', type=int, default=100)
    parser.add_argument('--sources', type=int, default=1000)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--web-threads', type=int, default=8)
    parser.add_argument('--web-processes', type=int, default=2)
    parser.add_argument('--serve-config', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--serve-port', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--url', help='Read-only load of an existing isolated deployment')
    parser.add_argument('--cookies', type=Path, help='JSON array of Cookie header strings; never added to the report')
    parser.add_argument('--path', default='/?period=all')
    parser.add_argument('--output', type=Path, default=ROOT/'data/source-audits/capacity.json')
    args = parser.parse_args()
    if args.serve_config:
        from backend import create_app
        from waitress import serve
        serve(create_app(json.loads(args.serve_config.read_text(encoding='utf-8'))), host='127.0.0.1',
            port=args.serve_port, threads=args.web_threads)
        return
    if min(args.users, args.sources, args.seconds, args.workers) < 1:
        parser.error('Counts and duration must be positive')
    logging.basicConfig(level=logging.CRITICAL)
    if args.url:
        cookies = json.loads(args.cookies.read_text(encoding='utf-8')) if args.cookies else []
        result = read_load(args.url.rstrip('/'), cookies, args.seconds, args.users, args.path)
        result.update(mode='external_http', users=args.users, sources='verify using deployment metrics', duration_seconds=args.seconds)
    else:
        result = synthetic(args)
    result.update(timestamp=datetime.utcnow().isoformat()+'Z', python=platform.python_version(),
        platform=platform.platform(), logical_cpus=os.cpu_count())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
