"""Bind an official catalog identity exactly once, including concurrent subscriptions."""
import hashlib
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from backend.database.db import db
from backend.database.dialect import insert
from backend.database.models import School
from backend.database.school_registry_models import SchoolRegistryEntry
from backend.services.catalog import normalize_name


def registry_key(name):
    return hashlib.sha256(('school:' + normalize_name(name)).encode('utf-8')).hexdigest()


def ensure_school(name, url, *, submitted_by=None, origin='catalog'):
    """Return (school, created). Never merge schools merely sharing a hostname.

    The unique registry row serializes first binding on both supported databases.
    Existing rows keep their IDs and data. A historical name collision needs review.
    This function commits only its short registration transaction; it does no I/O.
    """
    canonical = normalize_name(name)
    if not canonical or len(canonical) > 200 or not isinstance(url, str) or len(url) > 1000:
        raise ValueError('学校名称或官网地址无效')
    key = registry_key(canonical)
    for attempt in range(4):
        try:
            _identity_lock()
            db.session.execute(insert(SchoolRegistryEntry).values(
                registry_key=key, canonical_name=canonical, root_url=url,
                aliases=sorted({name, canonical}), origin=origin,
                created_at=datetime.utcnow()).on_conflict_do_nothing(index_elements=['registry_key']))
            statement = select(SchoolRegistryEntry).where(SchoolRegistryEntry.registry_key == key)
            if db.engine.dialect.name == 'postgresql':
                statement = statement.with_for_update()
            entry = db.session.execute(statement).scalar_one()
            aliases = [row for row in SchoolRegistryEntry.query.all()
                       if row.school_id and canonical in _names(row)]
            if len(aliases) > 1:
                raise ValueError('学校别名存在冲突，请管理员先核对')
            if aliases and aliases[0].registry_key != entry.registry_key:
                if entry.school_id is not None:
                    raise ValueError('学校身份与别名存在冲突')
                db.session.delete(entry)
                entry = aliases[0]
            school = db.session.get(School, entry.school_id) if entry.school_id else None
            created = False
            if school is None:
                existing = [s for s in School.query.all() if normalize_name(s.name) == canonical]
                if len(existing) > 1:
                    raise ValueError('现有学校身份存在重复，请管理员先核对；原有数据已保留')
                if existing:
                    school = existing[0]
                    previous = SchoolRegistryEntry.query.filter_by(school_id=school.id).first()
                    if previous and previous.registry_key != entry.registry_key:
                        db.session.delete(entry)
                        entry = previous
                else:
                    school = School(name=canonical, url=url, submitted_by=submitted_by, subscriber_count=0)
                    db.session.add(school)
                    db.session.flush()
                    created = True
                entry.school_id = school.id
            entry.aliases = sorted(set(entry.aliases or []) | {name, canonical, school.name})
            school_id = school.id
            db.session.commit()
            return db.session.get(School, school_id), created
        except OperationalError as exc:
            db.session.rollback()
            if db.engine.dialect.name != 'sqlite' or 'locked' not in str(exc).lower() or attempt == 3:
                raise
        except Exception:
            db.session.rollback()
            raise


def _identity_lock():
    if db.engine.dialect.name == 'postgresql':
        # All instance identity/alias mutations share this transaction-scoped lock.
        db.session.execute(text('SELECT pg_advisory_xact_lock(1937202624)'))


def _names(entry):
    return {normalize_name(name) for name in [entry.canonical_name, *(entry.aliases or [])] if name}


def registered_school(name):
    canonical = normalize_name(name)
    matches = [entry for entry in SchoolRegistryEntry.query.all() if entry.school_id and canonical in _names(entry)]
    if len(matches) > 1:
        raise ValueError('学校别名存在冲突，请管理员先核对')
    return db.session.get(School, matches[0].school_id) if matches else None


def stable_registry_key(school):
    entry = SchoolRegistryEntry.query.filter_by(school_id=school.id).first()
    return entry.registry_key if entry else registry_key(school.name)


def rename_school(school, new_name):
    """Add an explicit administrator alias without replacing the stable key.

    Caller commits the complete school edit or rolls it back on error.
    """
    if not isinstance(new_name, str) or not new_name.strip() or len(new_name) > 200:
        raise ValueError('学校名称应为 1–200 个字符')
    canonical = normalize_name(new_name)
    _identity_lock()
    previous_key = registry_key(school.name)
    # This first write serializes SQLite aliases, like ensure_school's upsert.
    db.session.execute(insert(SchoolRegistryEntry).values(
        registry_key=previous_key, canonical_name=normalize_name(school.name), root_url=school.url,
        aliases=[school.name], origin='registered', created_at=datetime.utcnow())
        .on_conflict_do_nothing(index_elements=['registry_key']))
    existing = SchoolRegistryEntry.query.filter_by(school_id=school.id).first()
    placeholder = db.session.get(SchoolRegistryEntry, previous_key)
    if existing is None:
        if placeholder.school_id is not None and placeholder.school_id != school.id:
            raise ValueError('学校原名称已关联其他学校，请先核对')
        existing = placeholder
        existing.school_id = school.id
    elif placeholder.registry_key != existing.registry_key and placeholder.school_id is None:
        db.session.delete(placeholder)
    for other in SchoolRegistryEntry.query.all():
        if other.registry_key != existing.registry_key and canonical in _names(other):
            raise ValueError('该学校名称或别名已被其他学校使用')
    if any(other.id != school.id and normalize_name(other.name) == canonical for other in School.query.all()):
        raise ValueError('该学校名称已被使用')
    existing.aliases = sorted(set(existing.aliases or []) | {school.name, new_name, canonical})
    school.name = new_name.strip()
    return existing
