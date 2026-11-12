"""Preview-bound removal of old notice records; never remove files by path.

The preview is read-only. A signed, short-lived token binds the actor, calendar
cutoff, ID high-water mark and complete matching snapshot (including protection).
Execution rechecks that snapshot while holding the maintenance/writer locks.
"""
from datetime import date, datetime
from contextlib import contextmanager
import hashlib
import json
import re

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementMerge, AnnouncementResource,
    AnnouncementSource, BackgroundTask, RuntimeLease, ScrapeLog, School,
    UserAnnouncementState, UserRead, VerificationSession, VerificationWaiter)
from backend.ai.summary_models import AnnouncementSummary
from backend.database.student_information_models import StudentAssessment
from backend.services.saved_materials import pin_expression

TOKEN_TTL = 10 * 60
ACTIVE = ('pending', 'running', 'waiting')
_SALT = 'school-watcher:notice-cleanup:v1'


class NoticeCleanupError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _filters(filters):
    if not isinstance(filters, dict) or set(filters) - {'school_id', 'before'}:
        raise NoticeCleanupError('请选择学校和通知发布日期范围')
    before, school = filters.get('before'), filters.get('school_id')
    if not isinstance(before, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', before):
        raise NoticeCleanupError('截止日期必须为 YYYY-MM-DD')
    try:
        day = date.fromisoformat(before)
    except ValueError:
        raise NoticeCleanupError('截止日期无效') from None
    if school is not None and (type(school) is not int or school < 1):
        raise NoticeCleanupError('学校编号必须为正整数')
    return {'school_id': school, 'before': day.isoformat()}


def _actor(actor_id):
    if type(actor_id) is not int or actor_id < 1:
        raise NoticeCleanupError('操作用户无效')
    return actor_id


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_SALT)


def _scope(filters, max_id):
    # Datetimes in the main database are normalized for storage. Comparing the
    # stored midnight preserves that calendar day, without applying UI timezones.
    conditions = [Announcement.id <= max_id, Announcement.published_at.is_not(None),
        Announcement.published_at < datetime.combine(date.fromisoformat(filters['before']), datetime.min.time())]
    if filters['school_id'] is not None:
        conditions.append(Announcement.school_id == filters['school_id'])
    return and_(*conditions)


def _active_state(column):
    return or_(column.in_(ACTIVE), column.startswith('waiting'))


