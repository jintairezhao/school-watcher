"""Cooperative discovery pause: keep checkpoints and paid-call identities intact."""
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update

from backend.database.db import db
from backend.database.models import BackgroundTask, RuntimeLease, VerificationSession

PAUSE_KEY = 'discovery_pause_requested'
RESUME_KEY = 'discovery_resume'


def requested(task):
    return bool(task and (task.payload or {}).get(PAUSE_KEY))


def _resume_values(state, phase, capability, available_at):
    return dict(state=state, phase=phase, capability=capability,
                available_at=available_at.isoformat() if available_at else None)


def control(school_id, action):
    """Serialize UI commands with worker handoffs. Never change a live worker's lease."""
    now = datetime.utcnow()
    identity = f'discover:{school_id}'
    db.session.execute(update(BackgroundTask).where(BackgroundTask.identity == identity).values(
        updated_at=BackgroundTask.updated_at))
    task = BackgroundTask.query.filter_by(identity=identity).populate_existing().first()
    if task is None:
        raise ValueError('还没有可暂停的发现任务')
    payload = dict(task.payload or {})
    if action == 'pause':
        if requested(task):
            db.session.commit()
            return
        if task.state not in ('pending', 'running', 'waiting'):
            raise ValueError('本轮发现已结束，无需暂停')
        payload[PAUSE_KEY] = True
        live = task.state == 'running' and task.lease_until and task.lease_until > now
        if not live:
            payload[RESUME_KEY] = _resume_values('pending' if task.state == 'running' else task.state,
                                                task.phase, task.capability, task.available_at)
            task.state, task.phase = 'waiting', 'user_paused'
            if task.token:
                db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == 'directory:writer',
                                                               RuntimeLease.token == task.token))
            task.token = task.worker_id = task.lease_until = None
    elif action == 'resume':
        if not requested(task):
            db.session.commit()
            return
        if task.state != 'waiting' or task.phase != 'user_paused':
            raise ValueError('正在保存当前进度，请稍后继续')
        resume = payload.pop(RESUME_KEY, {})
        payload.pop(PAUSE_KEY, None)
        task.state = 'waiting' if resume.get('state') == 'waiting' else 'pending'
        task.phase = resume.get('phase') or 'fetch'
        task.capability = resume.get('capability') or 'directory'
        # Keep any website cooldown. Time spent paused does not consume the run budget.
        available = datetime.fromisoformat(resume['available_at']) if resume.get('available_at') else now
        task.available_at = max(now, available)
        task.queued_at = task.updated_at = now
        task.deadline_at = now + timedelta(hours=2)
        if task.state == 'waiting' and task.phase == 'verification':
            verification = db.session.get(VerificationSession, (task.checkpoint or {}).get('verification_id'))
            if verification and verification.status == 'verified':
                task.state, task.phase, task.capability = 'pending', 'render', 'browser'
        task.error = task.error_code = ''
    else:
        raise ValueError('不支持的发现操作')
    task.payload = payload
    task.updated_at = now
    db.session.commit()


def pause_if_requested():
    """Called at completed page/candidate boundaries, never halfway through paid output."""
    from backend.services import tasks
    handle = tasks.current_execution()
    if not handle or handle.get('kind') != 'discover':
        return
    statement = select(BackgroundTask.payload).where(
        BackgroundTask.id == handle['id'], BackgroundTask.token == handle['token'],
        BackgroundTask.state == 'running')
    session = db.session()
    if session.in_transaction():
        with session.no_autoflush:
            payload = session.execute(statement).scalar_one_or_none()
    else:
        # Do not open an ORM transaction that would span the next network request.
        with db.engine.connect() as connection:
            payload = connection.execute(statement).scalar_one_or_none()
    if payload and payload.get(PAUSE_KEY):
        tasks.defer(capability=handle['capability'], phase=handle['phase'],
                    checkpoint=handle.get('checkpoint'), reason='已暂停，进度已保存')


def park_requested_claim(task, now):
    """A worker restart must honor saved pause intent before applying expired deadlines."""
    payload = dict(task.payload or {})
    payload[RESUME_KEY] = _resume_values('pending', task.phase, task.capability, task.available_at)
    changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.id == task.id,
        or_(BackgroundTask.state == 'pending', (BackgroundTask.state == 'running') &
            (BackgroundTask.lease_until <= now))).values(
                state='waiting', phase='user_paused', payload=payload, token=None, worker_id=None,
                lease_until=None, updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount and task.token:
        db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == 'directory:writer',
                                                       RuntimeLease.token == task.token))


def honor_pause(handle, values):
    """Apply pause to any competing handoff/retry, under the same ownership row lock."""
    if handle.get('kind') != 'discover':
        return values
    from backend.services import tasks
    locked = db.session.execute(update(BackgroundTask).where(*tasks._owner_where(handle)).values(
        updated_at=BackgroundTask.updated_at))
    if not locked.rowcount:
        return values
    payload = dict(db.session.execute(select(BackgroundTask.payload).where(
        BackgroundTask.id == handle['id'])).scalar_one() or {})
    if not payload.get(PAUSE_KEY):
        return values
    if values['state'] in ('done', 'failed'):
        # A completed final page needs no future work to suspend.
        payload.pop(PAUSE_KEY, None)
        payload.pop(RESUME_KEY, None)
    else:
        payload[RESUME_KEY] = _resume_values(values['state'], values['phase'],
            values.get('capability', handle['capability']), values.get('available_at'))
        values.update(state='waiting', phase='user_paused', error='', error_code='')
    values['payload'] = payload
    return values
