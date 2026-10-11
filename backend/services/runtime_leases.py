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


def reserve_origin(url, owner_id, ttl=120, token=None, interval=None, concurrency=None,
                   transport='http', global_token=None):
    from backend.services.discovery_policy import get_policy
    policy = get_policy()
    interval = policy['fetch_host_interval_seconds'] if interval is None else interval
    concurrency = policy['fetch_host_concurrency'] if concurrency is None else concurrency
    key, now = origin_key(url), datetime.utcnow()
    if not key:
        raise ValueError('An official host is required')
    token = token or uuid4().hex
    global_token = global_token or token
    with runtime_session() as session:
        # Every process and transport takes this short database lock. Browser
        # redirects share their execution's global slot while retaining host permits.
        insert_if_missing(session, OriginBudget, dict(origin='__fetch_global__', next_start_at=now), 'origin')
        session.execute(update(OriginBudget).where(OriginBudget.origin == '__fetch_global__').values(next_start_at=now))
        insert_if_missing(session, OriginBudget, dict(origin=key, next_start_at=now), 'origin')
        # UPDATE also serializes SQLite writers; PostgreSQL takes a row lock.
        session.execute(update(OriginBudget).where(OriginBudget.origin == key).values(origin=key))
        budget = session.get(OriginBudget, key)
        session.execute(delete(OriginPermit).where(OriginPermit.origin == key, OriginPermit.expires_at <= now))
        prior = session.get(OriginPermit, token)
        if prior and prior.origin == key:
            prior.expires_at = now + timedelta(seconds=ttl)
            session.execute(update(RuntimeLease).where(RuntimeLease.token == global_token,
                RuntimeLease.key.like('fetch:%')).values(expires_at=now + timedelta(seconds=ttl), updated_at=now))
            return {'token': token, 'origin': key}, 0
        due = max(stamp for stamp in (budget.next_start_at, budget.cooldown_until, now) if stamp)
        active = session.scalar(select(func.count()).select_from(OriginPermit).where(OriginPermit.origin == key))
        if active >= concurrency:
            earliest = session.scalar(select(func.min(OriginPermit.expires_at)).where(OriginPermit.origin == key))
            return None, max(1, min(5, (earliest - now).total_seconds()))
        if due > now:
            return None, max(.05, (due - now).total_seconds())
        for category, maximum in [('global', policy['fetch_global_concurrency'])] + (
                [('browser', policy['browser_concurrency'])] if transport == 'browser' else []):
            existing = session.get(RuntimeLease, f'fetch:{category}:{global_token}')
            active_count = session.scalar(select(func.count()).select_from(RuntimeLease).where(
                RuntimeLease.key.like(f'fetch:{category}:%'), RuntimeLease.expires_at > now))
            if not (existing and existing.expires_at > now) and active_count >= max(1, int(maximum)):
                return None, 1
        for category in ['global', 'permit'] + (['browser'] if transport == 'browser' else []):
            lease_key = f'fetch:{category}:{token if category == "permit" else global_token}'
            acquire_in_transaction(session, lease_key, global_token, global_token, ttl, now)
        session.add(OriginPermit(token=token, origin=key, owner_id=owner_id,
                                 expires_at=now + timedelta(seconds=ttl)))
        budget.next_start_at = now + timedelta(seconds=interval)
        return {'token': token, 'origin': key}, 0


def release_origin(token):
    with runtime_session() as session:
        session.execute(update(OriginBudget).where(OriginBudget.origin == '__fetch_global__').values(next_start_at=datetime.utcnow()))
        mapping = session.get(RuntimeLease, f'fetch:permit:{token}')
        global_token = mapping.token if mapping else token
        session.execute(delete(OriginPermit).where(OriginPermit.token == token))
        session.execute(delete(RuntimeLease).where(RuntimeLease.key == f'fetch:permit:{token}'))
        remaining = session.scalar(select(func.count()).select_from(RuntimeLease).where(
            RuntimeLease.key.like('fetch:permit:%'), RuntimeLease.token == global_token,
            RuntimeLease.expires_at > datetime.utcnow()))
        if not remaining:
            session.execute(delete(RuntimeLease).where(RuntimeLease.token == global_token,
                RuntimeLease.key.in_([f'fetch:global:{global_token}', f'fetch:browser:{global_token}'])))


def renew_origin(token, ttl=120):
    now = datetime.utcnow()
    with runtime_session() as session:
        renewed = bool(session.execute(update(OriginPermit).where(OriginPermit.token == token,
            OriginPermit.expires_at > now).values(expires_at=now + timedelta(seconds=ttl))).rowcount)
        mapping = session.get(RuntimeLease, f'fetch:permit:{token}')
        if renewed and mapping:
            session.execute(update(RuntimeLease).where(RuntimeLease.token == mapping.token,
                RuntimeLease.key.like('fetch:%'), RuntimeLease.expires_at > now).values(
                    expires_at=now + timedelta(seconds=ttl), updated_at=now))
        return renewed


def cool_origin(url, seconds):
    key, now = origin_key(url), datetime.utcnow()
    until = now + timedelta(seconds=max(1, seconds))
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
