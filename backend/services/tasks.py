"""One durable queue with conditional ownership and business commit fences."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update, delete, case, func, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.database.db import db
from backend.database.models import BackgroundTask, RuntimeLease, VerificationSession, VerificationWaiter
from backend.services.runtime_leases import insert_if_missing, acquire_in_transaction

_execution = ContextVar('task_execution', default=None)
_control = ContextVar('task_control', default=False)
ACTIVE_STATES = ('pending', 'running', 'waiting')
CAPABILITIES = ('http', 'browser', 'directory')


class LeaseLost(BaseException):
    """An expired worker must never publish late results."""


class PolicyChanged(LeaseLost):
    """A changed source profile invalidates this generation's responses."""


class TaskDeferred(BaseException):
    def __init__(self, *, capability=None, phase='fetch', checkpoint=None, delay=0,
                 reason='', state='pending', error_code=''):
        self.capability, self.phase, self.checkpoint = capability, phase, checkpoint
        self.delay, self.reason, self.state, self.error_code = delay, reason, state, error_code
        super().__init__(reason)


def current_execution():
    return _execution.get()


@contextmanager
def execution_scope(handle):
    marker = _execution.set(handle)
    try:
        yield handle
    finally:
        _execution.reset(marker)


@contextmanager
def control_transaction():
    marker = _control.set(True)
    try:
        yield
    finally:
        _control.reset(marker)


def _owner_where(handle, now=None):
    conditions = [BackgroundTask.id == handle['id'], BackgroundTask.token == handle['token'],
                  BackgroundTask.state == 'running', BackgroundTask.lease_until > (now or datetime.utcnow())]
    if 'generation' in handle:
        conditions.append(BackgroundTask.generation == handle['generation'])
    if 'policy_version' in handle:
        conditions.append(BackgroundTask.policy_version == handle['policy_version'])
    return conditions


def assert_owned(handle=None):
    """Cancellation check without retaining a transaction over network I/O."""
    handle = handle or current_execution()
    if not handle:
        return
    if handle.get('policy_validator') and not handle['policy_validator']():
        raise PolicyChanged('Source profiles changed during execution')
    if handle.get('cancelled') and handle['cancelled'].is_set():
        raise LeaseLost('Task lease was lost')
    statement = select(BackgroundTask.id).where(*_owner_where(handle))
    session = db.session()
    if session.in_transaction():
        # In-memory SQLite uses one connection. Opening/closing a second wrapper
        # would roll back this session's pending page inserts. Reuse the current
        # transaction without flushing; the commit fence remains authoritative.
        with session.no_autoflush:
            owned = session.execute(statement).scalar_one_or_none()
    else:
        with db.engine.connect() as connection:
            owned = connection.execute(statement).scalar_one_or_none()
    if owned is None:
        raise LeaseLost('Task lease was lost or configuration changed')


def _fence(session):
    handle = current_execution()
    if not handle or _control.get() or session.info.get('runtime_control'):
        return
    if handle.get('policy_validator') and not handle['policy_validator']():
        raise PolicyChanged('Source profiles changed during execution')
    if handle.get('cancelled') and handle['cancelled'].is_set():
        raise LeaseLost('Task lease was lost')
    # UPDATE locks the owner row until COMMIT; a separate preflight check alone
    # has a race between checking the lease and committing business results.
    with session.no_autoflush:
        changed = session.execute(update(BackgroundTask).where(*_owner_where(handle)).values(
            lease_until=BackgroundTask.lease_until).execution_options(synchronize_session=False))
        if not changed.rowcount:
            raise LeaseLost('Task lease was lost or configuration changed')
        if handle.get('source_key'):
            held = session.execute(update(RuntimeLease).where(RuntimeLease.key == handle['source_key'],
                RuntimeLease.token == handle['token'], RuntimeLease.expires_at > datetime.utcnow()).values(
                    expires_at=RuntimeLease.expires_at).execution_options(synchronize_session=False))
            if not held.rowcount:
                raise LeaseLost('Source lease was lost')


@event.listens_for(Session, 'before_flush')
def _fence_flush(session, flush_context, instances):
    _fence(session)


@event.listens_for(Session, 'before_commit')
def _fence_commit(session):
    _fence(session)


def default_capability(kind):
    return 'directory' if kind in ('directory', 'discover', 'source_review', 'source_baseline') else 'http'


def _freshness_column():
    # Legacy rows without checked_at retain their pre-migration admission gate.
    return func.coalesce(BackgroundTask.checked_at, BackgroundTask.updated_at)


