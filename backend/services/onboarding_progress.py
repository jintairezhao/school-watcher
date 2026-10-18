"""Subscriber-visible discovery state, independent of article scrape logs."""
from datetime import datetime, timedelta

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, WorkerHeartbeat


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
    if data.get('ai_state') == 'disabled':
        return '本校已关闭 AI 识别，仍会自动查找和更新公开通知'
    unconfirmed=(data.get('ai_usage') or {}).get('unconfirmed_requests',0)
    if unconfirmed:
        return f'{unconfirmed} 次模型请求结果未确认，预留额度保留；已有进度和已接入栏目保留，不会自动重发'
    if data.get('ai_error_code') == 'provider_connection_paused':
        return '官网识别因连续连接失败暂停；请重新测试当前模型配置，成功后自动继续。进度已保存，已接入栏目继续更新'
    if data.get('ai_error_code') == 'ai_result_unknown':
        return 'AI 返回结果尚未确认，进度已保存；已接入的栏目继续更新'
    if not available:
        return 'AI 尚未开启，仍会自动查找和更新公开通知'
    if data.get('ai_error_code') == 'budget_exhausted':
        return '额度已用完，进度已保存；已接入的栏目继续更新'
    if data.get('ai_state') == 'running':
        return '正在处理 AI 官网识别任务'
    if data.get('ai_state') == 'queued':
        return '已安排 AI 识别官网入口，已接入的栏目继续更新'
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
    reviewed, cached = data.get('ai_reviewed_routes', 0), data.get('ai_cached_routes', 0)
    if reviewed:
        return f'AI 已识别 {reviewed} 个官网入口' + (f'，另复用 {cached} 个已识别入口' if cached else '')
    if cached:
        return f'已复用 {cached} 个 AI 识别的官网入口'
    return 'AI 辅助已完成本批识别' if succeeded or data.get('ai_state') == 'succeeded' else 'AI 辅助已开启'


def _connection_resumed(job):
    resumes = (job.checkpoint or {}).get('ai_connection_resume', 0)
    return (type(resumes) is int and resumes > 0 and job.state in ('pending', 'running')
            and job.error_code != 'provider_connection_paused')


def _current_ai_progress(job):
    """Project old checkpoints without rewriting progress or replaying a call."""
    progress = dict((job.checkpoint or {}).get('discovery_progress') or {}) if job else {}
    if job and _connection_resumed(job) and progress.get('ai_error_code') == 'provider_connection_paused':
        progress.update(ai_state='running' if job.state == 'running' else 'queued', ai_error_code='')
    return progress


def _usage_message(usage):
    requests, used, reserved = (usage.get(key, 0) for key in ('used_requests', 'used_tokens', 'reserved_tokens'))
    if not requests and not reserved:
        return ''
    message = f'本轮累计调用 {requests:,} 次（含未完成调用） · 服务商已确认用量 {used:,} Token'
    if reserved:
        message += f' · 另预留 {reserved:,} Token（最终用量待确认）'
    if usage.get('unconfirmed_requests'):
        message += f" · {usage['unconfirmed_requests']:,} 次调用结果未确认"
    return message