def _ident(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[1-9]\d*', value):
        return int(value)
    return None


def _references(value, summary_ids, *, allow_ids=False):
    """Find typed article references; department/source IDs are not article IDs."""
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(_references(item, summary_ids, allow_ids=allow_ids))
    elif isinstance(value, dict):
        for key in ('announcement_id',):
            ident = _ident(value.get(key))
            if ident:
                result.add(ident)
        if value.get('subject_kind') in ('article', 'announcement'):
            ident = _ident(value.get('subject_id'))
            if ident:
                result.add(ident)
        for key in ('announcement_ids', 'ids') if allow_ids else ('announcement_ids',):
            if isinstance(value.get(key), list):
                result.update(i for v in value[key] if (i := _ident(v)))
        ident = _ident(value.get('summary_id'))
        if ident in summary_ids:
            result.add(summary_ids[ident])
        if isinstance(value.get('summary_ids'), list):
            result.update(summary_ids[i] for v in value['summary_ids'] if (i := _ident(v)) in summary_ids)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                result.update(_references(nested, summary_ids, allow_ids=allow_ids))
    return result


def _task_references(task, summary_ids):
    result = set()
    for field in ('payload', 'result', 'checkpoint'):
        result.update(_references(task[field], summary_ids, allow_ids=task['kind'] == 'summarize'))
    return result


def _snapshot(connection, filters, max_id):
    scope = _scope(filters, max_id)
    rows = connection.execute(select(Announcement.id, Announcement.school_id,
        Announcement.department_id, Announcement.title, Announcement.url,
        Announcement.canonical_url, Announcement.url_key, Announcement.created_at,
        Announcement.published_at, School.name.label('school'), pin_expression().label('pinned'))
        .join(School, School.id == Announcement.school_id).where(scope).order_by(Announcement.id)).mappings().all()
    ids = {row['id'] for row in rows}
    protected = {row['id'] for row in rows if row['pinned']}
    scope_ids = select(Announcement.id).where(scope)
    summary_ids = dict(connection.execute(select(AnnouncementSummary.id, AnnouncementSummary.announcement_id)
        .where(AnnouncementSummary.announcement_id.in_(scope_ids))).all())
    for model in (AnnouncementResource, AnnouncementSummary, StudentAssessment):
        protected.update(connection.execute(select(model.announcement_id).where(
            model.announcement_id.in_(scope_ids), _active_state(model.state))).scalars())
    protected.update(connection.execute(select(AnnouncementSummary.announcement_id).join(
        BackgroundTask, BackgroundTask.id == AnnouncementSummary.task_id).where(
            AnnouncementSummary.announcement_id.in_(scope_ids), _active_state(BackgroundTask.state))).scalars())
    active = connection.execute(select(BackgroundTask.__table__).where(
        _active_state(BackgroundTask.state))).mappings()
    for task in active:
        protected.update(_task_references(task, summary_ids) & ids)
    digest = hashlib.sha256()
    for row in rows:
        identity = [row[key].isoformat() if isinstance(row[key], datetime) else row[key]
            for key in ('id', 'school_id', 'department_id', 'title', 'url', 'canonical_url',
                        'url_key', 'created_at', 'published_at')]
        identity.append(row['id'] in protected)
        digest.update(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf8'))
        digest.update(b'\n')
    unknown = select(func.count()).select_from(Announcement).where(
        Announcement.id <= max_id, Announcement.published_at.is_(None))
    if filters['school_id'] is not None:
        unknown = unknown.where(Announcement.school_id == filters['school_id'])
    return rows, protected, digest.hexdigest(), connection.scalar(unknown), summary_ids


def _result(filters, rows, protected, unknown):
    return {**filters, 'matched_count': len(rows), 'deletable_count': len(rows) - len(protected),
        'protected_count': len(protected), 'unknown_date_count': unknown}


def preview(filters, actor_id):
    """Read-only preview. Unknown dates and every user's pinned notice survive."""
    filters, actor_id = _filters(filters), _actor(actor_id)
    with db.engine.connect() as connection:
        if filters['school_id'] is not None and not connection.scalar(
                select(School.id).where(School.id == filters['school_id'])):
            raise NoticeCleanupError('学校不存在')
        max_id = connection.scalar(select(func.max(Announcement.id))) or 0
        rows, protected, digest, unknown, _ = _snapshot(connection, filters, max_id)
    token = _serializer().dumps({'version': 1, 'actor': actor_id, 'filters': filters,
        'max_id': max_id, 'digest': digest})
    sample = [{'id': row['id'], 'title': row['title'], 'school': row['school'],
        'published_at': row['published_at'].isoformat()} for row in rows if row['id'] not in protected][:5]
    return {**_result(filters, rows, protected, unknown), 'sample': sample, 'preview_token': token}


def _remove_related(connection, ids, summary_ids):
    """Only database-owned rows are removed. Resource filename is never a path."""
    if not ids:
        return
    removed = set(ids)
    # Historical duplicate IDs can themselves point at older aliases. Remove
    # the entire alias chain so no retained mapping leads to a missing survivor.
    aliases = set(removed)
    incoming = {}
    for old, survivor in connection.execute(select(AnnouncementMerge.old_id, AnnouncementMerge.survivor_id)):
        incoming.setdefault(survivor, []).append(old)
    pending = list(aliases)
    while pending:
        for old in incoming.get(pending.pop(), ()):
            if old not in aliases:
                aliases.add(old); pending.append(old)
    finished = connection.execute(select(BackgroundTask.__table__).where(
        ~_active_state(BackgroundTask.state))).mappings().all()
    owned = [task for task in finished if _task_references(task, summary_ids) & aliases]
    for start in range(0, len(owned), 400):
        batch = owned[start:start + 400]
        task_ids = [task['id'] for task in batch]
        tokens = [task['token'] for task in batch if task['token']]
        connection.execute(VerificationWaiter.__table__.delete().where(VerificationWaiter.task_id.in_(task_ids)))
        for model in (VerificationSession, ScrapeLog, AnnouncementSummary):
            connection.execute(model.__table__.update().where(model.task_id.in_(task_ids)).values(task_id=None))
        if tokens:
            connection.execute(RuntimeLease.__table__.delete().where(RuntimeLease.token.in_(tokens)))
        connection.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id.in_(task_ids)))
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        for model in (AnnouncementResource, UserRead, UserAnnouncementState, AnnouncementSource,
                      AnnouncementSummary, StudentAssessment):
            connection.execute(model.__table__.delete().where(model.announcement_id.in_(batch)))
        connection.execute(Announcement.__table__.delete().where(Announcement.id.in_(batch)))
    alias_ids = list(aliases)
    for start in range(0, len(alias_ids), 400):
        batch = alias_ids[start:start + 400]
        connection.execute(AnnouncementMerge.__table__.delete().where(or_(
            AnnouncementMerge.old_id.in_(batch), AnnouncementMerge.survivor_id.in_(batch))))