def enqueue(kind, key, payload=None, *, delay=0, replace_finished=True, min_interval=0,
            expedite=False, capability=None, policy_version='1', commit=True):
    identity, now = f'{kind}:{key}', datetime.utcnow()
    lane = capability or default_capability(kind)
    if lane not in CAPABILITIES:
        raise ValueError('Unknown task capability')
    insert_if_missing(db.session, BackgroundTask, dict(identity=identity, kind=kind, payload=payload or {},
        result={}, checkpoint={}, state='pending', attempts=0, claim_count=0, generation=1,
        capability=lane, phase='fetch', policy_version=str(policy_version), error='', error_code='',
        available_at=now + timedelta(seconds=delay), created_at=now, queued_at=now, updated_at=now,
        deadline_at=now + timedelta(hours=2)), 'identity')
    task = BackgroundTask.query.filter_by(identity=identity).one()
    if task.state in ('done', 'failed') and replace_finished:
        eligible = [BackgroundTask.id == task.id, BackgroundTask.state.in_(('done', 'failed'))]
        if min_interval:
            eligible.append(_freshness_column() <= now - timedelta(seconds=min_interval))
        db.session.execute(update(BackgroundTask).where(*eligible).values(
            state='pending', payload=payload or {}, result={}, error='', error_code='', attempts=0,
            claim_count=0, generation=BackgroundTask.generation + 1, checkpoint={}, capability=lane,
            phase='fetch', policy_version=str(policy_version), token=None, worker_id=None, lease_until=None,
            available_at=now + timedelta(seconds=delay), queued_at=now, next_run_at=None,
            deadline_at=now + timedelta(hours=2), finished_at=None, updated_at=now))
    if expedite:
        db.session.execute(update(BackgroundTask).where(BackgroundTask.id == task.id,
            BackgroundTask.state == 'pending', BackgroundTask.phase == 'fetch', BackgroundTask.attempts == 0,
            BackgroundTask.available_at > now).values(available_at=now))
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    db.session.refresh(task)
    return task


def _source_key(kind, payload):
    if kind in ('directory', 'discover', 'source_review', 'source_baseline'):
        return 'directory:writer'
    if kind in ('collect', 'source_health') and payload.get('department_id') is not None:
        return f"source:{payload['department_id']}"
    return None


def _claim_statement(eligible, now):
    # Age removes class priority after five minutes, so body reads cannot starve
    # list refreshes. queued_at is retained on every resource/browser handoff.
    priority = case((BackgroundTask.queued_at <= now - timedelta(minutes=5), -1),
                    (BackgroundTask.kind == 'content', 0), (BackgroundTask.kind == 'collect', 1), else_=2)
    statement = select(BackgroundTask).where(eligible).order_by(priority, BackgroundTask.queued_at,
                                                              BackgroundTask.available_at, BackgroundTask.id)
    if db.engine.dialect.name == 'postgresql':
        statement = statement.with_for_update(skip_locked=True)
    return statement.limit(16)


def claim(lease_seconds=120, *, capabilities=None, worker_id='local'):
    lanes = tuple(capabilities or CAPABILITIES)
    if not set(lanes).issubset(CAPABILITIES):
        raise ValueError('Unknown worker capability')
    for _ in range(8):
        now = datetime.utcnow()
        eligible = (BackgroundTask.capability.in_(lanes) & or_(
            (BackgroundTask.state == 'pending') & (BackgroundTask.available_at <= now),
            (BackgroundTask.state == 'running') & (BackgroundTask.lease_until <= now)))
        try:
            for row in db.session.execute(_claim_statement(eligible, now)).scalars().all():
                if row.deadline_at and row.deadline_at <= now:
                    db.session.execute(update(BackgroundTask).where(BackgroundTask.id == row.id, eligible).values(
                        state='failed', error='本轮采集超过时间预算，稍后可重新安排', error_code='deadline_exceeded',
                        lease_until=None, token=None, checked_at=now, finished_at=now, updated_at=now))
                    continue
                token = uuid4().hex
                key = _source_key(row.kind, row.payload or {})
                if key and not acquire_in_transaction(db.session, key, token, worker_id, lease_seconds, now):
                    continue
                changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.id == row.id, eligible).values(
                    state='running', token=token, worker_id=worker_id, lease_until=now + timedelta(seconds=lease_seconds),
                    claim_count=BackgroundTask.claim_count + 1, updated_at=now))
                if changed.rowcount:
                    handle = dict(id=row.id, token=token, generation=row.generation, policy_version=row.policy_version,
                                  capability=row.capability, phase=row.phase, checkpoint=dict(row.checkpoint or {}),
                                  identity=row.identity, kind=row.kind, payload=dict(row.payload or {}),
                                  source_key=key, worker_id=worker_id, deadline_at=row.deadline_at, attempts=row.attempts)
                    db.session.commit()
                    return handle
                if key:
                    db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == key, RuntimeLease.token == token))
            db.session.commit()
            return None
        except OperationalError as exc:
            db.session.rollback()
            if db.engine.dialect.name != 'sqlite' or 'locked' not in str(exc).lower():
                raise
    return None