def _navigation_progress(task, jobs, data):
    """Independent navigation workers retain their own actual AI progress."""
    data = dict(data)
    if not task:
        return data
    if (task.payload or {}).get('ai_assist') is False:
        return dict(data, ai_state='disabled', ai_error_code='', ai_reviewed_routes=0, ai_cached_routes=0)
    children = [job for job in jobs if job.kind == 'navigation_review' and
        job.payload.get('school_generation', job.payload.get('parent_generation')) == task.generation]
    reviewed = sum(max((job.result or {}).get('reviewed_routes', 0),
                       (job.checkpoint or {}).get('route_cursor', 0)) for job in children)
    cached = sum((job.result or {}).get('cached_routes', 0) for job in children)
    data.update(ai_reviewed_routes=reviewed, ai_cached_routes=cached)
    failures = [job for job in children if job.state == 'failed' or
        ((job.result or {}).get('status') == 'needs_recovery' and not (
            _connection_resumed(job) and (job.result or {}).get('error_code') == 'provider_connection_paused')) or
        _current_ai_progress(job).get('ai_state') in ('failed', 'uncertain')]
    if failures:
        latest = max(failures, key=lambda job: job.updated_at)
        progress = _current_ai_progress(latest)
        data.update(ai_state='failed', ai_error_code=(latest.result or {}).get('error_code') or
                    progress.get('ai_error_code') or latest.error_code)
    elif any(job.state == 'running' for job in children):
        data.update(ai_state='running', ai_error_code='')
    elif any(job.state in ('pending', 'waiting') for job in children):
        data.update(ai_state='queued', ai_error_code='')
    elif reviewed or cached:
        data.update(ai_state='succeeded', ai_error_code='')
    return data


