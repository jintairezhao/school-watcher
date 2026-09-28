"""Resolve and restore a reader's refresh scope without widening subscriptions."""
import json
from datetime import datetime, timedelta

from flask import abort
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.database.db import db
from backend.database.models import School, Department, Subscription, BackgroundTask, AppConfig
from backend.services.directory_options import directory_entries_for, expand_directory_ids
from backend.services.tasks import enqueue


def subscribed_sources(school, department_ids=None):
    entries = directory_entries_for(school.id)
    directories = {e.parent_id for e in entries}
    wanted = expand_directory_ids(school.id, department_ids, entries)
    return [d for d in school.departments.order_by(Department.id) if d.list_url
            and d.id not in directories and d.name.upper() != 'DAILY NEWS'
            and (wanted is None or d.id in wanted)]


def resolve_scope(user_id, payload):
    if not isinstance(payload, dict):
        abort(400, description='刷新范围格式不正确')
    scope = payload.get('scope', 'current')
    school_id, ids, group = payload.get('school_id'), payload.get('department_ids', []), payload.get('group', '')
    if (scope not in ('current', 'all') or (school_id is not None and type(school_id) is not int)
            or not isinstance(ids, list) or len(ids) > 1000 or any(type(i) is not int for i in ids)
            or not isinstance(group, str) or len(group) > 200 or (scope == 'current' and (ids or group) and not school_id)):
        abort(400, description='请选择有效的刷新范围')
    subs = Subscription.query.join(School).filter(Subscription.user_id == user_id, School.enabled.is_(True)).all()
    if scope == 'current' and school_id:
        subs = [s for s in subs if s.school_id == school_id]
        if not subs:
            abort(403, description='只能刷新自己订阅的来源')
    result = []
    for sub in subs:
        allowed = subscribed_sources(sub.school, sub.department_ids)
        if scope == 'current' and school_id:
            expanded = expand_directory_ids(school_id, ids)
            all_ids = {d.id for d in sub.school.departments}
            subscribed = set(expand_directory_ids(school_id, sub.department_ids) or []) if sub.department_ids is not None else all_ids
            if any(i not in all_ids or i not in subscribed for i in ids):
                abort(403, description='只能刷新自己订阅的来源')
            if ids:
                allowed = [d for d in allowed if d.id in expanded]
            if group:
                from backend.services.inbox import source_hierarchy
                tree = source_hierarchy(sub.school.departments.all(), directory_entries=directory_entries_for(school_id))
                group_ids = {c['department'].id for u in tree.get(group, []) for c in u['columns']}
                allowed = [d for d in allowed if d.id in group_ids]
        result.extend(allowed)
    return result


def collection_interval_seconds():
    """Use the same persisted interval in web admission and background scheduling."""
    try:
        return max(5, min(720, int(AppConfig.get('scrape_interval', '30')))) * 60
    except (ValueError, TypeError):
        return 1800


def queue_sources(departments, *, manual=False):
    """Reuse active work and admit only sources whose shared cache is due.

    A recent failed attempt counts as a check, otherwise many readers opening
    the site could repeatedly retry an unavailable university. Successful data
    also remains fresh when its historical task has already been removed.
    """
    departments = list({d.id: d for d in departments}.values())
    interval = 60 if manual else collection_interval_seconds()
    cutoff = datetime.utcnow() - timedelta(seconds=interval)
    identities = [f'collect:{d.id}' for d in departments]
    existing = {t.identity: t for t in BackgroundTask.query.filter(
        BackgroundTask.identity.in_(identities))} if identities else {}
    jobs = []
    for dept in departments:
        task = existing.get(f'collect:{dept.id}')
        if not manual:
            if task and task.state in ('pending', 'running', 'waiting'):
                jobs.append(task.id)
                continue
            checked = [stamp for stamp in (dept.last_scraped_at,
                       (task.checked_at or task.updated_at) if task else None) if stamp]
            if checked and max(checked) > cutoff:
                continue
        jobs.append(enqueue('collect', dept.id, {'school_id': dept.school_id, 'department_id': dept.id},
                            min_interval=interval, expedite=manual).id)
    return jobs


def _tracking_key(user_id):
    return f'inbox_refresh:user:{user_id}'


def _read_pointer(user_id):
    raw = db.session.execute(select(AppConfig.value).where(AppConfig.key == _tracking_key(user_id))).scalar_one_or_none()
    try:
        pointer = json.loads(raw) if raw else {}
        if (not isinstance(pointer, dict) or pointer.get('scope') not in ('current', 'all') or
                not isinstance(pointer.get('department_ids'), list) or
                any(type(i) is not int for i in pointer['department_ids'])):
            pointer = {}
    except (ValueError, TypeError):
        pointer = {}
    return raw, pointer


def _tracked_status(pointer, status):
    allowed = {s['id']: s for s in status['sources']}
    selected = set(pointer.get('department_ids', [])) & allowed.keys()
    # The previous response or scope-pointer write might have been interrupted.
    # Active shared tasks are recoverable without posting another collection job.
    selected.update(s['id'] for s in status['sources'] if s['state'] in ('pending', 'running'))
    result = status_totals([s for s in status['sources'] if s['id'] in selected])
    result['tracked'] = bool(selected)
    result['tracking'] = {'scope': pointer.get('scope', 'current'),
                          'scope_label': '全部订阅' if pointer.get('scope') == 'all' else '本次更新',
                          'started_at': pointer.get('started_at')}
    return result


