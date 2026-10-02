"""Cooperative discovery pause: keep checkpoints and paid-call identities intact."""
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update

from backend.database.db import db
from backend.database.models import BackgroundTask, RuntimeLease, VerificationSession

PAUSE_KEY = 'discovery_pause_requested'
RESUME_KEY = 'discovery_resume'
CHILD_KINDS = ('onboard', 'navigation_review')


def requested(task):
    return bool(task and (task.payload or {}).get(PAUSE_KEY))


def children(school_id):
    return BackgroundTask.query.filter(BackgroundTask.kind.in_(CHILD_KINDS),
        BackgroundTask.payload['school_id'].as_integer() == school_id,
        BackgroundTask.state.in_(('pending', 'running', 'waiting')))


def _read_payload(statement):
    session = db.session()
    if session.in_transaction():
        with session.no_autoflush:
            return session.execute(statement).scalar_one_or_none()
    with db.engine.connect() as connection:
        return connection.execute(statement).scalar_one_or_none()


def should_pause(kind, payload):
    if kind not in ('discover', *CHILD_KINDS):
        return False
    if payload.get(PAUSE_KEY):
        return True
    if kind in CHILD_KINDS:
        parent = _read_payload(select(BackgroundTask.payload).where(
            BackgroundTask.identity == f'discover:{payload.get("school_id")}'))
        return bool(parent and parent.get(PAUSE_KEY))
    return False


def _resume_values(state, phase, capability, available_at):
    return dict(state=state, phase=phase, capability=capability,
                available_at=available_at.isoformat() if available_at else None)


def _live(task, now):
    return task.state == 'running' and task.lease_until and task.lease_until > now


def _pause(task, now):
    from backend.services.tasks import _source_key
    payload = dict(task.payload or {})
    if requested(task):
        return
    payload[PAUSE_KEY] = True
    if not _live(task, now):
        payload[RESUME_KEY] = _resume_values('pending' if task.state == 'running' else task.state,
                                            task.phase, task.capability, task.available_at)
        task.state, task.phase = 'waiting', 'user_paused'
        if task.token:
            db.session.execute(delete(RuntimeLease).where(
                RuntimeLease.key == _source_key(task.kind, payload), RuntimeLease.token == task.token))
        task.token = task.worker_id = task.lease_until = None
    task.payload, task.updated_at = payload, now


def _resume(task, now):
    payload = dict(task.payload or {})
    resume = payload.pop(RESUME_KEY, {})
    payload.pop(PAUSE_KEY, None)
    task.state = resume.get('state') if resume.get('state') in ('waiting', 'done', 'failed') else 'pending'
    task.phase = resume.get('phase') or 'fetch'
    task.capability = resume.get('capability') or task.capability
    available = datetime.fromisoformat(resume['available_at']) if resume.get('available_at') else now
    task.available_at = max(now, available)
    task.queued_at = task.updated_at = now
    task.deadline_at = now + timedelta(hours=2)
    if task.state == 'waiting' and task.phase == 'verification':
        verification = db.session.get(VerificationSession, (task.checkpoint or {}).get('verification_id'))
        if verification and verification.status == 'verified':
            task.state, task.phase, task.capability = 'pending', 'render', 'browser'
    if task.state not in ('done', 'failed'):
        task.error = task.error_code = ''
    task.payload = payload


def control(school_id, action):
    """Serialize UI commands with worker handoffs. Never change a live worker's lease."""
    now = datetime.utcnow()
    identity = f'discover:{school_id}'
    db.session.execute(update(BackgroundTask).where(BackgroundTask.identity == identity).values(
        updated_at=BackgroundTask.updated_at))
    task = BackgroundTask.query.filter_by(identity=identity).populate_existing().first()
    if task is None:
        raise ValueError('还没有可暂停的发现任务')
    # Serialize child state changes with worker handoffs, without revoking any
    # active lease. A running page saves its result at the next safe boundary.
    children(school_id).update({BackgroundTask.updated_at: BackgroundTask.updated_at}, synchronize_session=False)
    pending_children = children(school_id).populate_existing().all()
    if action == 'pause':
        if requested(task):
            db.session.commit()
            return
        if task.state not in ('pending', 'running', 'waiting') and not pending_children:
            raise ValueError('本轮发现已结束，无需暂停')
        for item in [task, *pending_children]:
            _pause(item, now)
    elif action == 'resume':
        if not requested(task):
            db.session.commit()
            return
        if task.state != 'waiting' or task.phase != 'user_paused' or any(_live(t, now) for t in pending_children):
            raise ValueError('正在保存当前进度，请稍后继续')
        for item in [task, *pending_children]:
            if requested(item):
                if item.state == 'running':
                    park_requested_claim(item, now)
                    db.session.refresh(item)
                _resume(item, now)
    else:
        raise ValueError('不支持的发现操作')
    task.updated_at = now
    db.session.commit()


def pause_if_requested():
    """Called at completed page/candidate boundaries, never halfway through paid output."""
    from backend.services import tasks
    handle = tasks.current_execution()
    if not handle or handle.get('kind') not in ('discover', *CHILD_KINDS):
        return
    statement = select(BackgroundTask.payload).where(
        BackgroundTask.id == handle['id'], BackgroundTask.token == handle['token'],
        BackgroundTask.state == 'running')
    # Do not open an ORM transaction spanning the next network request.
    payload = _read_payload(statement)
    if payload and should_pause(handle['kind'], payload):
        tasks.defer(capability=handle['capability'], phase=handle['phase'],
                    checkpoint=handle.get('checkpoint'), reason='已暂停，进度已保存', keep_place=True)


def park_requested_claim(task, now):
    """A worker restart must honor saved pause intent before applying expired deadlines."""
    payload = dict(task.payload or {})
    payload[PAUSE_KEY] = True
    payload[RESUME_KEY] = _resume_values('pending', task.phase, task.capability, task.available_at)
    changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.id == task.id,
        or_(BackgroundTask.state == 'pending', (BackgroundTask.state == 'running') &
            (BackgroundTask.lease_until <= now))).values(
                state='waiting', phase='user_paused', payload=payload, token=None, worker_id=None,
                lease_until=None, updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount and task.token:
        from backend.services.tasks import _source_key
        db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == _source_key(task.kind, payload),
                                                       RuntimeLease.token == task.token))


def honor_pause(handle, values):
    """Apply pause to any competing handoff/retry, under the same ownership row lock."""
    if handle.get('kind') not in ('discover', *CHILD_KINDS):
        return values
    from backend.services import tasks
    locked = db.session.execute(update(BackgroundTask).where(*tasks._owner_where(handle)).values(
        updated_at=BackgroundTask.updated_at))
    if not locked.rowcount:
        return values
    payload = dict(db.session.execute(select(BackgroundTask.payload).where(
        BackgroundTask.id == handle['id'])).scalar_one() or {})
    if not should_pause(handle['kind'], payload):
        return values
    unfinished_children = handle['kind'] == 'discover' and children(payload.get('school_id')).first() is not None
    if values['state'] in ('done', 'failed') and not unfinished_children:
        # A completed page needs no future work to suspend; a school may still
        # have independent columns which must remain paused after its last page.
        payload.pop(PAUSE_KEY, None)
        payload.pop(RESUME_KEY, None)
    else:
        payload[PAUSE_KEY] = True
        payload[RESUME_KEY] = _resume_values(values['state'], values['phase'],
            values.get('capability', handle['capability']), values.get('available_at'))
        values.update(state='waiting', phase='user_paused', error='', error_code='')
    values['payload'] = payload
    return values
