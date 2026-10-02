"""Subscriber-visible discovery state, independent of article scrape logs."""
from datetime import datetime, timedelta

from backend.database.db import db
from backend.database.models import BackgroundTask, Department, WorkerHeartbeat


def record_progress(**values):
    from backend.services import tasks
    handle = tasks.current_execution()
    if not handle:
        return
    data = dict(handle.get('checkpoint') or {})
    data['discovery_progress'] = dict(data.get('discovery_progress') or {}, **values,
                                     updated_at=datetime.utcnow().isoformat() + 'Z')
    tasks.checkpoint(data)


def ai_available():
    from backend.ai.configuration import get_model_binding, AIConfigError
    try:
        get_model_binding('directory')
        return True
    except AIConfigError:
        return False


def _ai_message(task, data, available):
    if not available:
        return 'AI 辅助尚未开启：明确的栏目照常接入，用途不明的入口会保留待判断'
    if data.get('ai_error_code') == 'budget_exhausted':
        return '本轮 AI 用量已达上限；明确栏目继续接入，未完成的入口已保留'
    if data.get('ai_state') == 'running':
        return 'AI 已连接，正在分析官网'
    history = dict((task.checkpoint or {}).get('ai_navigation') or {}) if task else {}
    succeeded = sum(value == 'succeeded' for value in history.values())
    failed = sum(value in ('failed', 'uncertain') for value in history.values())
    if failed or data.get('ai_state') in ('failed', 'uncertain'):
        code = data.get('ai_error_code')
        if not code and task:
            # Older saved progress only contains status; use its existing ledger.
            from backend.ai.models import AIExecution
            execution = AIExecution.query.filter(
                AIExecution.execution_id.like(f'navigation:{task.id}:{task.generation}:%'),
                AIExecution.status.in_(('failed', 'uncertain'))).order_by(AIExecution.created_at.desc()).first()
            code = execution.error_code if execution else ''
        reason = {'incomplete_model_output': '模型输出不完整', 'invalid_json': '返回格式不正确',
                  'output_validation_failed': '识别结果未通过检查', 'authentication_failed': '密钥验证失败',
                  'budget_exhausted': '已达到用量上限', 'rate_limited': '服务商暂时限流',
                  'concurrency_limit': '正在等待 AI 空闲', 'network_result_unknown': '网络响应中断'}.get(code, '本次识别未完成')
        count = f'{succeeded} 次成功、{failed or 1} 次未完成'
        return f'AI 已配置：{count}（{reason}），继续按网页规则识别。'
    return 'AI 辅助已完成本批识别' if succeeded or data.get('ai_state') == 'succeeded' else 'AI 辅助已开启'


