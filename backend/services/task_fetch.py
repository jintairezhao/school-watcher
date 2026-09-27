"""Bridge pure acquisition to the one durable task queue and bounded replay files."""
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from flask import current_app

from backend.scraper.acquisition import FetchRequest, FetchResult
from backend.scraper.acquisition.browser_client import BrowserClient
from backend.services import tasks
from backend.services.runtime_leases import reserve_origin, release_origin, renew_origin


def evidence_root():
    root = current_app.config.get('FETCH_EVIDENCE_DIR')
    return Path(root) if root else Path(current_app.config['SOURCE_CATALOG_PATH']).parent / 'fetch-evidence'


def request_key(request):
    value = request.to_dict()
    for field in ('request_id', 'session_id', 'timeout_seconds'):
        value.pop(field, None)
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_lookup(request):
    handle = tasks.current_execution()
    if not handle:
        return None
    handle['_fetch_request'] = request
    pending = handle.get('checkpoint', {}).get('browser') or {}
    if pending.get('original_key') == request_key(request):
        from backend.scraper.acquisition import classify_result
        raw = browser_dispatch(FetchRequest.from_dict(pending['request']))
        result = raw if raw.outcome in ('busy', 'unavailable', 'network_error', 'needs_manual') else classify_result(request, raw)
        if result.ok:
            cache_store(request, result)
        return result
    entry = (handle.get('checkpoint', {}).get('completed_fetches') or {}).get(request_key(request))
    if not entry or not isinstance(entry, str) or Path(entry).name != entry:
        return None
    path = evidence_root() / entry
    try:
        if path.stat().st_size > 12 * 1024 * 1024:
            return None
        result = FetchResult.from_dict(json.loads(path.read_text(encoding='utf-8')))
        return result if result.ok else None
    except (OSError, ValueError, TypeError):
        return None


def cache_store(request, result):
    handle = tasks.current_execution()
    if not handle or not result.ok:
        return
    key = request_key(request)
    root = evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{handle['id']}-{handle['generation']}-{key}.json"
    temporary = root / (name + '.' + handle['token'] + '.tmp')
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    if len(payload.encode('utf-8')) > 12 * 1024 * 1024:
        return
    from filelock import FileLock
    with FileLock(str(root) + '.lock', timeout=30):
        prune_fetch_evidence(_locked=True)
        capacity = current_app.config.get('FETCH_EVIDENCE_BYTES', 100 * 1024 * 1024)
        occupied = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p.name != name)
        if occupied + len(payload.encode('utf-8')) > capacity:
            # Checkpoints are an optimization; never evict an active execution or
            # exceed the disk budget. Re-reading remains idempotent on recovery.
            return
        temporary.write_text(payload, encoding='utf-8')
        temporary.replace(root / name)
    data = dict(handle.get('checkpoint') or {})
    completed = dict(data.get('completed_fetches') or {})
    completed[key] = name
    # Directory walks already have bounded crawl budgets; keep JSON pointers small.
    data['completed_fetches'] = dict(list(completed.items())[-500:])
    data.pop('browser', None)
    tasks.checkpoint(data)