def heartbeat(handle, lease_seconds=120):
    now = datetime.utcnow()
    with control_transaction():
        result = db.session.execute(update(BackgroundTask).where(*_owner_where(handle, now)).values(
            lease_until=now + timedelta(seconds=lease_seconds)))
        if result.rowcount and handle.get('source_key'):
            source = db.session.execute(update(RuntimeLease).where(RuntimeLease.key == handle['source_key'],
                RuntimeLease.token == handle['token'], RuntimeLease.expires_at > now).values(
                    expires_at=now + timedelta(seconds=lease_seconds), updated_at=now))
            if not source.rowcount:
                db.session.rollback()
                return False
        db.session.commit()
    return bool(result.rowcount)


def _release_source(handle):
    if handle.get('source_key'):
        db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == handle['source_key'],
                                                       RuntimeLease.token == handle['token']))


def checkpoint(data):
    handle = current_execution()
    if not handle:
        raise RuntimeError('A running task is required for checkpoints')
    changed = db.session.execute(update(BackgroundTask).where(*_owner_where(handle)).values(checkpoint=dict(data)))
    if not changed.rowcount:
        raise LeaseLost('Task lease was lost')
    db.session.commit()
    handle['checkpoint'] = dict(data)


def defer(**kwargs):
    if not current_execution():
        raise RuntimeError('Only durable tasks can be handed off')
    raise TaskDeferred(**kwargs)


def handoff(handle, deferred):
    now = datetime.utcnow()
    lane = deferred.capability or handle.get('capability', 'http')
    if lane not in CAPABILITIES or deferred.state not in ('pending', 'waiting'):
        raise ValueError('Invalid task handoff')
    data = deferred.checkpoint if deferred.checkpoint is not None else handle.get('checkpoint', {})
    with control_transaction():
        values = dict(
            state=deferred.state, phase=deferred.phase, capability=lane, checkpoint=dict(data),
            available_at=now + timedelta(seconds=max(0, deferred.delay)), token=None, worker_id=None,
            lease_until=None, error=deferred.reason[:500], error_code=deferred.error_code, updated_at=now)
        if deferred.state == 'waiting' and data.get('verification_id'):
            verified = db.session.execute(select(VerificationSession.id).where(
                VerificationSession.id == data['verification_id'], VerificationSession.status == 'verified')).scalar_one_or_none()
            if verified:
                values.update(state='pending', phase='render', error='', error_code='',
                              generation=BackgroundTask.generation + 1, available_at=now)
        changed = db.session.execute(update(BackgroundTask).where(*_owner_where(handle, now)).values(**values))
        if changed.rowcount:
            _release_source(handle)
        db.session.commit()
    return bool(changed.rowcount)


def finish(handle, result=None, error=None, *, retryable=True):
    now = datetime.utcnow()
    # Final business writes may still be pending. Fence and flush them before
    # switching the owner row out of running and bypassing the event hooks.
    try:
        with execution_scope(handle):
            _fence(db.session())
            db.session.flush()
    except PolicyChanged:
        db.session.rollback()
        raise
    except LeaseLost:
        db.session.rollback()
        return False
    with control_transaction():
        task = db.session.execute(select(BackgroundTask).where(*_owner_where(handle, now))).scalar_one_or_none()
        if not task:
            db.session.rollback()
            return False
        failures = task.attempts + (1 if error else 0)
        retry = bool(error) and retryable and failures < 3 and (not task.deadline_at or task.deadline_at > now)
        from backend.services.inbox_refresh import collection_interval_seconds
        interval = collection_interval_seconds()
        error_code = getattr(error, 'error_code', '') or getattr(getattr(error, 'result', None), 'error_code', '')
        values = dict(state='pending' if retry else 'failed' if error else 'done', attempts=failures,
            phase='retry' if retry else 'complete', result=result or {}, error=str(error or '')[:500],
            error_code=error_code, lease_until=None, token=None, worker_id=None, updated_at=now,
            checked_at=now, next_run_at=now + timedelta(seconds=interval), finished_at=None if retry else now,
            available_at=now + timedelta(seconds=min(900, 30 * 2 ** failures)))
        changed = db.session.execute(update(BackgroundTask).where(*_owner_where(handle, now)).values(**values))
        if changed.rowcount:
            _release_source(handle)
        db.session.commit()
    return bool(changed.rowcount)