def status(school):
    """Report actual work and usable columns; coverage audits are not onboarding."""
    from backend.database.source_governance_models import SourceProposal, SchoolOnboarding
    from backend.services.discovery_control import requested
    task = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    data = dict((task.checkpoint or {}).get('discovery_progress') or {}) if task else {}
    columns = Department.query.filter_by(school_id=school.id).all()
    count = sum(bool(d.list_selector) for d in columns)
    jobs = [t for t in BackgroundTask.query.filter(BackgroundTask.kind.in_(
        ('onboard', 'navigation_review', 'source_grouping'))).all() if t.payload.get('school_id') == school.id]
    now = datetime.utcnow()
    live = [t for t in jobs if t.state in ('pending', 'running', 'waiting')]
    running = [t for t in live if t.state == 'running' and (not t.lease_until or t.lease_until > now)]
    parent_running = bool(task and task.state == 'running' and (not task.lease_until or task.lease_until > now))
    pause = requested(task)
    busy = bool(parent_running or running) and not pause
    state = task.state if task else 'idle'
    message = '尚未开始查找栏目'
    active = bool(live or task and task.state in ('pending', 'running'))
    retry_at = None
    if pause:
        state = 'paused' if task.state == 'waiting' and task.phase == 'user_paused' and not any(
            t.kind in ('onboard', 'navigation_review') for t in running) else 'pausing'
        message = '已暂停，进度已保存' if state == 'paused' else '正在暂停，等待当前处理完成'
        active = state == 'pausing'
    elif busy:
        state = 'running'
        message = '正在接入通知栏目' if any(t.kind == 'onboard' for t in running) else '正在查找官网栏目'
        if not parent_running and running and all(t.kind == 'source_grouping' for t in running):
            message = '正在核对部门与栏目归属'
        if count:
            message += '，已接入的栏目可以使用'
    elif active:
        pending = [t for t in live if t.state == 'pending']
        if task and task.state == 'pending':
            pending.append(task)
        ready = [t for t in pending if t.available_at <= now]
        if ready:
            state, message = 'queued', '等待继续查找栏目' if data else '等待查找官网栏目'
            if task in ready:
                from backend.services.tasks import queue_ahead
                from flask import current_app
                isolated = task.capability == 'directory' and int(current_app.config.get('WORKER_CONCURRENCY', 2)) > 1
                ahead, _ = queue_ahead(task, capability='directory' if isolated else None)
                if ahead:
                    message = f'正在排队：前面还有 {ahead} 个任务'
        elif pending:
            state = 'retry_wait'
            retry_at = min(t.available_at for t in pending).isoformat() + 'Z'
            message = '暂时无法继续，稍后自动重试'
        elif any(t.state == 'waiting' and t.phase == 'verification' for t in live):
            state, message = 'waiting', '部分官网需要访问验证，已接入栏目仍可使用' if count else '官网需要访问验证，完成后继续'
        else:
            state, message = 'recovering', '正在恢复上次保存的进度'
        worker = WorkerHeartbeat.query.filter(WorkerHeartbeat.stopped_at.is_(None),
            WorkerHeartbeat.heartbeat_at > now - timedelta(seconds=90)).first()
        queued = min(t.queued_at for t in ([task] if task else []) + live)
        if not worker and (now - queued).total_seconds() > 90:
            state, message, active = 'unavailable', '后台服务未响应，请重启应用后重试', False
    elif state == 'waiting':
        message = '可直接开始查找栏目' if task.phase == 'ai_setup' else '官网需要访问验证，完成后继续'
    elif state == 'failed':
        message = '本轮查找未完成，可以重试；已接入的栏目仍可使用' if count else '本轮查找未完成，可以重试'
    elif state == 'done' or count:
        message = f'已接入 {count} 个栏目' if count else '本轮暂未找到可接入的栏目'
    gaps = []
    for job in jobs:
        if job.kind == 'source_grouping':
            continue
        result = job.result or {}
        if job.kind == 'navigation_review' and result.get('status') == 'needs_recovery':
            gaps.append({'name': job.payload.get('page', {}).get('label', ''),
                'url': job.payload.get('page', {}).get('url', ''),
                'reason': '部分入口用途尚待判断，已保留官网路径，可稍后重新查找'})
        if job.state == 'failed' or result.get('state') == 'unsupported' or result.get('issues'):
            gaps.append({'name': job.payload.get('label', ''), 'url': job.payload.get('url', ''),
                'reason': result.get('reason') or '该页面暂未接入，稍后可重试'})
    from backend.services.source_grouping import placement_gaps
    grouping_gaps = placement_gaps(school.id)
    gaps.extend(grouping_gaps)
    if data.get('entry_failure'):
        gaps.insert(0, {'name': school.name, 'url': school.url, 'reason': data['entry_failure']['reason']})
    if data.get('failed_pages'):
        gaps.append({'reason': f"有 {data['failed_pages']} 个官网入口暂未读取成功"})
    if not active and not pause and count and gaps:
        message = f'已接入 {count} 个栏目，部分页面暂未接入'
    onboarding = db.session.get(SchoolOnboarding, school.id)
    import json
    departments = len(json.loads(onboarding.scope_json or '[]')) if onboarding else len({d.group_name for d in columns if d.group_name})
    official_units = sum(d.kind == 'unit' for d in columns)
    if official_units:
        departments = official_units
    available = ai_available()
    review_count = SourceProposal.query.filter_by(school_id=school.id, state='needs_review').count()
    return {'state': state, 'message': message, 'phase': data.get('phase', 'fetch'),
        'busy': busy, 'active': active, 'retry_at': retry_at,
        'checked_pages': data.get('checked_pages', 0), 'pending_pages': data.get('pending_pages', 0),
        'failed_pages': data.get('failed_pages', 0), 'current_label': data.get('current_label', '') if busy else '',
        'changes': data.get('changes', {}),
        'updated_at': data.get('updated_at'), 'source_count': len(columns), 'verified_source_count': count,
        'department_count': departments, 'processing_count': len(live), 'failed_count': len(gaps),
        'unit_checked': data.get('unit_checked', 0), 'unit_pending': data.get('unit_pending', 0),
        'unit_failed': data.get('unit_failed', 0),
        'column_tasks': sum(t.kind == 'onboard' for t in live),
        'assessment_tasks': 0,
        'waiting_recovery_count': 0, 'review_count': review_count, 'queued_review_count': 0,
        'background_exploration': any(t.kind == 'navigation_review' for t in live),
        'ai_available': available, 'ai_state': data.get('ai_state', 'not_started'),
        'ai_message': _ai_message(task, data, available),
        'coverage': {'complete': False, 'gaps': gaps, 'placement_gap_count':len(grouping_gaps)},
        'incomplete_reasons': [g['reason'] for g in gaps],
        'needs_verification': any(t.state == 'waiting' and t.phase == 'verification' for t in ([task] if task else []) + live),
        'can_pause': bool(task and not pause and (task.state in ('pending', 'running', 'waiting') or any(
            t.kind in ('onboard', 'navigation_review') for t in live))),
        'can_resume': state == 'paused',
        'can_retry': not active and state != 'paused' and (state != 'waiting' or task.phase == 'ai_setup'),
        'task_id': task.id if task else None}
