"""20-user HTTP benchmark with the real durable worker and deterministic collection I/O.

Creates only isolated test data. External university latency is deliberately excluded.
"""
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def app_for(folder):
    from backend import create_app
    return create_app({'TESTING': True, 'SECRET_KEY': 'isolated-load-test-only',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(folder / 'load.db'),
        'SOURCE_CATALOG_PATH': str(folder / 'catalog.db'), 'DISCOVERY_CACHE_PATH': str(folder / 'scratch.db'),
        'BACKUP_DIR': str(folder / 'backups'), 'BACKUP_COPY_DIR': ''})


def fixture_worker(folder):
    from backend.worker import execute
    from backend.services import tasks
    from backend.database.models import AppConfig
    from backend.database.db import db
    app = app_for(folder)
    html = '<ul><li><a href="/info/1001/99999.htm">本科生选课安排通知（压测采集样例）</a><span>2026-09-21</span></li></ul>'
    def fetch(*args, **kwargs):
        time.sleep(.03)
        return html
    with patch('backend.scraper.engine._fetch_html', side_effect=fetch):
        while not (folder / 'stop').exists():
            with app.app_context():
                tasks.enqueue('scrape', 1, {'school_id': 1})
                handle = tasks.claim()
                AppConfig.set('worker_heartbeat', datetime.utcnow().isoformat())
            if handle:
                execute(app, handle)
            time.sleep(2)


def benchmark(seconds, users, output):
    from backend.database.db import db
    from backend.database.models import School, Department, Announcement, User, Subscription, ScrapeLog
    from waitress import create_server
    import requests
    logging.getLogger('waitress').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix='watcher-load-') as temp:
        folder = Path(temp)
        app = app_for(folder)
        with app.app_context():
            db.create_all()
            db.session.add(School(id=1, name='压测样例学校', url='https://load.example.edu.cn/', subscriber_count=users))
            db.session.flush()
            db.session.add(Department(id=1, school_id=1, name='教务通知', list_url='https://load.example.edu.cn/notices/',
                list_selector='li', title_selector='a', link_selector='a', date_selector='span', content_selector='article'))
            db.session.flush()
            db.session.execute(db.insert(Announcement), [dict(school_id=1, department_id=1,
                title=f'课程通知压测样例 {i}', url=f'https://load.example.edu.cn/info/1001/{i}.htm',
                published_at=datetime.utcnow(), content_text='', content_html='') for i in range(10000)])
            db.session.add_all([User(id=i+1, username=f'load-reader-{i}', password_hash='unused') for i in range(users)])
            db.session.flush()
            db.session.add_all([Subscription(user_id=i+1, school_id=1) for i in range(users)])
            db.session.commit()
            cookies = [app.session_interface.get_signing_serializer(app).dumps({'user_id': i+1, '_csrf_token': 'load-test'}) for i in range(users)]
        server = create_server(app, host='127.0.0.1', port=0, threads=8)
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        address = f'http://127.0.0.1:{server.effective_port}'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        log = (folder / 'collection.log').open('w', encoding='utf-8')
        collector = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(folder)],
                                     stdout=log, stderr=log, creationflags=flags, cwd=ROOT)
        timings, failures = [], []
        guard = threading.Lock()
        start = time.monotonic(); end = start + seconds
        def user(index):
            client = requests.Session(); client.trust_env = False
            client.cookies.set('session', cookies[index]); client.headers['X-CSRF-Token'] = 'load-test'
            count = 0
            while time.monotonic() < end:
                began = time.monotonic()
                path = '/?period=all' if count % 10 < 6 else '/api/subscriptions'
                method = 'GET'
                if count % 10 == 9:
                    method, path = 'POST', f'/api/announcements/{index + 1}/read'
                try:
                    response = client.request(method, address + path, timeout=20)
                    elapsed = time.monotonic() - began
                    with guard:
                        timings.append(elapsed)
                        if response.status_code != 200:
                            failures.append({'status': response.status_code, 'path': path})
                except Exception as exc:
                    with guard:
                        failures.append({'error': type(exc).__name__, 'path': path})
                count += 1
                time.sleep(max(.01, 1 - (time.monotonic() - began)))
            client.close()
        try:
            with ThreadPoolExecutor(max_workers=users) as pool:
                futures = [pool.submit(user, i) for i in range(users)]
                while any(not f.done() for f in futures):
                    time.sleep(min(20, max(1, end - time.monotonic())))
                    print(json.dumps({'elapsed_seconds': round(time.monotonic()-start), 'requests': len(timings), 'errors': len(failures)}), flush=True)
                for future in futures:
                    future.result()
        finally:
            (folder / 'stop').touch()
            try:
                collector.wait(timeout=30)
            except subprocess.TimeoutExpired:
                collector.terminate(); collector.wait(timeout=10)
            log.close()
            server.close()
            thread.join(timeout=5)
        with app.app_context():
            runs = ScrapeLog.query.count()
            collected = Announcement.query.filter_by(url='https://load.example.edu.cn/info/1001/99999.htm').count()
            db.session.remove(); db.engine.dispose()
        text = (folder / 'collection.log').read_text(encoding='utf-8', errors='replace')
        ordered = sorted(timings)
        report = {'duration_seconds': round(time.monotonic()-start, 2), 'users': users, 'seed_articles': 10000,
            'requests': len(timings), 'errors': len(failures), 'error_samples': failures[:10],
            'p95_ms': round(ordered[max(0, int(len(ordered)*.95)-1)]*1000, 2) if ordered else None,
            'collection_runs': runs, 'unique_fixture_articles': collected,
            'database_lock_errors': text.count('database is locked'),
            'external_network': 'deterministic fixture; external university latency not measured'}
        report['passed'] = bool(ordered) and report['p95_ms'] < 1000 and not failures and runs > 0 and collected == 1 and not report['database_lock_errors']
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--users', type=int, default=20)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/source-audits/runtime-load.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    if args.worker:
        fixture_worker(args.worker)
    else:
        sys.exit(0 if benchmark(args.seconds, args.users, args.output) else 1)
