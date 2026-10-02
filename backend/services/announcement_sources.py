"""Observed publication memberships, independent of an article's first-seen column."""
from datetime import datetime, timezone

from sqlalchemy import or_, func, case
from backend.database.dialect import insert
from sqlalchemy.orm import joinedload

from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, Department, School


def record_source(announcement, department, article_url=None, observed_at=None):
    if announcement.id is None:
        db.session.flush()
    seen_at = observed_at or datetime.now(timezone.utc)
    values = dict(announcement_id=announcement.id, department_id=department.id,
                  list_url=department.list_url or '', article_url=article_url or announcement.url or '',
                  first_seen_at=seen_at, last_seen_at=seen_at)
    statement = insert(AnnouncementSource).values(**values)
    db.session.execute(statement.on_conflict_do_update(
        index_elements=['announcement_id', 'department_id'],
        set_={'list_url': statement.excluded.list_url, 'article_url': statement.excluded.article_url,
              'last_seen_at': case((AnnouncementSource.last_seen_at < statement.excluded.last_seen_at,
                                    statement.excluded.last_seen_at), else_=AnnouncementSource.last_seen_at)}))


def source_expression(school_ids=None, department_ids=None, group=None):
    allowed = db.select(Department.id).join(School, School.id == Department.school_id).where(School.enabled.is_(True))
    if school_ids is not None:
        allowed = allowed.where(Department.school_id.in_(school_ids))
    if department_ids is not None:
        allowed = allowed.where(Department.id.in_(department_ids))
    if group is not None:
        normalized = func.coalesce(func.nullif(func.trim(Department.group_name), ''), '归属待核实')
        allowed = allowed.where(normalized == group)
    observed = db.select(AnnouncementSource.announcement_id).where(
        AnnouncementSource.announcement_id == Announcement.id,
        AnnouncementSource.department_id.in_(allowed)).correlate(Announcement).exists()
    # The original column remains readable even for records inserted by older integrations.
    return or_(Announcement.department_id.in_(allowed), observed)


def memberships(announcement_ids=None):
    original = db.select(Announcement.id.label('announcement_id'), Announcement.department_id.label('department_id'))
    observed = db.select(AnnouncementSource.announcement_id, AnnouncementSource.department_id)
    if announcement_ids is not None:
        original = original.where(Announcement.id.in_(announcement_ids))
        observed = observed.where(AnnouncementSource.announcement_id.in_(announcement_ids))
    return original.union(observed).subquery()


def source_counts(school_id):
    refs = memberships()
    return dict(db.session.query(refs.c.department_id, func.count(refs.c.announcement_id))
                .join(Department, Department.id == refs.c.department_id)
                .filter(Department.school_id == school_id).group_by(refs.c.department_id).all())


def sources_for(announcement_ids):
    if not announcement_ids:
        return {}
    refs = memberships(announcement_ids)
    rows = (db.session.query(refs.c.announcement_id, Department)
            .join(Department, Department.id == refs.c.department_id)
            .join(School, School.id == Department.school_id).filter(School.enabled.is_(True))
            .options(joinedload(Department.school)).order_by(School.name, Department.id).all())
    result = {}
    for announcement_id, department in rows:
        result.setdefault(announcement_id, []).append(department)
    return result


def schools_for(query):
    ids = query.with_entities(Announcement.id).order_by(None).subquery()
    refs = memberships(db.select(ids.c.id))
    school_ids = db.select(Department.school_id).join(refs, refs.c.department_id == Department.id)
    return School.query.filter(School.enabled.is_(True), School.id.in_(school_ids)).order_by(School.name).all()


def preferred_source(announcement, sources, school_id=None, department_id=None):
    """Choose the source in the reader's current context, from actual memberships only."""
    return min(sources, key=lambda d: (d.id != department_id,
               d.school_id != school_id, d.id != announcement.department_id, d.id)) if sources else None


def school_memberships(school_ids):
    """One article can count once in each school, even if it appears in several columns."""
    refs = memberships()
    return (db.select(refs.c.announcement_id, Department.school_id)
            .join(Department, Department.id == refs.c.department_id)
            .where(Department.school_id.in_(school_ids)).distinct().subquery())


def preserve_shared_articles(department_ids):
    """Before removing sources, retain articles that still belong to another real source.

    The caller owns the transaction. Only the original source pointer is changed;
    article IDs, content and every user's reading state remain untouched.
    """
    if not department_ids:
        return
    rows = (db.session.query(Announcement, Department)
            .join(AnnouncementSource, AnnouncementSource.announcement_id == Announcement.id)
            .join(Department, Department.id == AnnouncementSource.department_id)
            .filter(Announcement.department_id.in_(department_ids),
                    ~Department.id.in_(department_ids))
            .order_by(Announcement.id, Department.id).all())
    handled = set()
    for announcement, replacement in rows:
        if announcement.id not in handled:
            announcement.department = replacement
            announcement.school = replacement.school
            handled.add(announcement.id)
    # The delete cascades must query the new ownership, not the old source pointer.
    db.session.flush()
