"""Short database reservations shared by every execution process.

Network work never runs in these transactions. Expiring permits recover after a
crash, while conditional renew/release cannot affect a replacement owner.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import delete, select, update, func, or_
from sqlalchemy.orm import Session

from backend.database.db import db
from backend.database.models import RuntimeLease, OriginBudget, OriginPermit, WorkerHeartbeat


def insert_if_missing(session, model, values, key):
    dialect = session.get_bind().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('The runtime supports SQLite and PostgreSQL only')
    return session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=[key]))


@contextmanager
def runtime_session():
    with Session(db.engine, info={'runtime_control': True}) as session:
        with session.begin():
            yield session


def acquire_in_transaction(session, key, token, owner_id, seconds=120, now=None):
    now = now or datetime.utcnow()
    insert_if_missing(session, RuntimeLease, dict(key=key, token='', owner_id='',
                                                expires_at=now, updated_at=now), 'key')
    result = session.execute(update(RuntimeLease).where(RuntimeLease.key == key,
        or_(RuntimeLease.expires_at <= now, RuntimeLease.token == token)).values(
            token=token, owner_id=owner_id, expires_at=now + timedelta(seconds=seconds), updated_at=now))
    return bool(result.rowcount)


def acquire(key, owner_id, seconds=120, token=None):
    token = token or uuid4().hex
    with runtime_session() as session:
        if not acquire_in_transaction(session, key, token, owner_id, seconds):
            return None
    return {'key': key, 'token': token}


def renew(handle, seconds=120):
    now = datetime.utcnow()
    with runtime_session() as session:
        return bool(session.execute(update(RuntimeLease).where(RuntimeLease.key == handle['key'],
            RuntimeLease.token == handle['token'], RuntimeLease.expires_at > now).values(
                expires_at=now + timedelta(seconds=seconds), updated_at=now)).rowcount)


def release(handle):
    with runtime_session() as session:
        session.execute(delete(RuntimeLease).where(RuntimeLease.key == handle['key'],
                                                   RuntimeLease.token == handle['token']))


def origin_key(url):
    # HTTP to HTTPS redirects on one official host must share a budget.
    return (urlsplit(url).hostname or '').lower().rstrip('.')


def reserve_origin(url, owner_id, ttl=120, token=None, interval=1, concurrency=1):
    key, now = origin_key(url), datetime.utcnow()
    if not key:
        raise ValueError('An official host is required')
    token = token or uuid4().hex
    with runtime_session() as session:
        insert_if_missing(session, OriginBudget, dict(origin=key, next_start_at=now), 'origin')
        # UPDATE also serializes SQLite writers; PostgreSQL takes a row lock.
        session.execute(update(OriginBudget).where(OriginBudget.origin == key).values(origin=key))
        budget = session.get(OriginBudget, key)
        session.execute(delete(OriginPermit).where(OriginPermit.origin == key, OriginPermit.expires_at <= now))
        prior = session.get(OriginPermit, token)
        if prior and prior.origin == key:
            prior.expires_at = now + timedelta(seconds=ttl)
            return {'token': token, 'origin': key}, 0
        due = max(stamp for stamp in (budget.next_start_at, budget.cooldown_until, now) if stamp)
        active = session.scalar(select(func.count()).select_from(OriginPermit).where(OriginPermit.origin == key))
        if active >= concurrency:
            earliest = session.scalar(select(func.min(OriginPermit.expires_at)).where(OriginPermit.origin == key))
            return None, max(1, min(5, (earliest - now).total_seconds()))
        if due > now:
            return None, max(.05, (due - now).total_seconds())
        session.add(OriginPermit(token=token, origin=key, owner_id=owner_id,
                                 expires_at=now + timedelta(seconds=ttl)))
        budget.next_start_at = now + timedelta(seconds=interval)
        return {'token': token, 'origin': key}, 0


def release_origin(token):
    with runtime_session() as session:
        session.execute(delete(OriginPermit).where(OriginPermit.token == token))


def renew_origin(token, ttl=120):
    now = datetime.utcnow()
    with runtime_session() as session:
        return bool(session.execute(update(OriginPermit).where(OriginPermit.token == token,
            OriginPermit.expires_at > now).values(expires_at=now + timedelta(seconds=ttl))).rowcount)


def cool_origin(url, seconds):
    key, now = origin_key(url), datetime.utcnow()
    until = now + timedelta(seconds=max(1, min(86400, seconds)))
    with runtime_session() as session:
        insert_if_missing(session, OriginBudget, dict(origin=key, next_start_at=now), 'origin')
        session.execute(update(OriginBudget).where(OriginBudget.origin == key,
            or_(OriginBudget.cooldown_until.is_(None), OriginBudget.cooldown_until < until))
            .values(cooldown_until=until))


def worker_heartbeat(worker_id, roles, *, stopped=False):
    now = datetime.utcnow()
    with runtime_session() as session:
        insert_if_missing(session, WorkerHeartbeat, dict(worker_id=worker_id, roles=list(roles),
            started_at=now, heartbeat_at=now), 'worker_id')
        session.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id).values(
            roles=list(roles), heartbeat_at=now, stopped_at=now if stopped else None))


def prune_expired(now=None):
    now = now or datetime.utcnow()
    with runtime_session() as session:
        session.execute(delete(OriginPermit).where(OriginPermit.expires_at <= now))
        session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.heartbeat_at < now - timedelta(days=7)))
        session.execute(delete(RuntimeLease).where(RuntimeLease.expires_at < now - timedelta(days=1)))


def runtime_status(max_age_seconds=180):
    now = datetime.utcnow()
    with runtime_session() as session:
        rows = session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.stopped_at.is_(None),
            WorkerHeartbeat.heartbeat_at >= now - timedelta(seconds=max_age_seconds))).scalars().all()
        roles = sorted({role for row in rows for role in (row.roles or [])})
        return {'roles': roles, 'workers': len(rows), 'ready': all(role in roles for role in ('http', 'scheduler')),
                'browser_ready': 'browser' in roles, 'directory_ready': 'directory' in roles}
