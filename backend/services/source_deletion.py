"""Remove owned records and revoke writers in the caller's transaction."""
from sqlalchemy import or_, and_

from backend.database.db import db
from backend.database.models import (Announcement, AppConfig, BackgroundTask,
    RuntimeLease, ScrapeLog)
from backend.database.source_governance_models import SourceProposal


def revoke_work(department_ids, *, school_id=None):
    # Shared articles have already moved to a surviving source. Their independent
    # reading/analysis work is retained when only one of their columns is removed.
    articles = db.select(Announcement.id).where(Announcement.department_id.in_(department_ids))
    proposals = db.select(SourceProposal.id).where(
        SourceProposal.school_id == school_id if school_id is not None
        else SourceProposal.department_id.in_(department_ids))
    payload = BackgroundTask.payload
    conditions = [payload['department_id'].as_integer().in_(department_ids),
        payload['proposal_id'].as_integer().in_(proposals),
        payload['announcement_id'].as_integer().in_(articles),
        and_(payload['subject_kind'].as_string() == 'source', payload['subject_id'].as_integer().in_(department_ids)),
        and_(payload['subject_kind'].as_string() == 'article', payload['subject_id'].as_integer().in_(articles))]
    if school_id is not None:
        conditions.extend([payload['school_id'].as_integer() == school_id,
                           BackgroundTask.identity.in_((f'discover:{school_id}', f'scrape:{school_id}'))])
    owned = or_(*conditions)
    tokens = db.select(BackgroundTask.token).where(owned, BackgroundTask.token.is_not(None))
    db.session.execute(db.delete(RuntimeLease).where(RuntimeLease.token.in_(tokens)))
    # Deleting the owner row invalidates the queue's existing commit fence, so a
    # running worker cannot publish a late result or restart work for a reused ID.
    db.session.execute(db.delete(BackgroundTask).where(owned).execution_options(synchronize_session='fetch'))


def remove_school(school):
    from backend.services.announcement_sources import preserve_shared_articles
    department_ids = [d.id for d in school.departments]
    preserve_shared_articles(department_ids)
    revoke_work(department_ids, school_id=school.id)
    db.session.execute(db.delete(ScrapeLog).where(ScrapeLog.school_id == school.id))
    db.session.execute(db.delete(AppConfig).where(AppConfig.key == f'discovery_started_{school.id}'))
    db.session.delete(school)


def remove_department(department):
    from backend.services.announcement_sources import preserve_shared_articles
    preserve_shared_articles([department.id])
    revoke_work([department.id])
    db.session.delete(department)