def status(school):
    """Report useful output; website organisation is never a reader's task."""
    from backend.database.source_governance_models import SourceProposal, SchoolOnboarding
    from backend.services.discovery_control import requested
    task = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    data = _current_ai_progress(task)
    columns = Department.query.filter_by(school_id=school.id).all()
    count = sum(bool(d.list_selector) for d in columns)
    jobs = [t for t in BackgroundTask.query.filter(BackgroundTask.kind.in_(
        ('onboard', 'navigation_review', 'student_assessment'))).all() if t.payload.get('school_id') == school.id
        and (t.kind != 'student_assessment' or t.payload.get('automatic_source') or t.payload.get('automatic_listing'))]
    # An earlier navigation generation is history, not the current round's
    # connection state. Its billing ledger remains available in school_usage.
    if task:
        jobs = [job for job in jobs if job.kind != 'navigation_review' or
            job.payload.get('school_generation', job.payload.get('parent_generation')) == task.generation]
    data = _navigation_progress(task, jobs, data)
    from backend.ai.school_budget import school_usage
    usage = school_usage(school.id)
    data['ai_usage'] = usage
    budget_paused = any(job.state == 'waiting' and job.phase == 'ai_budget'
                        for job in ([task] if task else []) + jobs)
    if budget_paused:
        data.update(ai_state='budget_paused', ai_error_code='budget_exhausted')
    waiting_ai = [job for job in ([task] if task else []) + jobs if job.state == 'waiting'
                  and (job.phase in ('ai_connection', 'ai_result') or
                       job.error_code in ('provider_connection_paused', 'ai_result_unknown'))]
    ai_wait_state = ''
    if waiting_ai and data.get('ai_state') != 'disabled':
        unknown = any(job.phase == 'ai_result' or job.error_code == 'ai_result_unknown' for job in waiting_ai)
        ai_wait_state = 'ai_result' if unknown else 'ai_connection'
        data.update(ai_state='result_unknown' if unknown else 'connection_paused',
                    ai_error_code='ai_result_unknown' if unknown else 'provider_connection_paused')
    from backend.services.announcement_sources import source_expression
    article_count = Announcement.query.filter(source_expression(school_ids=[school.id])).count()
    from backend.database.student_information_models import StudentAssessment
    assessments = StudentAssessment.query.filter_by(school_id=school.id, state='ready').all()
    source_assessments = [r for r in assessments if r.subject_key.startswith('source:')]
    useful_source_count = sum(r.result.get('value') in ('relevant', 'potential', 'mixed') or
                              r.result.get('historical') == 'high' for r in source_assessments)
    now = datetime.utcnow()
    live = [t for t in jobs if t.state in ('pending', 'running', 'waiting')]
    running = [t for t in live if t.state == 'running' and t.lease_until and t.lease_until > now]
    parent_running = bool(task and task.state == 'running' and task.lease_until and task.lease_until > now)
    pause = requested(task)
    busy = bool(parent_running or running) and not pause
    state = task.state if task else 'idle'
    message = '正在准备查找学生信息'
    active = bool(live or task and task.state in ('pending', 'running'))
    retry_at = None
    if pause:
        recoverable = task.state == 'waiting' and task.phase == 'user_paused' or (
            task.state == 'running' and not parent_running)
        state = 'paused' if recoverable and not any(
            t.kind in ('onboard', 'navigation_review', 'student_assessment') for t in running) else 'pausing'
        message = '已暂停，进度已保存' if state == 'paused' else '正在暂停，等待当前处理完成'
        active = state == 'pausing'
    elif busy:
        state = 'running'
        message = '正在读取官网通知' if any(t.kind == 'onboard' for t in running) else '正在查找对学生有用的信息'
        if not parent_running and running and all(t.kind == 'student_assessment' for t in running):
            message = '正在判断通知对学生的价值'
        if article_count:
            message += f'，已找到 {article_count} 条，可以先阅读'
    elif active:
        pending = [t for t in live if t.state == 'pending']
        if task and task.state == 'pending':
            pending.append(task)
        ready = [t for t in pending if t.available_at <= now]
        if ready:
            state, message = 'queued', '等待继续查找学生信息' if data else '已关注，正在安排查找信息'
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
        message = f'已找到 {article_count} 条通知，后续自动更新' if article_count else '本轮暂未读到公开通知，学校已保留'
    if budget_paused and not pause and not ai_wait_state:
        message = '额度已用完，进度已保存；已接入的栏目继续更新'
        if not busy and not any(t.state == 'pending' for t in live) and not (task and task.state == 'pending'):
            state, active = 'budget_paused', False
    if ai_wait_state and not pause and not any(t.state in ('pending', 'running')
                                               for t in ([task] if task else []) + live):
        # A saved wait needs no running worker and must not suggest a fresh run.
        state, active = ai_wait_state, False
        message = _ai_message(task, data, True)
    gaps = []
    recovery_jobs = []
    for job in jobs:
        if job.kind == 'student_assessment':
            continue
        result = job.result or {}
        if result.get('recovery_rules') and job.state in ('done', 'failed'):
            recovery_jobs.append(job)
        if job.kind == 'navigation_review' and result.get('status') == 'needs_recovery' and job not in waiting_ai:
            gaps.append({'name': job.payload.get('page', {}).get('label', ''),
                'url': job.payload.get('page', {}).get('url', ''),
                'reason': '部分入口用途尚待判断，已保留官网路径，可稍后重新查找'})
        if job.state == 'failed' or result.get('state') == 'unsupported' or result.get('issues'):
            codes = [code for issue in result.get('issues', []) for code in issue.get('codes', [])]
            access_codes = {'article_access_restricted', 'pagination_access_restricted'}
            material_codes = {'article_material_incomplete', 'pagination_material_incomplete'}
            kind = ('access_limited' if codes and all(code in access_codes for code in codes)
                    else 'material_limited' if codes and all(code in material_codes for code in codes)
                    else 'unresolved')
            gaps.append({'name': job.payload.get('label', ''), 'url': job.payload.get('url', ''),
                'kind': kind, 'reason': result.get('reason') or '该页面暂未接入，稍后可重试'})
    if data.get('entry_failure'):
        gaps.insert(0, {'name': school.name, 'url': school.url, 'reason': data['entry_failure']['reason']})
    if data.get('failed_pages'):
        gaps.append({'reason': f"有 {data['failed_pages']} 个官网入口暂未读取成功"})
    if not active and not pause and not budget_paused and not ai_wait_state and article_count and gaps:
        message = f'已找到 {article_count} 条通知，部分官网页面暂不可读'
    if recovery_jobs and not active and not pause and not budget_paused and not ai_wait_state and state != 'failed':
        state = 'retry_wait'
        message = ('已接通的栏目继续更新，部分来源进度已保存，后台稍后恢复'
                   if count else '部分来源暂时不可读取，进度已保存，后台稍后恢复')
        dates = [job.result.get('recovery_not_before') for job in recovery_jobs
                 if job.result.get('recovery_not_before')]
        if dates:
            retry_at = min(dates)
            if not retry_at.endswith('Z') and '+' not in retry_at:
                retry_at += 'Z'
    onboarding = db.session.get(SchoolOnboarding, school.id)
    import json
    departments = len(json.loads(onboarding.scope_json or '[]')) if onboarding else len({d.group_name for d in columns if d.group_name})
    official_units = sum(d.kind == 'unit' for d in columns)
    if official_units:
        departments = official_units
    available = ai_available()
    if pause:
        stage_message = '进度已保存，继续后从这里接着查找' if state == 'paused' else message
    elif ai_wait_state:
        stage_message = ('AI 识别进度已保存，其他信息仍在处理' if busy else
                         'AI 识别进度已保存，其他信息等待处理' if active else
                         '已保留本次处理记录，未重复调用 AI' if ai_wait_state == 'ai_result' else
                         '进度已保存，等待 AI 连接恢复')
    elif budget_paused:
        stage_message = '未完成的入口已保留，调整额度后从原进度继续'
    else:
        stage_message = f'正在处理 {len(live)} 个后台任务' if live else '本轮后台处理已结束'
    review_count = SourceProposal.query.filter_by(school_id=school.id, state='needs_review').count()
    from backend.services.discovery_policy import attention_summary
    return {'state': state, 'message': message, 'stage_message': stage_message, 'phase': data.get('phase', 'fetch'),
        'value_summary': attention_summary(school.id),
        'busy': busy, 'active': active, 'retry_at': retry_at,
        'checked_pages': data.get('checked_pages', 0), 'pending_pages': data.get('pending_pages', 0),
        'failed_pages': data.get('failed_pages', 0), 'current_label': data.get('current_label', '') if busy else '',
        'changes': data.get('changes', {}),
        'updated_at': data.get('updated_at'), 'source_count': len(columns), 'verified_source_count': count,
        'article_count': article_count, 'assessed_source_count': len(source_assessments),
        'useful_source_count': useful_source_count,
        'department_count': departments, 'processing_count': len(live),
        'failed_count': sum(gap.get('kind') not in ('access_limited', 'material_limited') for gap in gaps),
        'unit_checked': data.get('unit_checked', 0), 'unit_pending': data.get('unit_pending', 0),
        'unit_failed': data.get('unit_failed', 0),
        'column_tasks': sum(t.kind == 'onboard' for t in live),
        'assessment_tasks': sum(t.kind == 'student_assessment' for t in live),
        'waiting_recovery_count': len(recovery_jobs), 'review_count': review_count, 'queued_review_count': 0,
        'background_exploration': any(t.kind == 'navigation_review' for t in live),
        'ai_available': available, 'ai_state': data.get('ai_state', 'not_started'),
        'ai_reviewed_routes': data.get('ai_reviewed_routes', 0), 'ai_cached_routes': data.get('ai_cached_routes', 0),
        'ai_message': _ai_message(task, data, available),
        'ai_usage': usage, 'ai_usage_message': _usage_message(usage), 'budget_paused': budget_paused,
        'coverage': {'complete': False, 'gaps': gaps, 'placement_gap_count': 0},
        'incomplete_reasons': [g['reason'] for g in gaps],
        'needs_verification': any(t.state == 'waiting' and t.phase == 'verification' for t in ([task] if task else []) + live),
        'can_pause': bool(task and not pause and (task.state in ('pending', 'running', 'waiting') or any(
            t.kind in ('onboard', 'navigation_review', 'student_assessment') for t in live))),
        'can_resume': state == 'paused',
        'can_retry': not active and not ai_wait_state and state not in ('paused', 'budget_paused')
                     and (state != 'waiting' or bool(task and task.phase == 'ai_setup')),
        'task_id': task.id if task else None}
