"""Pause/remove collection without deleting a source or anybody's history."""
from datetime import datetime
from sqlalchemy import select, update, delete, or_

from backend.database.db import db
from backend.database.models import Department, BackgroundTask, RuntimeLease
from backend.database.source_governance_models import SourceProposal
from backend.database.source_lifecycle_models import SourceLifecycle

STATES = ('active', 'disabled', 'removed')
SOURCE_WORK = ('collect', 'source_health', 'source_review', 'onboard')


class SourceInactiveError(ValueError):
    pass


def active_source_clause():
    return ~select(SourceLifecycle.source_id).where(
        SourceLifecycle.source_id == Department.id, SourceLifecycle.state != 'active').exists()


def states_for(source_ids, *, session=None):
    session = session if session is not None else db.session
    ids = {int(i) for i in source_ids if i is not None}
    result = dict.fromkeys(ids, 'active')
    if ids:
        with session.no_autoflush:
            result.update(session.execute(select(SourceLifecycle.source_id, SourceLifecycle.state).where(
                SourceLifecycle.source_id.in_(ids))).all())
    return result


def ensure_source_active(source_id, *, lock=False, session=None):
    session = session if session is not None else db.session
    with session.no_autoflush:
        if lock:
            # Both lifecycle changes and publishing workers acquire this lock
            # before task-row locks, closing the check/commit race.
            session.execute(update(Department).where(Department.id == source_id).values(id=Department.id)
                .execution_options(synchronize_session=False))
        state = states_for([source_id], session=session).get(source_id, 'active')
    if state != 'active':
        raise SourceInactiveError('来源已停用或移除，请先恢复来源')


def task_source_ids(kind, payload, *, session=None):
    if kind not in SOURCE_WORK:
        return set()
    session = session if session is not None else db.session
    payload = payload or {}
    ids = {payload[k] for k in ('department_id', 'repair_department_id') if type(payload.get(k)) is int}
    if kind == 'source_review' and payload.get('proposal_id'):
        with session.no_autoflush:
            ident = session.execute(select(SourceProposal.department_id).where(
                SourceProposal.id == payload['proposal_id'])).scalar_one_or_none()
        if ident:
            ids.add(ident)
    return ids


def ensure_task_sources_active(kind, payload, *, lock=False, session=None):
    for source_id in sorted(task_source_ids(kind, payload, session=session)):
        ensure_source_active(source_id, lock=lock, session=session)


def task_sources_active(kind, payload, *, session=None):
    try:
        ensure_task_sources_active(kind, payload, session=session)
        return True
    except SourceInactiveError:
        return False


def park_task(task, *, session=None):
    session = session if session is not None else db.session
    if task.token:
        session.execute(delete(RuntimeLease).where(RuntimeLease.token == task.token))
    session.execute(update(BackgroundTask).where(BackgroundTask.id == task.id).values(
        state='failed', generation=BackgroundTask.generation + 1, token=None, worker_id=None,
        lease_until=None, next_run_at=None, error_code='source_inactive',
        error='来源已停用或移除，历史信息仍保留', finished_at=datetime.utcnow(), updated_at=datetime.utcnow())
        .execution_options(synchronize_session='fetch'))


def set_source_state(department, state, *, session=None):
    """Caller commits state, task revocation and structure revision together."""
    if state not in STATES or department.kind != 'column':
        raise ValueError('请选择有效的信息来源状态')
    session = session if session is not None else db.session
    with session.no_autoflush:
        session.execute(update(Department).where(Department.id == department.id).values(id=Department.id)
            .execution_options(synchronize_session=False))
        row = session.execute(select(SourceLifecycle).where(SourceLifecycle.source_id == department.id)
            .execution_options(populate_existing=True)).scalar_one_or_none()
    if (row.state if row else 'active') == state:
        return False
    if row is None:
        row = SourceLifecycle(source_id=department.id, school_id=department.school_id, state=state)
        session.add(row)
    else:
        row.state = state
        row.school_id = department.school_id
    row.updated_at = datetime.utcnow()
    if state != 'active':
        proposals = select(SourceProposal.id).where(SourceProposal.department_id == department.id)
        payload = BackgroundTask.payload
        jobs = session.execute(select(BackgroundTask).where(BackgroundTask.kind.in_(SOURCE_WORK),
            BackgroundTask.state.in_(('pending', 'running', 'waiting')), or_(
            payload['department_id'].as_integer() == department.id,
            payload['repair_department_id'].as_integer() == department.id,
            payload['proposal_id'].as_integer().in_(proposals)))).scalars().all()
        for job in jobs:
            park_task(job, session=session)
    return True
