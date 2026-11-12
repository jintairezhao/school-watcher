"""Bridge pure acquisition to the one durable task queue and bounded replay files."""
from dataclasses import replace
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from flask import current_app

from backend.scraper.acquisition import FetchRequest, FetchResult
from backend.scraper.acquisition.browser_client import BrowserClient
from backend.services import tasks
from backend.services.runtime_leases import reserve_origin, release_origin, renew_origin

_collection_observation = ContextVar('collection_fetch_observation', default=None)


@contextmanager
def collection_observation_context(department, log):
    """Freeze the actual collecting configuration before reading any resource."""
    with _source_observation_context(department, 'collect', log):
        yield


@contextmanager
def source_health_observation_context(department):
    """Attribute a due health read without inventing a collection run/log."""
    with _source_observation_context(department, 'source_health'):
        yield


@contextmanager
def _source_observation_context(department, task_kind, log=None):
    from backend.services.source_governance import source_config, _hash
    handle = tasks.current_execution() or {}
    value = None
    if handle.get('kind') == task_kind and (task_kind == 'source_health' or log is not None and log.id):
        config = source_config(department)
        value = dict(schema_version=1, log_id=log.id if log is not None else None, task_id=handle['id'],
            task_generation=handle['generation'], school_id=department.school_id,
            source_id=department.id, config_hash=_hash(config), config=config)
        if task_kind != 'collect':
            value['task_kind'] = task_kind
    token = _collection_observation.set(value)
    try:
        yield
    finally:
        _collection_observation.reset(token)


def _collection_identity():
    value = _collection_observation.get()
    return {key: value[key] for key in ('school_id', 'source_id', 'config_hash')} if value else None


def _cache_key(request):
    identity = _collection_identity()
    if identity is None:
        return request_key(request)
    return sha256(json.dumps([request_key(request), identity], sort_keys=True).encode()).hexdigest()


def collection_observation_path(log):
    """The existing log/task identity locates optional, retention-bound metadata."""
    identifiers = (log.task_id, log.task_generation, log.id)
    if not all(type(value) is int and value > 0 for value in identifiers):
        return None
    return evidence_root() / f'{log.task_id}-{log.task_generation}-observations-{log.id}.json'


def read_collection_observations(log):
    path = collection_observation_path(log)
    if path is None:
        return None
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        context = data['collection']
        from backend.services.source_governance import _hash
        if not isinstance(data, dict) or not isinstance(context, dict) or not isinstance(data.get('observations'), list):
            return None
        if (data.get('schema_version') != 1 or context.get('school_id') != log.school_id
                or context.get('log_id') != log.id or context.get('task_id') != log.task_id
                or context.get('task_generation') != log.task_generation
                or context.get('config_hash') != _hash(context.get('config'))):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def record_collection_observation(request, result, *, replay_ref=None):
    """Keep response metadata beside existing replay files, without copying DOM."""
    context = _collection_observation.get()
    handle = tasks.current_execution()
    if not context or not handle or (not result.observed_at and not replay_ref):
        return
    if request.source_id and str(request.source_id) != str(context['source_id']):
        return  # A read explicitly attributed to another source is not ours.
    tasks.assert_owned(handle)
    from backend.services.collection_access import save_entry_observation
    save_entry_observation(context, request, result, replay_ref=replay_ref)
    if context['log_id'] is None:
        return  # Health evidence belongs to the existing entry row, not a fake ScrapeLog.
    from types import SimpleNamespace
    path = collection_observation_path(SimpleNamespace(id=context['log_id'],
        task_id=context['task_id'], task_generation=context['task_generation']))
    root = evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    observation = dict(request_url=request.url, final_url=result.final_url,
        purpose=request.purpose, embedded_pdf=bool(request.policy.get('embedded_pdf_child')),
        is_configured_entry=request.url == context['config']['list_url'],
        request_source_id=request.source_id, observed_at=result.observed_at,
        transport=result.transport, status=result.status, outcome=result.outcome,
        error_code=result.error_code,
        html_sha256=sha256(result.html.encode('utf-8')).hexdigest() if result.html else None)
    if replay_ref:
        observation['cache_file'] = replay_ref
    from filelock import FileLock
    with FileLock(str(root) + '.lock', timeout=30):
        tasks.assert_owned(handle)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(data, dict) or data.get('collection') != context
                        or not isinstance(data.get('observations'), list)):
                    return
            except (OSError, ValueError, TypeError):
                return
        else:
            data = dict(schema_version=1, collection=context, observations=[], reused_materials=[], omitted_records=0)
        field = 'reused_materials' if replay_ref else 'observations'
        entries = data.setdefault(field, [])
        if not isinstance(entries, list):
            return
        if observation in entries:
            return
        entries.append(observation)
        if len(entries) > 500:
            data['omitted_records'] = data.get('omitted_records', 0) + len(entries) - 500
            data[field] = entries[-500:]
        payload = json.dumps(data, ensure_ascii=False)
        if len(payload.encode('utf-8')) > 2 * 1024 * 1024:
            return
        prune_fetch_evidence(_locked=True)
        from backend.services.storage_policy import capacity_bytes
        capacity = capacity_bytes('fetch_cache_mb', 'FETCH_EVIDENCE_BYTES', 100 * 1048576)
        occupied = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p.name != path.name)
        if capacity and occupied + len(payload.encode('utf-8')) > capacity:
            return
        temporary = root / ('.' + handle['token'] + '.tmp')
        temporary.write_text(payload, encoding='utf-8')
        temporary.replace(path)


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
    if (pending.get('original_key') == request_key(request)
            and pending.get('collection_context') == _collection_identity()):
        from backend.scraper.acquisition import classify_result
        raw = browser_dispatch(FetchRequest.from_dict(pending['request']))
        result = raw if raw.outcome in ('busy', 'unavailable', 'network_error', 'needs_manual') else classify_result(request, raw)
        from backend.scraper.acquisition.coordinator import complete_observation
        result = complete_observation(request, result)
        if result.replayable:
            cache_store(request, result)
        return result
    entry = (handle.get('checkpoint', {}).get('completed_fetches') or {}).get(_cache_key(request))
    if not entry or not isinstance(entry, str) or Path(entry).name != entry:
        return None
    path = evidence_root() / entry
    try:
        if path.stat().st_size > 12 * 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
        if _collection_identity() is not None and payload.get('collection_context') != _collection_identity():
            return None
        result = FetchResult.from_dict(payload)
        if not result.replayable:
            return None
    except (OSError, ValueError, TypeError):
        return None
    # A metadata-write error must not masquerade as a cache miss and spend an
    # extra website request. Surface it through the normal task error handling.
    record_collection_observation(request, result, replay_ref=entry)
    return result


