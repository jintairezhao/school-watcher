"""Run real source workers and a configured API in isolated, resumable storage.

The credential stays in memory. The installed application database is read-only.
Reports measure an observation window, not whole-school coverage or correctness.
Use the separately confirmed source oracle to assess coverage and attribution.
"""
import argparse
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_logical_page = ContextVar('acceptance_logical_page', default=None)


def unsent_proxy_failure(error):
    """Version-independent observation for baseline and candidate transports."""
    import aiohttp
    import requests
    from urllib3.exceptions import MaxRetryError, ProxyError, NewConnectionError
    if isinstance(error, aiohttp.ClientProxyConnectionError):
        return True
    reason = error.args[0] if error.args else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return (isinstance(error, requests.exceptions.ProxyError) and isinstance(reason, ProxyError)
            and isinstance(reason.original_error, NewConnectionError))


def pending_within_window(jobs, seconds, now=None):
    """Wait for scheduled retries/recoverable leases that fit the observation window."""
    now = now or datetime.utcnow()
    return any(stamp is not None and (stamp - now).total_seconds() <= seconds
        for stamp in (job.lease_until if job.state == 'running' else job.available_at for job in jobs))


class HTTPMeasurements:
    """Harness-only transport observations, excluding providers and private brokers.

    An attempted HTTP send is not proof the server received it. Each redirect or
    proxy/direct fallback is recorded separately; no headers or credentials are stored.
    """
    def __init__(self, records, persist, lock, school_url):
        self.records, self.persist, self.lock = records, persist, lock
        self.school_host = (urlsplit(school_url).hostname or '').removeprefix('www.')

    def destination(self, url, headers=None):
        parsed = urlsplit(str(url))
        authority = next((str(value) for key, value in (headers or {}).items()
                          if key.lower() == 'host'), parsed.netloc)
        host = (urlsplit('//' + authority).hostname or '').lower().rstrip('.')
        if not (host == self.school_host or host.endswith('.' + self.school_host)
                or host.endswith(('.edu.cn', '.ac.cn', '.edu'))):
            return None
        return host, urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ''))

    def begin(self, method, url, kwargs, transport, proxy):
        destination = self.destination(url, kwargs.get('headers'))
        if not destination:
            return None
        host, visible_url = destination
        entry = {'url': visible_url, 'host': host, 'method': str(method), 'transport': transport,
            'path': 'proxy' if proxy else 'direct', 'started_at': datetime.utcnow().isoformat() + 'Z',
            'status': None, 'bytes': 0, 'elapsed_seconds': 0, 'state': 'attempted',
            'page_read_id': _logical_page.get(), 'request_evidence': 'dispatch_started'}
        started = time.monotonic()
        with self.lock:
            self.records.append(entry)
            self.persist()
        def snapshot(response=None, error=None, complete=False):
            count = 0
            if response is not None:
                if transport == 'aiohttp':
                    count = getattr(response.content, 'total_raw_bytes', 0)
                    status = response.status
                else:
                    raw = getattr(response, 'raw', None)
                    count = raw.tell() if raw is not None else len(getattr(response, '_content', b'') or b'')
                    status = response.status_code
                with self.lock:
                    entry.update(status=status, bytes=count, state='response_received', request_evidence='response_received')
            with self.lock:
                entry.update(elapsed_seconds=round(time.monotonic() - started, 3), complete=complete)
                if error is not None:
                    entry.update(state='failed', error_code=type(error).__name__, request_evidence=
                        'proxy_connection_failed_before_send' if unsent_proxy_failure(error) else 'delivery_unknown')
                self.persist()
        return snapshot

    def install(self, stack):
        import requests
        import aiohttp
        original_sync, original_async = requests.Session.request, aiohttp.ClientSession._request
        def synchronous(session, method, url, **kwargs):
            proxy = kwargs.get('proxies') or (requests.utils.get_environ_proxies(url) if session.trust_env else {})
            observe = self.begin(method, url, kwargs, 'requests', any(proxy.values()))
            if observe is None:
                return original_sync(session, method, url, **kwargs)
            try:
                response = original_sync(session, method, url, **kwargs)
                observe(response, complete=not kwargs.get('stream', False))
                close = response.close
                def measured_close():
                    try:
                        observe(response, complete=True)
                    finally:
                        close()
                response.close = measured_close
                return response
            except BaseException as exc:
                observe(error=exc, complete=True)
                raise
        async def asynchronous(session, method, url, **kwargs):
            observe = self.begin(method, url, kwargs, 'aiohttp', kwargs.get('proxy'))
            if observe is None:
                return await original_async(session, method, url, **kwargs)
            try:
                response = await original_async(session, method, url, **kwargs)
                observe(response)
                release = response.release
                def measured_release():
                    try:
                        observe(response, complete=True)
                    finally:
                        result = release()
                    return result
                response.release = measured_release
                return response
            except BaseException as exc:
                observe(error=exc, complete=True)
                raise
        stack.enter_context(patch.object(requests.Session, 'request', synchronous))
        stack.enter_context(patch.object(aiohttp.ClientSession, '_request', asynchronous))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    # Windows readers/antivirus may briefly hold a file without delete sharing.
    # Retrying this local rename must never retry the measured provider request.
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.05 * (attempt + 1))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(args):
    repo, output = args.repo.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'report.json'
    if (output / 'test.db').exists() != report_path.exists():
        raise SystemExit('The resumable database and report ledger must both be retained; refusing a budget reset')
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    os.environ.update(WATCHER_DATA_DIR=str(output), WATCHER_ENV_FILE=str(output / '.env'),
                      WATCHER_SEED_ON_START='0', WATCHER_BROWSER='1')
    from dotenv import dotenv_values
    source = args.app_data.resolve()
    connection = sqlite3.connect((source / 'school_watcher.db').as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    configured = connection.execute('SELECT * FROM ai_profiles WHERE provider=? AND model=? ORDER BY id DESC',
                                   ('deepseek', 'deepseek-flash')).fetchone()
    user_limits = dict(connection.execute("SELECT key,value FROM app_config WHERE key IN ('ai_token_limit_total','ai_token_limit_directory','ai_max_concurrent')"))
    connection.close()
    if not configured:
        raise SystemExit('The requested configured model was not found')
    values = dotenv_values(source / '.env')
    os.environ['FIELD_ENC_KEY'] = values.get('FIELD_ENC_KEY') or (source / '.field-key').read_text(encoding='ascii').strip()
    from backend.core.secrets import decrypt_field
    credential = decrypt_field(configured['encrypted_key'])
    if not credential:
        raise SystemExit('The saved credential is unavailable')
    # Only the broker secret, never the API credential, enters a child environment.
    browser_port, broker_port, broker_token = port(), port(), secrets.token_hex(32)
    os.environ.update(WATCHER_BROWSER_URL=f'http://127.0.0.1:{browser_port}',
                      WATCHER_BROWSER_TOKEN=broker_token)
    from backend import create_app
    from backend.database.db import db
    from backend.database.models import School, Department, Announcement, AnnouncementSource, AppConfig, BackgroundTask
    from backend.ai.models import AIProfile, AIBinding, AIExecution, AIBudget
    from backend.ai import providers, runtime
    from backend.ai.skill_loader import canonical
    from backend.services import tasks, task_fetch
    from backend.scraper.acquisition import coordinator
    from backend.services.discovery_cache import DiscoveryCache
    from backend.services.source_inventory import site_key
    from backend.worker import execute
    app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-live-acceptance',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(output / 'test.db'),
        'SOURCE_CATALOG_PATH': str(output / 'catalog.db'),
        'DISCOVERY_CACHE_PATH': str(output / 'discovery.db'),
        'FETCH_EVIDENCE_DIR': str(output / 'fetch-evidence'),
        'SOURCE_GOVERNANCE_EVIDENCE_PATH': str(output / 'evidence'),
        'BROWSER_SERVICE_URL': os.environ['WATCHER_BROWSER_URL'], 'BROWSER_SERVICE_TOKEN': broker_token})
    site = next(s for s in json.loads(args.plan.read_text(encoding='utf-8'))['sites'] if s['id'] == args.site)
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {
        'site': site, 'implementation': args.label, 'started_at': datetime.utcnow().isoformat() + 'Z',
        'requests': [], 'page_reads': [], 'steps': [], 'cache_hits': 0, 'elapsed_seconds': 0,
        'limits': {'requests': 40, 'tokens': 200000}, 'coverage_verified': False}
    if report['site']['id'] != site['id'] or report['implementation'] != args.label:
        raise SystemExit('The existing report belongs to a different site or implementation')
    report.setdefault('http_requests', [])
    report.setdefault('discovery_cache_hits', 0)
    report['measurement_scope'] = {
        'actual_requests': 'Provider adapter dispatch attempts retained in this ledger; not confirmed bills.',
        'http_requests': 'Official-host transport attempts, with redirects recorded separately. request_evidence distinguishes proven unsent proxy-connection failures, observed responses and unknown delivery. Transport attempts can exceed website request attempts due to proven pre-send failures; do not count all connection attempts as website requests. Bytes exclude headers and use received wire-body bytes where available.',
        'page_reads': 'Logical HTTP page attempts, including admission deferrals that send no HTTP request.',
        'cache_hits': 'Completed task_fetch evidence lookups, including negative observations supported by the measured version (the historical counter is retained).',
        'discovery_cache_hits': 'Successful DiscoveryCache.snapshot reads in the instrumented windows, including evidence reuse; not necessarily avoided network requests. Browser caches are not measured.',
        'browser_reads': 'Browser navigation operations in browser_reads.jsonl, not browser subresource HTTP requests.'}
    measurement_window = {'started_at': datetime.utcnow().isoformat() + 'Z',
                          'repo': str(repo),
                          'snapshot_sha256': json.loads((repo / 'ACCEPTANCE_SNAPSHOT.json').read_text())['sha256'] if (repo / 'ACCEPTANCE_SNAPSHOT.json').exists() else None,
                          'first_http_index': len(report['http_requests']),
                          'first_task_cache_hits': report['cache_hits'],
                          'first_discovery_cache_hits': report['discovery_cache_hits']}
    report.setdefault('measurement_windows', []).append(measurement_window)
    report_lock = threading.RLock()
    def persist():
        with report_lock:
            write_json(report_path, report)
    persist()
    def token_totals():
        actual = sum(r.get('usage', {}).get('total_tokens', 0) for r in report['requests'])
        unknown = sum(r['reserved_tokens'] for r in report['requests'] if not r.get('usage', {}).get('known'))
        return actual, unknown
    original_complete, original_http, original_cache = providers.complete, coordinator.http_fetch, task_fetch.cache_lookup
    original_snapshot = DiscoveryCache.snapshot
    def paid_complete(profile, key, messages, **kwargs):
        reservation = len(canonical(messages).encode('utf-8')) + int(kwargs.get('max_tokens') or profile['max_output_tokens'])
        actual, unknown = token_totals()
        if len(report['requests']) >= 40 or actual + unknown + reservation > 200000:
            report['budget_stopped'] = True
            persist()
            raise providers.ProviderError('budget_exhausted', usage={'known': True, 'total_tokens': 0})
        entry = {'started_at': datetime.utcnow().isoformat() + 'Z', 'reserved_tokens': reservation,
                 'input_digest': hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()}
        report['requests'].append(entry)
        persist()  # An interrupted paid request is retained and never silently forgotten.
        started = time.monotonic()
        try:
            result = original_complete(profile, key, messages, **kwargs)
            entry.update(usage=result.usage, finish_reason=result.finish_reason, request_id=result.request_id)
            return result
        except providers.ProviderError as exc:
            entry.update(usage=exc.usage if exc.uncertain else {'known': True, 'total_tokens': 0},
                         error_code=exc.code, uncertain=exc.uncertain)
            raise
        finally:
            entry['elapsed_seconds'] = round(time.monotonic() - started, 3)
            persist()
    def measured_http(request):
        started = time.monotonic()
        entry = {'id': secrets.token_hex(8), 'url': request.url, 'purpose': request.purpose, 'transport': 'http'}
        context = _logical_page.set(entry['id'])
        try:
            result = original_http(request)
            entry.update(final_url=result.final_url, outcome=result.outcome, error_code=result.error_code,
                         status=result.status, bytes=len(result.html.encode('utf-8')),
                         html_sha256=hashlib.sha256(result.html.encode('utf-8')).hexdigest())
            return result
        finally:
            _logical_page.reset(context)
            entry['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['page_reads'].append(entry)
            persist()
    def measured_cache(request):
        result = original_cache(request)
        if result is not None:
            report['cache_hits'] += 1
        return result
    def measured_snapshot(cache, *args, **kwargs):
        result = original_snapshot(cache, *args, **kwargs)
        if result is not None:
            with report_lock:
                report['discovery_cache_hits'] += 1
        return result
    providers.complete, coordinator.http_fetch, task_fetch.cache_lookup = paid_complete, measured_http, measured_cache
    runtime.credential_for = lambda binding, **kwargs: credential
    from werkzeug.serving import make_server
    broker = make_server('127.0.0.1', broker_port, app, threaded=True)
    threading.Thread(target=broker.serve_forever, daemon=True).start()
    child_env = dict(os.environ, WATCHER_BROWSER_HOST='127.0.0.1', WATCHER_BROWSER_PORT=str(browser_port),
        WATCHER_BROWSER_CHANNEL='msedge', WATCHER_ORIGIN_BROKER_URL=f'http://127.0.0.1:{broker_port}/internal/browser-origin')
    child_env.pop('FIELD_ENC_KEY', None)
    with ExitStack() as stack:
        HTTPMeasurements(report['http_requests'], persist, report_lock, site['url']).install(stack)
        stack.enter_context(patch.object(DiscoveryCache, 'snapshot', measured_snapshot))
        log = stack.enter_context((output / 'browser.log').open('a', encoding='utf-8'))
        browser = subprocess.Popen([sys.executable, str(ROOT / 'scripts/checks/acceptance_browser.py'),
            str(repo), str(output / 'browser_reads.jsonl')],
            cwd=repo, env=child_env, stdout=log, stderr=log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started = time.monotonic()
        try:
            with app.app_context():
                db.create_all()
                profile = AIProfile.query.first()
                if not profile:
                    profile = AIProfile(name='Isolated configured DeepSeek', provider=configured['provider'],
                        model=configured['model'], region=configured['region'], encrypted_key='',
                        version=1, tested_version=1, enabled=True, last_test_code='ok',
                        max_output_tokens=configured['max_output_tokens'])
                    db.session.add(profile); db.session.flush()
                    db.session.add(AIBinding(purpose='directory', profile_id=profile.id))
                    for key, value in user_limits.items():
                        AppConfig.set(key, value)
                    AppConfig.set('ai_token_limit_school_discovery', '200000')
                    AppConfig.set('ai_request_limit_school_discovery', '40')
                    db.session.commit()
                school = School.query.first()
                if not school:
                    school = School(name=site['name'], url=site['url'])
                    db.session.add(school); db.session.commit()
                    tasks.enqueue('discover', school.id, {'school_id': school.id, 'ai_assist': True})
                school_id = school.id
                if getattr(args, 'resume_discovery', False):
                    # Mirrors an explicit source check, retaining the same
                    # school's persisted round and all paid usage.
                    tasks.enqueue('discover', school.id, {'school_id': school.id, 'refresh': True,
                        'ai_assist': True, 'trigger': 'manual_changes'}, expedite=True)
            while time.monotonic() - started < args.seconds:
                with app.app_context():
                    handle = tasks.claim()
                    db.session.remove()
                if handle is None:
                    with app.app_context():
                        queued = BackgroundTask.query.filter(BackgroundTask.state.in_(('pending', 'running'))).all()
                        soon = pending_within_window(queued, args.seconds - (time.monotonic() - started))
                    if not soon:
                        break
                    time.sleep(0.5)
                    continue
                execute(app, handle)
                with app.app_context():
                    job = db.session.get(BackgroundTask, handle['id'])
                    step = {'kind': job.kind, 'state': job.state, 'phase': job.phase, 'error_code': job.error_code,
                            'result': job.result, 'task_id': job.id, 'generation': job.generation}
                    report['steps'].append(step)
                    if job.phase == 'ai_budget':
                        report['budget_stopped'] = True
                    print(json.dumps({key: value for key, value in step.items() if key != 'result'}, ensure_ascii=False), flush=True)
                persist()
            with app.app_context():
                report.update(columns=[d.to_dict() | {'kind': d.kind} for d in Department.query.all()],
                    notices=[{'id': n.id, 'department_id': n.department_id, 'title': n.title, 'url': n.url,
                              'has_body': bool(n.content_text), 'content_error': n.content_error} for n in Announcement.query.all()],
                    source_links=[{'announcement_id': x.announcement_id, 'department_id': x.department_id} for x in AnnouncementSource.query.all()],
                    executions=[{'id': x.execution_id, 'status': x.status, 'usage': x.usage, 'error_code': x.error_code,
                                 'mode': x.mode} for x in AIExecution.query.all()],
                    budgets=[{'key': x.key, 'used_tokens': x.used_tokens, 'reserved_tokens': x.reserved_tokens} for x in AIBudget.query.all()],
                    tasks=[{'kind': x.kind, 'state': x.state, 'phase': x.phase, 'error_code': x.error_code,
                            'available_at': x.available_at, 'payload': x.payload} for x in BackgroundTask.query.all()])
                inventory = DiscoveryCache(app.config['DISCOVERY_CACHE_PATH'])
                report['inventory'] = inventory.report(site_key(site['url']))
                db.session.remove(); db.engine.dispose()
        finally:
            report['elapsed_seconds'] += round(time.monotonic() - started, 3)
            report['actual_tokens'], report['unknown_reserved_tokens'] = token_totals()
            report['actual_requests'] = len(report['requests'])
            report['ended_at'] = datetime.utcnow().isoformat() + 'Z'
            measurement_window.update(ended_at=report['ended_at'], last_http_index=len(report['http_requests']),
                last_task_cache_hits=report['cache_hits'], last_discovery_cache_hits=report['discovery_cache_hits'])
            report['http_request_attempts'] = len(report['http_requests'])
            report['http_definitely_unsent_proxy_failures'] = sum(
                row.get('request_evidence') == 'proxy_connection_failed_before_send' for row in report['http_requests'])
            persist()
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill(); browser.wait(timeout=10)
            broker.shutdown()
    print(json.dumps({key: report.get(key) for key in ('actual_requests','actual_tokens','unknown_reserved_tokens','elapsed_seconds','budget_stopped')}, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--app-data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--seconds', type=int, default=900)
    parser.add_argument('--resume-discovery', action='store_true', help='Queue an explicit check in the existing ledger; never reset the school round')
    parser.add_argument('--plan', type=Path, default=ROOT / 'tests/acceptance/source_agent_sites.json')
    run(parser.parse_args())