def require_verification(source_id, url, origin, error='', request_payload=None):
    handle = current_execution()
    if not handle:
        raise RuntimeError('Verification requires a durable task')
    # Serialize check/create across body/list tasks sharing the same source.
    # This lock lives only for this short transaction, never for human input.
    lock_key, stamp = 'verification:' + str(source_id), datetime.utcnow()
    insert_if_missing(db.session, RuntimeLease, dict(key=lock_key, token='', owner_id='',
        expires_at=stamp, updated_at=stamp), 'key')
    db.session.execute(update(RuntimeLease).where(RuntimeLease.key == lock_key).values(updated_at=stamp))
    row = VerificationSession.query.filter(VerificationSession.source_id == str(source_id),
        VerificationSession.status.in_(('required', 'opening', 'active'))).first()
    if row is None:
        row = VerificationSession(id=uuid4().hex, source_id=str(source_id), origin=origin, url=url,
                                  task_id=handle['id'], status='required', error_code='needs_manual',
                                  error_message=str(error or '官网需要管理员完成访问验证'),
                                  request_payload=request_payload)
        db.session.add(row)
    else:
        row.task_id = handle['id']
        row.request_payload = request_payload or row.request_payload
    db.session.flush()
    insert_if_missing(db.session, VerificationWaiter, dict(task_id=handle['id'], session_id=row.id), 'task_id')
    db.session.execute(update(VerificationWaiter).where(VerificationWaiter.task_id == handle['id']).values(session_id=row.id))
    db.session.commit()
    data = dict(handle.get('checkpoint') or {})
    data['verification_id'] = row.id
    defer(capability='browser', phase='verification', state='waiting',
          checkpoint=data, reason='官网需要管理员完成访问验证', error_code='needs_manual')


def resume_verification(source_id):
    ids = select(VerificationWaiter.task_id).join(VerificationSession,
        VerificationWaiter.session_id == VerificationSession.id).where(
            VerificationSession.source_id == str(source_id), VerificationSession.status == 'verified')
    now = datetime.utcnow()
    changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.id.in_(ids),
        BackgroundTask.state == 'waiting', BackgroundTask.phase == 'verification').values(
            state='pending', capability='browser', phase='render', available_at=now, updated_at=now,
            generation=BackgroundTask.generation + 1,
            deadline_at=now + timedelta(hours=2), error='', error_code=''))
    db.session.commit()
    return changed.rowcount


def invalidate_source(department_id, policy_version=None):
    """Call in the same transaction as administrator source configuration edits."""
    now = datetime.utcnow()
    version = str(policy_version or uuid4().hex)
    identities = (f'collect:{department_id}', f'source_health:{department_id}')
    changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.identity.in_(identities)).values(
        state='pending', phase='fetch', capability='http', token=None, worker_id=None, lease_until=None,
        generation=BackgroundTask.generation + 1, policy_version=version, checkpoint={}, attempts=0,
        available_at=now, queued_at=now, updated_at=now, error='', error_code='',
        deadline_at=now + timedelta(hours=2)))
    db.session.execute(delete(RuntimeLease).where(RuntimeLease.key == f'source:{department_id}'))
    return changed.rowcount


def restart_configuration(handle):
    now = datetime.utcnow()
    with control_transaction():
        changed = db.session.execute(update(BackgroundTask).where(*_owner_where(handle, now)).values(
            state='pending', phase='fetch', capability=default_capability(handle['kind']), checkpoint={},
            generation=BackgroundTask.generation + 1, token=None, worker_id=None, lease_until=None,
            available_at=now, updated_at=now, error='', error_code=''))
        if changed.rowcount:
            _release_source(handle)
        db.session.commit()
    return bool(changed.rowcount)


def task_status(kind, key):
    task = BackgroundTask.query.filter_by(identity=f'{kind}:{key}').first()
    if not task:
        return {'status': 'none'}
    return {'id': task.id, 'session_id': str(task.id),
        'status': {'done': 'completed', 'failed': 'failed', 'waiting': 'waiting'}.get(task.state, 'running'),
        'state': task.state, 'progress': 100 if task.state == 'done' else 0,
        'phase': task.phase, 'current_phase': task.phase, 'progress_pct': 100 if task.state == 'done' else 0,
        'error': task.error, 'error_code': task.error_code,
        'message': task.error or ('等待同步' if task.state == 'pending' else ''),
        'result': task.result or {}, 'found_departments': (task.result or {}).get('departments', []),
        **(task.result or {})}
