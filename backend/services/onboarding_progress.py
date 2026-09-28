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


def status(school):
    task = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    data = dict((task.checkpoint or {}).get('discovery_progress') or {}) if task else {}
    count = Department.query.filter_by(school_id=school.id).count()
    state = task.state if task else 'idle'
    now = datetime.utcnow()
    worker = WorkerHeartbeat.query.filter(WorkerHeartbeat.stopped_at.is_(None),
        WorkerHeartbeat.heartbeat_at > now - timedelta(seconds=90)).first()
    phase = data.get('phase', 'fetch')
    message = {'fetch': '正在读取官网', 'ai': 'AI 正在识别部门与栏目',
               'crawl': '正在发现部门与栏目', 'verify': '正在核实栏目',
               'complete': '本轮发现已完成'}.get(phase, '正在发现部门与栏目')
    if state == 'idle':
        message = '尚未开始发现栏目'
    elif state == 'failed':
        message = '栏目发现未完成，请重试'
    elif state == 'waiting':
        message = '配置 AI 后开始发现栏目' if task.phase == 'ai_setup' else '官网需要访问验证，完成后继续'
    elif state == 'done':
        message = '本轮发现已完成' if count else '暂未找到可订阅栏目，可用 AI 重新识别'
    elif state == 'pending':
        if task.phase == 'retry':
            message = '上次发现中断，正在等待重试'
        elif task.phase == 'origin_wait':
            message = '等待官网响应，稍后继续'
        else:
            message = '等待继续发现' if data else '已加入发现队列'
        if worker is None and (now - task.queued_at).total_seconds() > 90:
            message = '发现服务暂未响应，请重启应用后重试'
            state = 'unavailable'
    elif state == 'running' and task.lease_until and task.lease_until < now:
        state, message = 'recovering', '发现任务中断，正在恢复'
    return {'state': state, 'message': message, 'phase': phase,
            'checked_pages': data.get('checked_pages', 0), 'pending_pages': data.get('pending_pages', 0),
            'failed_pages': data.get('failed_pages', 0), 'current_label': data.get('current_label', ''),
            'updated_at': data.get('updated_at'), 'source_count': count,
            'ai_available': ai_available(), 'ai_state': data.get('ai_state', 'not_started'),
            'needs_verification': bool(task and task.state == 'waiting' and task.phase == 'verification'),
            'active': state in ('pending', 'running', 'recovering'),
            'can_retry': state in ('idle', 'done', 'failed', 'unavailable'),
            'task_id': task.id if task else None}
