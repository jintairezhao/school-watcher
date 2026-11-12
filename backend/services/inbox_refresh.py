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
    from backend.services.source_lifecycle import active_source_clause
    entries = directory_entries_for(school.id)
    directories = {e.parent_id for e in entries}
    wanted = expand_directory_ids(school.id, department_ids, entries)
    return [d for d in school.departments.filter(active_source_clause()).order_by(Department.id) if d.list_url
            and d.kind not in ('unit', 'group')
            and (d.id not in directories or d.list_selector) and d.name.upper() != 'DAILY NEWS'
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
            from backend.services.inbox import source_filter_ids,source_group_ids
            expanded = source_filter_ids(school_id, ids)
            all_ids = {d.id for d in sub.school.departments}
            subscribed = set(expand_directory_ids(school_id, sub.department_ids) or []) if sub.department_ids is not None else all_ids
            if any(i not in all_ids or i not in subscribed for i in ids):
                abort(403, description='只能刷新自己订阅的来源')
            if ids:
                allowed = [d for d in allowed if d.id in expanded]
            if group:
                group_ids = source_group_ids(school_id, group)
                allowed = [d for d in allowed if d.id in group_ids]
        result.extend(allowed)
    return result


def collection_interval_seconds():
    """Use the same persisted interval in web admission and background scheduling."""
    from backend.services.discovery_policy import get_policy
    default = get_policy()['collection_interval_seconds']
    try:
        configured = AppConfig.get('scrape_interval', '')
        return max(5, min(720, int(configured))) * 60 if configured else default
    except (ValueError, TypeError):
        return default


def next_collection_interval_seconds():
    from random import uniform
    from backend.services.discovery_policy import get_policy
    jitter = get_policy()['collection_jitter']
    return collection_interval_seconds() * uniform(1 - jitter, 1 + jitter)


def queue_sources(departments, *, manual=False, decisions=None):
    """Reuse active work and admit only sources whose shared cache is due.

    A recent failed attempt counts as a check, otherwise many readers opening
    the site could repeatedly retry an unavailable university. Successful data
    also remains fresh when its historical task has already been removed.
    """
    departments = list({d.id: d for d in departments}.values())
    from backend.services.source_lifecycle import states_for
    lifecycle = states_for([d.id for d in departments])
    departments = [d for d in departments if lifecycle[d.id] == 'active']
    from backend.services.student_information import collection_policy
    value_policy = collection_policy(departments, manual=manual)
    departments.sort(key=lambda d: (value_policy[d.id][0], d.id))
    interval = 60 if manual else collection_interval_seconds()
    cutoff = datetime.utcnow() - timedelta(seconds=interval)
    identities = [f'collect:{d.id}' for d in departments]
    existing = {t.identity: t for t in BackgroundTask.query.filter(
        BackgroundTask.identity.in_(identities))} if identities else {}
    discovery_preferences = {t.payload.get('school_id'): t.payload.get('ai_assist', True)
        for t in BackgroundTask.query.filter(BackgroundTask.kind == 'discover',
            BackgroundTask.payload['school_id'].as_integer().in_({d.school_id for d in departments}))}
    jobs = []
    def record(dept, reason, *, checked_at=None, next_check_at=None):
        if decisions is not None:
            decisions.append({'id': dept.id, 'reason': reason,
                'checked_at': checked_at.isoformat() if checked_at else None,
                'next_check_at': next_check_at.isoformat() if next_check_at else None})

    for dept in departments:
        source_interval = min(interval * value_policy[dept.id][1], 86400)
        cutoff = datetime.utcnow() - timedelta(seconds=source_interval)
        task = existing.get(f'collect:{dept.id}')
        if not manual:
            if task and task.state in ('pending', 'running', 'waiting'):
                jobs.append(task.id)
                record(dept, 'active')
                continue
            if task and task.next_run_at:
                if task.state == 'failed':
                    if task.next_run_at > datetime.utcnow():
                        record(dept, 'retry_delay', checked_at=task.checked_at,
                               next_check_at=task.next_run_at)
                        continue
                    source_interval = max(0, (task.next_run_at - task.checked_at).total_seconds()) if task.checked_at else interval
                    cutoff = datetime.utcnow() - timedelta(seconds=source_interval)
                # A newly changed user interval overrides an older schedule.
                # Persisted jitter is reused, never rerolled on every UI visit.
                elif (task.result or {}).get('_collection_interval_seconds') == interval and task.checked_at:
                    source_interval = min(86400, max(0, (task.next_run_at - task.checked_at).total_seconds()) * value_policy[dept.id][1])
                    cutoff = datetime.utcnow() - timedelta(seconds=source_interval)
            checked = [stamp for stamp in (dept.last_scraped_at,
                       (task.finished_at or task.checked_at or task.updated_at) if task else None) if stamp]
            if checked and max(checked) > cutoff:
                successful = (collection_display_state(task, task.result or {}) == 'done'
                              if task else bool(dept.last_scraped_at))
                record(dept, 'recent_success' if successful else 'recent_attempt',
                       checked_at=max(checked), next_check_at=max(checked) + timedelta(seconds=source_interval))
                continue
        jobs.append(enqueue('collect', dept.id, {'school_id': dept.school_id, 'department_id': dept.id,
                            'ai_assist': discovery_preferences.get(dept.school_id, True)},
                            min_interval=source_interval, expedite=manual).id)
        record(dept, 'included')
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
    admission = pointer.get('admission') or {}
    recorded = isinstance(admission, dict) and isinstance(admission.get('requested_ids'), list)
    # Old pointers never saved omitted IDs or reasons. Today's subscription is
    # useful context, but must not be presented as a historical admission trace.
    requested = ({i for i in admission['requested_ids'] if type(i) is int} if recorded else
                 set(allowed) if pointer.get('scope') == 'all' else set(selected))
    requested = (requested | selected) & allowed.keys()
    reasons = {r['id']: r for r in admission.get('decisions', [])
               if isinstance(r, dict) and type(r.get('id')) is int} if recorded else {}
    outside = []
    for source in status['sources']:
        if source['id'] not in requested - selected:
            continue
        decision = reasons.get(source['id'], {})
        reason = decision.get('reason')
        if reason not in ('recent_success', 'recent_attempt', 'retry_delay'):
            reason = 'unrecorded'
        outside.append(dict(source, admission_reason=reason,
                            admission_checked_at=decision.get('checked_at'),
                            admission_next_check_at=decision.get('next_check_at')))
    members = [s for s in status['sources'] if s['id'] in requested]
    schools = {}
    for source in members:
        school = schools.setdefault(source['school_id'], {'id': source['school_id'],
            'name': source['school_name'], 'total': 0, 'configured': 0,
            'batch_total': 0, 'batch_configured': 0})
        school['total'] += 1
        school['configured'] += int(source['configured'])
        school['batch_total'] += int(source['id'] in selected)
        school['batch_configured'] += int(source['configured'] and source['id'] in selected)
    result['scope_summary'] = {'total': len(members),
        'configured': sum(s['configured'] for s in members), 'batch_total': len(selected),
        'outside_batch_total': len(outside), 'recorded_at_admission': recorded,
        'checked_at': admission.get('checked_at') if recorded else None,
        'schools': list(schools.values())}
    result['outside_batch'] = outside
    result['tracked'] = bool(selected or (recorded and members))
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


def remember_refresh_scope(user_id, departments, scope, *, baseline, requested=None, decisions=None):
    """Keep one small pointer per user, merging concurrent tabs with bounded CAS.

    Task results are deliberately absent: every progress read comes from durable
    collection tasks. Compare-and-swap retries prevent one tab replacing another
    tab's still-active source selection. This adds no queue or collection work.
    """
    wanted = {d.id for d in departments}
    requested_ids = {d.id for d in requested} if requested is not None else wanted
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
        previous_admission = previous.get('admission') or {}
        old_decisions = previous_admission.get('decisions', []) if isinstance(previous_admission, dict) else []
        reason_by_id = {r['id']: r for r in old_decisions
                       if continuing and isinstance(r, dict) and type(r.get('id')) is int
                       and r['id'] in allowed and r['id'] not in requested_ids}
        reason_by_id.update({r['id']: r for r in (decisions or []) if r['id'] in allowed})
        scope_ids = requested_ids & allowed
        if continuing and isinstance(previous_admission, dict):
            scope_ids.update(i for i in previous_admission.get('requested_ids', [])
                             if type(i) is int and i in allowed)
        scope_ids.update(ids)
        value = json.dumps({'scope': merged_scope, 'department_ids': sorted(ids),
                            'started_at': previous.get('started_at') if continuing and previous.get('started_at')
                                          else datetime.utcnow().isoformat(),
                            'admission': {'requested_ids': sorted(scope_ids),
                                'checked_at': datetime.utcnow().isoformat(),
                                'decisions': list(reason_by_id.values())}}, separators=(',', ':'))
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


# Codes the acquisition classifier actually emits. Keep the legacy spellings
# too: rows written before the classifier existed still carry them.
MANUAL_CODES = ('needs_manual', 'human_verification', 'access_challenge')
LIMITED_CODES = ('access_denied', 'http_forbidden', 'challenge_denied', 'access_denied_page',
                 'http_401', 'http_403',
                 # The official site sent us to its identity provider. The column
                 # and its public notices are fine; nothing here is a parser task.
                 'source_login_required', 'source_access_restricted')
THROTTLED_CODES = ('http_429', 'http_budget_exhausted', 'origin_busy', 'browser_busy', 'browser_pending')
ADAPTER_CODES = ('needs_adapter', 'content_not_found', 'invalid_api_profile', 'content_not_recognized',
                 'readiness_missing', 'invalid_readiness_selector', 'invalid_empty_selector',
                 'empty_response', 'non_html_resource', 'api_response_changed', 'api_mapping_changed',
                 'invalid_source_profile')
GONE_CODES = ('http_404', 'http_410')
RENDER_CODES = ('fragment_route', 'javascript_shell', 'http_412', 'page_size_requires_review')
INCOMPLETE_COLLECTION_STATES = frozenset({'repairing', 'retry_wait', 'discovering', 'needs_check', 'partial', 'skipped'})


def collection_display_state(task, result):
    """A worker can finish handing off a collection that is still incomplete.

    Preserve the execution row and its retry schedule. Only the public read
    state reflects the saved collection outcome, never a guessed success.
    A new pending/running generation takes precedence over its old result.
    """
    if task.state != 'done':
        return task.state
    if result.get('skipped') is True:
        return 'skipped'
    outcome = result.get('state')
    if outcome == 'waiting':
        return 'retry_wait'
    if isinstance(outcome, str) and outcome in INCOMPLETE_COLLECTION_STATES:
        return outcome
    return 'partial' if result.get('partial') is True else 'done'


def source_status_kind(state, message, error_code=''):
    """Machine-readable category so the UI need not parse Chinese labels.

    Distinguishes an official-site access limit (nothing is broken here) from a
    program/rule fault (the program must fix it) and from a genuine user task.
    """
    if state == 'waiting' or error_code in MANUAL_CODES:
        return 'manual_verification'
    if state in INCOMPLETE_COLLECTION_STATES:
        return state
    if state == 'failed':
        if error_code in LIMITED_CODES:
            return 'access_limited'
        if error_code in THROTTLED_CODES or 'HTTP 429' in message:
            return 'throttled'
        if error_code in ADAPTER_CODES or any(word in message for word in ('未识别', '解析规则')):
            return 'needs_adapter'
        if error_code in GONE_CODES:
            return 'address_gone'
        if error_code in RENDER_CODES:
            return 'render_required'
        if any(word in message for word in ('访问校验', '拒绝自动访问', 'HTTP 401', 'HTTP 403')):
            return 'access_limited'
        return 'unreadable'
    return {'unloaded': 'unloaded', 'pending': 'pending', 'running': 'running',
            'unavailable': 'unavailable'}.get(state, '')


def source_status_label(state, message, error_code=''):
    return {'manual_verification': '需要验证', 'access_limited': '访问受限', 'throttled': '请求受限',
            'needs_adapter': '待适配', 'address_gone': '地址失效', 'render_required': '需要渲染',
            'unreadable': '暂不可读', 'unloaded': '待采集', 'pending': '等待更新', 'running': '更新中',
            'repairing': '待恢复读取', 'retry_wait': '等待重试', 'discovering': '待识别',
            'needs_check': '尚未读通', 'partial': '部分更新', 'skipped': '本次未采集',
            'unavailable': '官网未提供链接'}.get(source_status_kind(state, message, error_code), '')


def refresh_status(departments):
    departments = list(departments)
    school_ids = {d.school_id for d in departments}
    school_names = dict(db.session.execute(select(School.id, School.name).where(School.id.in_(school_ids))).all()) if school_ids else {}
    identities = [f'collect:{d.id}' for d in departments]
    jobs = {t.identity: t for t in BackgroundTask.query.filter(BackgroundTask.identity.in_(identities))} if identities else {}
    sources = []
    for dept in departments:
        task = jobs.get(f'collect:{dept.id}')
        result = task.result or {} if task else {}
        state = (collection_display_state(task, result) if task else
                 'saved' if dept.last_scraped_at else 'unloaded') if dept.list_url else 'unavailable'
        message = task.error or result.get('message', '') if task else ''
        progress = (task.checkpoint or {}).get('collection_progress', {}) if task else {}
        latest = next(reversed(progress.values())) if progress else {}
        if task and state in ('pending', 'running') and progress:
            if latest.get('page', 1) > 1 and not latest.get('finished'):
                message = f"已检查 {latest['page'] - 1} 页，继续抓取历史通知"
        sources.append({'id': dept.id, 'name': dept.name, 'state': state,
                        'school_id': dept.school_id, 'school_name': school_names.get(dept.school_id, ''),
                        'configured': bool(dept.list_url and dept.list_selector),
                        'task_state': task.state if task else None,
                        'partial': state in INCOMPLETE_COLLECTION_STATES,
                        'status_label': source_status_label(state, message, task.error_code if task else ''),
                        'status_kind': source_status_kind(state, message, task.error_code if task else ''),
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
    incomplete = sum(s['state'] in INCOMPLETE_COLLECTION_STATES for s in sources)
    return {'sources': sources, 'total': len(sources), 'active': active, 'failed': failed, 'done': done,
            'incomplete': incomplete, 'processed': done + failed + incomplete,
            'new_count': sum(s['new_count'] for s in sources)}