@contextmanager
def _write_connection(filters, max_id):
    """Use a short local writer wait, without changing pooled connection policy."""
    with db.engine.connect() as connection:
        previous_timeout = None
        try:
            if connection.dialect.name == 'sqlite':
                previous_timeout = int(connection.exec_driver_sql('PRAGMA busy_timeout').scalar())
                connection.exec_driver_sql('PRAGMA busy_timeout=250')
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                connection.begin()
                if connection.dialect.name == 'postgresql':
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '500ms'")
                # Favorites acquire the same Announcement parent lock first;
                # PG FK key-share locks serialize new related row insertions.
                connection.execute(select(Announcement.id).where(_scope(filters, max_id))
                    .order_by(Announcement.id).with_for_update()).all()
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if previous_timeout is not None:
                try:
                    connection.exec_driver_sql(f'PRAGMA busy_timeout={previous_timeout}')
                except SQLAlchemyError:
                    connection.invalidate()


def delete_preview(preview_token, actor_id):
    """Delete exactly the unchanged preview, serialized against favorites/writers."""
    actor_id = _actor(actor_id)
    if not isinstance(preview_token, str) or not preview_token or len(preview_token) > 8192:
        raise NoticeCleanupError('请先预览待清理通知')
    try:
        data = _serializer().loads(preview_token, max_age=TOKEN_TTL)
    except SignatureExpired:
        raise NoticeCleanupError('清理预览已过期，请重新预览', 409) from None
    except BadSignature:
        raise NoticeCleanupError('清理预览无效，请重新预览') from None
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('actor') != actor_id:
        raise NoticeCleanupError('清理预览不属于当前用户')
    filters = _filters(data.get('filters'))
    max_id, digest = data.get('max_id'), data.get('digest')
    if type(max_id) is not int or max_id < 0 or not isinstance(digest, str):
        raise NoticeCleanupError('清理预览无效，请重新预览')
    from backend.services.database_space import DatabaseSpaceError, storage_maintenance_lock
    try:
        with storage_maintenance_lock():
            with _write_connection(filters, max_id) as connection:
                rows, protected, current, unknown, summaries = _snapshot(connection, filters, max_id)
                if current != digest:
                    raise NoticeCleanupError('通知或保护状态已变化，请重新预览', 409)
                ids = [row['id'] for row in rows if row['id'] not in protected]
                _remove_related(connection, ids, summaries)
        db.session.expire_all()
        return {**_result(filters, rows, protected, unknown), 'deleted_count': len(ids),
            'success': True}
    except DatabaseSpaceError as error:
        raise NoticeCleanupError(str(error), error.status_code) from None
    except SQLAlchemyError:
        current_app.logger.exception('Notice cleanup transaction rolled back')
        raise NoticeCleanupError('清理未完成，所有通知已保留，请稍后重试', 409) from None
