"""Saved task handoffs for a verified model connection, without replaying spend."""
from datetime import datetime, timedelta

from backend.database.db import db

PAUSED = 'provider_connection_paused'
REASON = '模型连接连续失败，进度已保存；连接测试成功后继续'


def binding_ref(binding):
    return {key: binding.get(key) for key in ('id', 'version', 'provider', 'model')}


def defer_connection(binding, checkpoint=None, capability='directory'):
    from backend.services import tasks
    handle = tasks.current_execution() or {}
    saved = dict(handle.get('checkpoint') or {})
    if checkpoint is not None:
        saved.update(checkpoint)
    saved['ai_connection_binding'] = binding_ref(binding)
    raise tasks.TaskDeferred(capability=capability, phase='ai_connection', state='waiting',
        error_code=PAUSED, reason=REASON, checkpoint=saved)


def unsent_pause(row):
    usage = (row.usage or {}) if row else {}
    return bool(row and row.status == 'failed' and row.error_code == PAUSED and row.request_sent is False
        and usage.get('known') is True and type(usage.get('total_tokens')) is int and usage['total_tokens'] == 0
        and all(type(usage.get(key, 0)) is int and usage.get(key, 0) == 0 for key in ('input_tokens', 'output_tokens'))
        and row.reserved_tokens == 0 and not row.output)


def resumed_execution_id(execution_id, checkpoint=None):
    """Only an explicit task resume can replace a proven-unsent pause record."""
    from backend.ai.models import AIExecution
    from backend.ai.skill_loader import digest
    from backend.services import tasks
    if checkpoint is None:
        checkpoint = (tasks.current_execution() or {}).get('checkpoint') or {}
    resumes = checkpoint.get('ai_connection_resume', 0)
    if type(resumes) is not int or resumes <= 0:
        return execution_id
    original = execution_id
    for resume in range(1, resumes + 1):
        row = db.session.get(AIExecution, execution_id)
        if not unsent_pause(row):
            break
        execution_id = 'connection-resume:' + digest([original, resume])
    return execution_id


def resume_connection_tasks(binding):
    """Called after a successful current-version connection test; no new jobs."""
    from backend.ai.models import AIExecution, AIProfile
    from backend.ai.configuration import _metadata
    from backend.ai.runtime import _connection_paused
    from backend.ai.summary_models import AnnouncementSummary
    from backend.database.models import BackgroundTask
    from backend.services.discovery_control import requested, should_pause
    if not binding.get('id'):
        return 0
    profile = db.session.get(AIProfile, binding['id'])
    if not profile or not profile.enabled or profile.tested_version != profile.version or any(
            getattr(profile, key) != binding.get(key) for key in ('provider', 'model')):
        return 0
    binding = _metadata(profile)
    verified = AIExecution.query.filter_by(profile_id=binding['id'], config_version=str(binding['version']),
        provider=binding['provider'], model=binding['model'], skill_id='connection-test', status='succeeded')
    if not any((row.usage or {}).get('known') is True and
               (row.diagnostics or {}).get('connection_test_verified') is True for row in verified):
        return 0
    if _connection_paused(binding):
        return 0
    resumed, now = 0, datetime.utcnow()
    for job in BackgroundTask.query.filter_by(state='waiting', phase='ai_connection', error_code=PAUSED):
        saved = dict(job.checkpoint or {})
        previous = saved.get('ai_connection_binding') or {}
        if any(previous.get(key) != binding.get(key) for key in ('id', 'provider', 'model')):
            continue
        if requested(job) or should_pause(job.kind, job.payload or {}):
            continue
        saved['ai_connection_resume'] = int(saved.get('ai_connection_resume', 0)) + 1
        saved['ai_connection_binding'] = binding_ref(binding)
        job.checkpoint = saved
        # A tested credential update can continue this same model's saved work.
        payload = dict(job.payload or {})
        if isinstance(payload.get('binding'), dict) and payload['binding'].get('id') == binding.get('id'):
            payload['binding'] = {key: binding.get(key, value) for key, value in payload['binding'].items()}
            job.payload = payload
        if job.kind == 'summary':
            summary = db.session.get(AnnouncementSummary, payload.get('summary_id'))
            if summary and summary.state == 'pending' and summary.error_code == PAUSED:
                summary.binding = dict(binding)
                summary.error = summary.error_code = ''
        job.state, job.phase = 'pending', 'ai_resume'
        job.available_at = job.updated_at = now
        if not job.deadline_at or job.deadline_at <= now:
            job.deadline_at = now + timedelta(hours=2)
        job.error = job.error_code = ''
        resumed += 1
    db.session.commit()
    return resumed
