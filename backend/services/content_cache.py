"""Article text is shared; retention pins are derived from each user's saved state."""
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import current_app
from sqlalchemy import exists

from backend.database.db import db
from backend.database.models import Announcement, Department, UserAnnouncementState, BackgroundTask


def status(ann):
    if ann.content_cached_at or ann.content_html:
        return 'saved'
    job = BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
    if job and job.state in ('pending', 'running'):
        return 'loading'
    return 'failed' if ann.content_error or (job and job.state == 'failed') else 'unloaded'


def request_content(ann):
    if ann.content_cached_at or ann.content_html:
        # Coalesce hot reads; viewing a cached article need not write every time.
        now = datetime.utcnow()
        if not ann.content_accessed_at or now - ann.content_accessed_at > timedelta(hours=1):
            ann.content_accessed_at = now
            db.session.commit()
        return None
    from backend.services.tasks import enqueue
    return enqueue('content', ann.id, {'announcement_id': ann.id})


def fetch_content(ann_id):
    from backend.scraper.engine import _fetch_html, _extract_text
    from backend.scraper.sanitizer import sanitize_html
    from backend.scraper.change_detector import compute_hash, parse_date
    ann = db.session.get(Announcement, ann_id)
    if not ann:
        return {'removed': True}
    if ann.content_cached_at:
        return {'cached': True}
    url, title = ann.url, ann.title
    from backend.services.announcement_sources import sources_for
    sources = sources_for([ann_id]).get(ann_id, [])
    selectors = [d.content_selector for d in sources if d.content_selector]
    selectors += ['.v_news_content', '#vsb_content', '#vsb_content_2', '.wp_articlecontent',
                  '.article-content', '.article_content', '.TRS_Editor', 'article']
    db.session.commit()  # No database transaction across the outbound request.
    try:
        html = _fetch_html(url, raise_fetch_errors=True, purpose='article', source_id='content:' + str(ann_id),
                           readiness_selector=', '.join(dict.fromkeys(selectors)))
        final_url = getattr(html, 'final_url', url)
        if not html:
            raise ValueError('暂时无法读取官网正文，请打开原文查看')
        soup = BeautifulSoup(html, 'lxml')
        content = None
        for selector in dict.fromkeys(selectors):
            try:
                candidate = soup.select_one(selector)
            except Exception:
                continue
            if candidate is not None and candidate.get_text(' ', strip=True):
                content = candidate
                break
        if content is None:
            raise ValueError('尚未识别到正文区域，请打开原文查看')
        for tag in content.select('script,style,iframe,form'):
            tag.decompose()
        for tag in content.find_all(['a', 'img']):
            for attr in ('href', 'src'):
                value = tag.get(attr)
                if value:
                    if value.startswith('data:'):
                        del tag[attr]
                    else:
                        tag[attr] = urljoin(final_url, value)
        clean = sanitize_html(str(content))
        text = _extract_text(clean)
        size = len(clean.encode()) + len(text.encode())
        if size > 2 * 1024 * 1024:
            raise ValueError('正文较大，请在官网阅读')
        ann = db.session.get(Announcement, ann_id)
        if not ann:
            return {'removed': True}
        ann.content_html, ann.content_text = clean, text
        ann.content_bytes = size
        ann.content_cached_at = ann.content_accessed_at = datetime.utcnow()
        ann.content_error = ''
        ann.content_hash = compute_hash(title, text)
        if not ann.published_at:
            meta = soup.select_one('meta[name="PubDate"],meta[name="pubdate"],meta[name="publishdate"],meta[property="article:published_time"]')
            if meta:
                ann.published_at = parse_date(meta.get('content', ''))
        db.session.commit()
        prune_content()
        return {'saved': True, 'bytes': size}
    except Exception as exc:
        db.session.rollback()
        ann = db.session.get(Announcement, ann_id)
        if ann:
            ann.content_error = str(exc)[:300]
            db.session.commit()
        raise


def prune_content(*, all_cache=False):
    from backend.services.storage_policy import policy, retention_cutoff, capacity_bytes
    days = policy()['body_cache_days']
    cutoff = retention_cutoff(days, datetime.utcnow()) if days else None
    limit = capacity_bytes('body_cache_mb', 'BODY_CACHE_BYTES', 150 * 1048576)
    pinned = exists().where(UserAnnouncementState.announcement_id == Announcement.id,
                            UserAnnouncementState.starred.is_(True))
    accessed = db.func.coalesce(Announcement.content_accessed_at, Announcement.content_cached_at, Announcement.created_at)
    rows = db.session.query(Announcement.id, Announcement.content_bytes, accessed.label('accessed')).filter(
        ~pinned, db.or_(Announcement.content_bytes > 0, Announcement.content_html != '',
                       Announcement.content_text != '', Announcement.content_cached_at.isnot(None))
    ).order_by(accessed, Announcement.id).all()
    total = sum(r.content_bytes for r in rows)
    expired = []
    for row in rows:
        if all_cache or (limit and total > limit) or (cutoff and (row.accessed is None or row.accessed < cutoff)):
            expired.append(row.id)
            total -= row.content_bytes
    evicted = 0
    for start in range(0, len(expired), 200):
        # Recheck pins at the write, in case a user saved the article since the scan.
        result = db.session.execute(db.update(Announcement).where(Announcement.id.in_(expired[start:start + 200]), ~pinned).values(
            content_html='', content_text='', content_bytes=0, content_cached_at=None,
            content_accessed_at=None, content_error=''))
        evicted += result.rowcount
    db.session.commit()
    return {'evicted': evicted, 'unpinned_bytes': total}
