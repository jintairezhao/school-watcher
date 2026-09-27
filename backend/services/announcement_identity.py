"""Database-enforced article identity, shared by collection and portable imports.

An appearance in another column is a membership, never a second article. Hashes
of titles or bodies are deliberately not identities: different notices can have
the same title, and a listing has not observed the body at all.
"""
from datetime import datetime
import hashlib

from sqlalchemy import event, select

from backend.database.db import db
from backend.database.dialect import insert
from backend.database.models import Announcement
from backend.services.source_inventory import canonical_url


def article_identity(url):
    try:
        canonical = canonical_url(url) if url else ''
    except (ValueError, UnicodeError):
        canonical = ''
    if not canonical:
        return None, None
    return canonical, hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@event.listens_for(Announcement, 'before_insert')
@event.listens_for(Announcement, 'before_update')
def _set_identity(mapper, connection, target):
    target.canonical_url, target.url_key = article_identity(target.url)


def upsert_listing(department, title, url, published_at=None, **initial_values):
    """Return (article, created), leaving the caller's transaction uncommitted.

The unique constraint resolves races from separate sources and workers. Existing
article content and original publication metadata remain untouched by list scans.
"""
    canonical, key = article_identity(url)
    if not key:
        raise ValueError('通知缺少有效原文地址，无法建立可靠身份')
    from backend.services import tasks
    if hasattr(tasks, 'assert_owned'):
        tasks.assert_owned()
    values = dict(school_id=department.school_id, department_id=department.id,
                  title=title, url=url, canonical_url=canonical, url_key=key,
                  published_at=published_at, created_at=datetime.utcnow(),
                  content_bytes=0, content_error='', is_updated=False)
    # Import can restore historical creation time and missing metadata atomically.
    allowed = {'created_at', 'summary', 'content_hash', 'is_updated'}
    values.update({k: v for k, v in initial_values.items() if k in allowed})
    statement = insert(Announcement).values(**values).on_conflict_do_nothing(
        index_elements=['school_id', 'url_key']).returning(Announcement.id)
    ident = db.session.execute(statement).scalar_one_or_none()
    created = ident is not None
    article = (db.session.get(Announcement, ident) if created else
               db.session.execute(select(Announcement).where(
                   Announcement.school_id == department.school_id,
                   Announcement.url_key == key).with_for_update()).scalar_one())
    if article.canonical_url != canonical:
        raise ValueError('通知身份冲突，需要人工核对原文地址')
    return article, created