def restore_refresh_status(user_id):
    """Read durable tasks, intersecting any saved pointer with today's permissions."""
    _, pointer = _read_pointer(user_id)
    return _tracked_status(pointer, refresh_status(resolve_scope(user_id, {'scope': 'all'})))


def refresh_tracking_baseline(user_id):
    raw, pointer = _read_pointer(user_id)
    ids = set(pointer.get('department_ids', []))
    status = refresh_status(resolve_scope(user_id, {'scope': 'all'}))
    active = any(s['id'] in ids and s['state'] in ('pending', 'running') for s in status['sources'])
    return raw, active


def remember_refresh_scope(user_id, departments, scope, *, baseline):
    """Keep one small pointer per user, merging concurrent tabs with bounded CAS.

    Task results are deliberately absent: every progress read comes from durable
    collection tasks. Compare-and-swap retries prevent one tab replacing another
    tab's still-active source selection. This adds no queue or collection work.
    """
    wanted = {d.id for d in departments}
    for _ in range(6):
        raw, previous = _read_pointer(user_id)
        status = refresh_status(resolve_scope(user_id, {'scope': 'all'}))
        allowed = {s['id'] for s in status['sources']}
        previous_ids = set(previous.get('department_ids', [])) & allowed
        continuing = (baseline[1] if raw == baseline[0] else any(s['id'] in previous_ids and
                      s['state'] in ('pending', 'running') for s in status['sources']))
        ids = wanted & allowed
        if continuing:
            ids.update(previous_ids)
        ids.update(s['id'] for s in status['sources'] if s['state'] in ('pending', 'running'))
        merged_scope = 'all' if scope == 'all' or (continuing and previous.get('scope') == 'all') else 'current'
        value = json.dumps({'scope': merged_scope, 'department_ids': sorted(ids),
                            'started_at': previous.get('started_at') if continuing and previous.get('started_at')
                                          else datetime.utcnow().isoformat()}, separators=(',', ':'))
        if raw is None:
            try:
                with db.session.begin_nested():
                    db.session.add(AppConfig(key=_tracking_key(user_id), value=value))
                    db.session.flush()
                db.session.commit()
                return
            except IntegrityError:
                db.session.rollback()
                continue
        changed = db.session.execute(update(AppConfig).where(
            AppConfig.key == _tracking_key(user_id), AppConfig.value == raw).values(value=value))
        db.session.commit()
        if changed.rowcount:
            return
    abort(503, description='更新已提交，正在恢复进度，请稍后重试。')


def source_status_label(state, message, error_code=''):
    if state == 'waiting' or error_code == 'needs_manual':
        return '需要验证'
    if state == 'failed':
        if error_code in ('access_denied', 'http_forbidden', 'challenge_denied'):
            return '访问受限'
        if error_code in ('needs_adapter', 'content_not_found', 'invalid_api_profile'):
            return '待适配'
        if any(word in message for word in ('访问校验', '拒绝自动访问', 'HTTP 401', 'HTTP 403')):
            return '访问受限'
        if 'HTTP 429' in message:
            return '请求受限'
        if any(word in message for word in ('未识别', '解析规则')):
            return '待适配'
        if 'HTTP 412' in message:
            return '访问异常'
        return '暂不可读'
    return {'unloaded': '待采集', 'pending': '等待更新', 'running': '更新中',
            'unavailable': '官网未提供链接'}.get(state, '')


def refresh_status(departments):
    identities = [f'collect:{d.id}' for d in departments]
    jobs = {t.identity: t for t in BackgroundTask.query.filter(BackgroundTask.identity.in_(identities))} if identities else {}
    sources = []
    for dept in departments:
        task = jobs.get(f'collect:{dept.id}')
        state = (task.state if task else 'saved' if dept.last_scraped_at else 'unloaded') if dept.list_url else 'unavailable'
        result = task.result or {} if task else {}
        message = task.error or result.get('message', '') if task else ''
        progress = (task.checkpoint or {}).get('collection_progress', {}) if task else {}
        latest = next(reversed(progress.values())) if progress else {}
        if task and state in ('pending', 'running') and progress:
            if latest.get('page', 1) > 1 and not latest.get('finished'):
                message = f"已检查 {latest['page'] - 1} 页，继续抓取历史通知"
        sources.append({'id': dept.id, 'name': dept.name, 'state': state,
                        'status_label': source_status_label(state, message, task.error_code if task else ''),
                        'error_code': task.error_code if task else '',
                        'new_count': result.get('new_count', latest.get('new', 0)),
                        'pages_checked': max(0, latest.get('page', 1) - (0 if latest.get('finished') else 1)),
                        'message': message,
                        'updated_at': (task.checked_at or task.updated_at).isoformat() if task else None,
                        'last_synced_at': dept.last_scraped_at.isoformat() if dept.last_scraped_at else None})
    return status_totals(sources)


def status_totals(sources):
    active = sum(s['state'] in ('pending','running') for s in sources)
    failed = sum(s['state'] == 'failed' for s in sources)
    done = sum(s['state'] in ('done','saved') for s in sources)
    return {'sources': sources, 'total': len(sources), 'active': active, 'failed': failed, 'done': done,
            'new_count': sum(s['new_count'] for s in sources)}