def cache_store(request, result):
    handle = tasks.current_execution()
    if not handle or not result.replayable:
        return
    key = _cache_key(request)
    root = evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{handle['id']}-{handle['generation']}-{key}.json"
    # The root lock serializes writes, so one short token-scoped name is
    # sufficient. Repeating the request digest can exceed Windows' path limit
    # even when the final cache path itself is valid.
    temporary = root / ('.' + handle['token'] + '.tmp')
    value = result.to_dict()
    if _collection_identity() is not None:
        value['collection_context'] = _collection_identity()
    payload = json.dumps(value, ensure_ascii=False)
    if len(payload.encode('utf-8')) > 12 * 1024 * 1024:
        return
    from filelock import FileLock
    with FileLock(str(root) + '.lock', timeout=30):
        prune_fetch_evidence(_locked=True)
        from backend.services.storage_policy import capacity_bytes
        capacity = capacity_bytes('fetch_cache_mb', 'FETCH_EVIDENCE_BYTES', 100 * 1048576)
        occupied = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p.name != name)
        if capacity and occupied + len(payload.encode('utf-8')) > capacity:
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
    collection_identity = _collection_identity()
    suffix = ':' + collection_identity['config_hash'] if collection_identity else ''
    execution_id = sha256(f"{handle['id']}:{handle['generation']}:{handle.get('attempts', 0)}:{key}{suffix}".encode()).hexdigest()
    request = replace(request, request_id=execution_id)
    data = dict(handle.get('checkpoint') or {})
    pending = data.get('browser') or {}
    if (pending.get('key') != key or pending.get('request_id') != execution_id
            or pending.get('collection_context') != collection_identity):
        pending = {'key': key, 'request_id': execution_id, 'request': request.to_dict(), 'submitted': False,
                   'original_key': request_key(handle.get('_fetch_request') or request),
                   'collection_context': collection_identity}
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
                                      ttl=request.timeout_seconds + 40, transport='browser')
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
        if request.purpose == 'article' and request.policy.get('exploration'):
            # Sampling continues to the next actual notification when this one
            # requires an account or human verification. It is not proof that
            # the entire column is inaccessible and must not block other samples.
            return result
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
    from backend.services.storage_policy import capacity_bytes, retention_cutoff
    max_bytes = capacity_bytes('fetch_cache_mb', 'FETCH_EVIDENCE_BYTES', 100 * 1048576)
    files = sorted(((p, p.stat()) for p in root.iterdir() if p.is_file() and not p.is_symlink()), key=lambda row: row[1].st_mtime)
    total, removed = sum(stat.st_size for _, stat in files), 0
    cutoff = retention_cutoff(days, datetime.now(timezone.utc)).timestamp()
    for path, stat in files:
        parts = path.name.split('-', 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and (int(parts[0]), int(parts[1])) in active:
            continue
        if all_cache or (days and stat.st_mtime < cutoff) or (max_bytes and total > max_bytes):
            path.unlink(missing_ok=True)
            total -= stat.st_size
            removed += 1
    return {'bytes': total, 'removed': removed}
