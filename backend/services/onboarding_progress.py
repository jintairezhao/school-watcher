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
        return '开启 AI 辅助，识别学校部门与栏目'
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
    return 'AI 辅助已完成本批识别' if succeeded else 'AI 辅助已开启'


def status(school):
    task = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    data = dict((task.checkpoint or {}).get('discovery_progress') or {}) if task else {}
    count = Department.query.filter_by(school_id=school.id).count()
    from backend.database.source_governance_models import SourceProposal
    review_count = SourceProposal.query.filter_by(school_id=school.id, state='needs_review').count()
    available = ai_available()
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
        message = ('本轮发现已完成' if count else
                   '已发现候选栏目，核实后可订阅' if review_count else '暂未找到可订阅栏目，可用 AI 重新识别')
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
    from backend.services.discovery_control import requested
    pause_requested = requested(task)
    if pause_requested and task.state in ('pending', 'running', 'waiting'):
        if task.state == 'waiting' and task.phase == 'user_paused':
            state, message = 'paused', '已暂停，进度已保存'
        else:
            state, message = 'pausing', '正在暂停，等待当前处理完成'
    return {'state': state, 'message': message, 'phase': phase,
            'checked_pages': data.get('checked_pages', 0), 'pending_pages': data.get('pending_pages', 0),
            'failed_pages': data.get('failed_pages', 0), 'current_label': data.get('current_label', ''),
            'updated_at': data.get('updated_at'), 'source_count': count, 'review_count': review_count,
            'ai_available': available, 'ai_state': data.get('ai_state', 'not_started'),
            'ai_message': _ai_message(task, data, available),
            'needs_verification': bool(task and task.state == 'waiting' and task.phase == 'verification'),
            'active': state in ('pending', 'running', 'recovering', 'pausing'),
            'can_pause': state in ('pending', 'running', 'recovering', 'waiting', 'unavailable'),
            'can_resume': state == 'paused',
            'can_retry': state in ('idle', 'done', 'failed', 'unavailable'),
            'task_id': task.id if task else None}