def browser_dispatch(request):
    handle = tasks.current_execution()
    tasks.assert_owned(handle)
    source_id = request.source_id or str(handle.get('payload', {}).get('department_id') or handle['identity'])
    request = replace(request, source_id=source_id)
    key = request_key(request)
    execution_id = sha256(f"{handle['id']}:{handle['generation']}:{handle.get('attempts', 0)}:{key}".encode()).hexdigest()
    request = replace(request, request_id=execution_id)
    data = dict(handle.get('checkpoint') or {})
    pending = data.get('browser') or {}
    if pending.get('key') != key or pending.get('request_id') != execution_id:
        pending = {'key': key, 'request_id': execution_id, 'request': request.to_dict(), 'submitted': False,
                   'original_key': request_key(handle.get('_fetch_request') or request)}
    data['browser'] = pending
    if handle['capability'] != 'browser':
        tasks.defer(capability='browser', phase='render', checkpoint=data, reason='等待读取动态网页')
    client = BrowserClient(base_url=current_app.config.get('BROWSER_SERVICE_URL'),
                           token=current_app.config.get('BROWSER_SERVICE_TOKEN'))
    if pending.get('submitted'):
        renew_origin(execution_id)
        state, result = client.poll(execution_id)
        if result and result.error_code in ('browser_http_404', 'execution_missing'):
            # Browser process restart loses only transient execution state. Read
            # requests can safely be resubmitted with the same idempotency key.
            pending['submitted'] = False
        else:
            started = datetime.fromisoformat(pending['submitted_at'])
            if datetime.utcnow() > started + timedelta(seconds=request.timeout_seconds + 30):
                release_origin(execution_id)
                return FetchResult(request.url, transport='browser', outcome='network_error',
                    error_code='browser_deadline', message='浏览器读取超过本次时间预算')
    if not pending.get('submitted'):
        permit, wait = reserve_origin(request.url, handle['worker_id'], token=execution_id,
                                      ttl=request.timeout_seconds + 40)
        if not permit:
            tasks.defer(capability='browser', phase='origin_wait', checkpoint=data,
                        delay=min(900, wait), reason='官网访问正在排队', error_code='origin_busy')
        state, result = client.submit(request)
        if state in ('queued', 'running'):
            pending.update(submitted=True, submitted_at=datetime.utcnow().isoformat())
    if state in ('queued', 'running'):
        tasks.defer(capability='browser', phase='render', checkpoint=data, delay=2, reason='正在读取动态网页')
    release_origin(execution_id)
    if state == 'busy' or (result and result.outcome == 'busy'):
        pending['submitted'] = False
        tasks.defer(capability='browser', phase='browser_wait', checkpoint=data,
                    delay=min(60, (result.retry_after if result else None) or 5), reason='浏览器任务正在排队')
    result = result or FetchResult(request.url, transport='browser', outcome='network_error',
                                   error_code='browser_no_result', message='浏览器未返回有效结果')
    if result.outcome not in ('busy', 'unavailable', 'network_error', 'needs_manual'):
        from backend.scraper.acquisition import classify_result
        result = classify_result(request, result)
    if result.outcome == 'needs_manual':
        tasks.checkpoint(data)
        from urllib.parse import urlsplit
        parsed = urlsplit(request.url)
        tasks.require_verification(source_id, request.url, f'{parsed.scheme}://{parsed.netloc}',
                                   result.message, request_payload=request.to_dict())
    return result


def prune_fetch_evidence(*, all_cache=False, _locked=False):
    from filelock import FileLock
    root = evidence_root()
    if not root.exists():
        return {'bytes': 0, 'removed': 0}
    if not _locked:
        with FileLock(str(root) + '.lock', timeout=30):
            return prune_fetch_evidence(all_cache=all_cache, _locked=True)
    from backend.database.db import db
    from backend.database.models import BackgroundTask, AppConfig
    active = {(row.id, row.generation) for row in db.session.execute(db.select(BackgroundTask).where(
        BackgroundTask.state.in_(tasks.ACTIVE_STATES))).scalars()}
    try:
        days = max(0, int(AppConfig.get('discovery_cache_days', '7')))
    except (ValueError, TypeError):
        days = 7
    max_bytes = current_app.config.get('FETCH_EVIDENCE_BYTES', 100 * 1024 * 1024)
    files = sorted(((p, p.stat()) for p in root.iterdir() if p.is_file() and not p.is_symlink()), key=lambda row: row[1].st_mtime)
    total, removed = sum(stat.st_size for _, stat in files), 0
    cutoff = (datetime.utcnow() - timedelta(days=days)).timestamp()
    for path, stat in files:
        parts = path.name.split('-', 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and (int(parts[0]), int(parts[1])) in active:
            continue
        if all_cache or (days and stat.st_mtime < cutoff) or total > max_bytes:
            path.unlink(missing_ok=True)
            total -= stat.st_size
            removed += 1
    return {'bytes': total, 'removed': removed}
