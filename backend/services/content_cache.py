"""Article text is shared; retention pins are derived from each user's saved state."""
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import current_app
from sqlalchemy import exists

from backend.database.db import db
from backend.database.models import Announcement, Department, UserAnnouncementState, BackgroundTask
from backend.scraper.article_observation import navigation_only_body


def _discard_navigation_cache(ann):
    if not navigation_only_body(ann.content_html, ann.url):
        return
    ann.content_html = ann.content_text = ann.content_error = ''
    ann.content_bytes = 0
    ann.content_cached_at = ann.content_accessed_at = None
    ann.resources_scanned_at = None
    ann.content_hash = None


def status(ann):
    if ann.content_cached_at or ann.content_html:
        return 'saved'
    job = BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
    if job and job.state in ('pending', 'running', 'waiting'):
        return 'loading'
    return 'failed' if ann.content_error or (job and job.state == 'failed') else 'unloaded'


def progress_message(ann):
    job=BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
    if not job:return ''
    if job.state=='waiting':return job.error or '正文任务已保存，等待采集恢复'
    if job.state=='pending':
        if job.phase=='retry':return '官网读取正在自动重试，完成后会自动显示'
        if job.phase in ('origin_wait','global_wait','browser_wait'):return '正在等待官网采集资源，完成后会自动显示'
        return '正文已加入读取队列，完成后会自动显示'
    if job.state=='running':return '正在读取官网正文，完成后会自动显示'
    return ''


def request_content(ann):
    _discard_navigation_cache(ann)
    if ann.content_cached_at or ann.content_html:
        from backend.services.publication_dates import reconcile_cached_date
        corrected_date = reconcile_cached_date(ann)
        # Coalesce hot reads; viewing a cached article need not write every time.
        now = datetime.utcnow()
        if not ann.content_accessed_at or now - ann.content_accessed_at > timedelta(hours=1):
            ann.content_accessed_at = now
            db.session.commit()
        elif corrected_date:
            db.session.commit()
        return None
    from backend.services.tasks import enqueue
    return enqueue('content', ann.id, {'announcement_id': ann.id})


def fetch_content(ann_id):
    from backend.scraper.engine import _fetch_html, _extract_text
    from backend.scraper.sanitizer import sanitize_html
    from backend.scraper.change_detector import compute_hash
    from backend.scraper.article_resources import has_public_body_resource
    ann = db.session.get(Announcement, ann_id)
    if not ann:
        return {'removed': True}
    _discard_navigation_cache(ann)
    if ann.content_cached_at:
        from backend.services.publication_dates import reconcile_cached_date
        reconcile_cached_date(ann)
        db.session.commit()
        return {'cached': True}
    url, title = ann.url, ann.title
    from backend.services.announcement_sources import sources_for
    sources = sources_for([ann_id]).get(ann_id, [])
    configured = [d.content_selector for d in sources if d.content_selector]
    # CMS bodies can sit inside a noninteractive form. Select the actual body
    # before a broad configured .main/.content wrapper, whose form would be
    # removed together with its contents during HTML cleanup.
    selectors = ['.v_news_content', '#vsb_content', '#vsb_content_2', '.wp_articlecontent',
                 '.article-content', '.article_content', '.TRS_Editor'] + configured + ['article']
    db.session.commit()  # No database transaction across the outbound request.
    try:
        html = _fetch_html(url, raise_fetch_errors=True, purpose='article', source_id='content:' + str(ann_id),
                           readiness_selector=', '.join(dict.fromkeys(configured)),
                           policy={'expected_article_title':title})
        final_url = getattr(html, 'final_url', url)
        attachment = getattr(getattr(html, 'result', None), 'attachment', {})
        if attachment.get('verified'):
            from html import escape
            # A link to the actual read file is the cached attachment view. Its
            # contents are not invented text and aren't executed in the app.
            html = '<article><p><a href="' + escape(final_url, quote=True) + '">' + escape(
                attachment.get('filename') or '查看官网 PDF 附件') + '</a></p></article>'
        elif not html:
            raise ValueError('暂时无法读取官网正文，请打开原文查看')
        # Publication evidence is independent of body extraction. A missing body
        # rule must not roll back an actual, title-matched publication date.
        ann = db.session.get(Announcement,ann_id)
        if not ann or ann.url != url or ann.title != title:
            return {'stale': True}
        from backend.services.publication_dates import apply_article_date
        apply_article_date(ann,html,final_url)
        db.session.commit()
        soup = BeautifulSoup(html, 'lxml')
        from backend.scraper.article_observation import title_bound_body
        content = title_bound_body(soup,final_url,title)
        for selector in dict.fromkeys(selectors) if content is None else ():
            try:
                candidate = soup.select_one(selector)
            except Exception:
                continue
            if (candidate is not None and not navigation_only_body(str(candidate), final_url)
                    and (candidate.get_text(' ', strip=True) or has_public_body_resource([candidate], final_url))):
                content = candidate
                break
        if content is None:
            raise ValueError('正文仍在适配中，可先打开官网原文')
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
        if navigation_only_body(clean, final_url) or not (text or has_public_body_resource(
                [BeautifulSoup(clean, 'lxml')], final_url)):
            raise ValueError('尚未读取到通知正文，可重试或打开官网原文')
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
        db.session.commit()
        from backend.services.saved_materials import queue_materials
        queue_materials(ann)
        prune_content()
        return {'saved': True, 'bytes': size}
    except Exception as exc:
        db.session.rollback()
        ann = db.session.get(Announcement, ann_id)
        if ann:
            from backend.scraper.acquisition import FetchFailure
            if (isinstance(exc,FetchFailure) and exc.outcome=='needs_adapter'
                    and 200 <= exc.result.status < 300 and exc.result.html
                    and ann.url==url and ann.title==title):
                from backend.services.publication_dates import apply_article_date
                apply_article_date(ann,exc.result.html,exc.result.final_url)
            ann.content_error = str(exc)[:300]
            db.session.commit()
        raise


def prune_content(*, all_cache=False):
    from backend.services.storage_policy import policy, retention_cutoff, capacity_bytes
    days = policy()['body_cache_days']
    cutoff = retention_cutoff(days, datetime.utcnow()) if days else None
    limit = capacity_bytes('body_cache_mb', 'BODY_CACHE_BYTES', 150 * 1048576)
    from backend.services.saved_materials import pin_expression, TRASH_DAYS
    from backend.database.models import AnnouncementResource
    pinned = pin_expression()
    trash_cutoff = datetime.utcnow() - timedelta(days=TRASH_DAYS)
    expired_trash = exists().where(UserAnnouncementState.announcement_id == Announcement.id,
        UserAnnouncementState.starred.is_(False), UserAnnouncementState.trashed_at <= trash_cutoff)
    resource_bytes = db.session.query(db.func.coalesce(db.func.sum(AnnouncementResource.byte_size), 0)).filter(
        AnnouncementResource.announcement_id == Announcement.id).correlate(Announcement).scalar_subquery()
    accessed = db.func.coalesce(Announcement.content_accessed_at, Announcement.content_cached_at, Announcement.created_at)
    rows = db.session.query(Announcement.id, (Announcement.content_bytes + resource_bytes).label('content_bytes'),
        accessed.label('accessed'), expired_trash.label('trash_expired')).filter(
        ~pinned, db.or_(Announcement.content_bytes > 0, Announcement.content_html != '',
                       Announcement.content_text != '', Announcement.content_cached_at.isnot(None),
                       exists().where(AnnouncementResource.announcement_id == Announcement.id))
    ).order_by(accessed, Announcement.id).all()
    total = sum(r.content_bytes for r in rows)
    expired = []
    for row in rows:
        if row.trash_expired or all_cache or (limit and total > limit) or (cutoff and (row.accessed is None or row.accessed < cutoff)):
            expired.append(row.id)
            total -= row.content_bytes
    evicted = 0
    for start in range(0, len(expired), 200):
        # Recheck pins at the write, in case a user saved the article since the scan.
        # State changes and resource commits take this same parent-row lock.
        # Recheck pins after locking, so restoring a favorite cannot race eviction.
        locked = db.session.query(Announcement.id).filter(Announcement.id.in_(expired[start:start + 200])).with_for_update().all()
        cleared = db.session.execute(db.update(Announcement).where(Announcement.id.in_([r.id for r in locked]), ~pinned).values(
            content_html='', content_text='', content_bytes=0, content_cached_at=None,
            content_accessed_at=None, content_error='', resources_scanned_at=None).returning(Announcement.id)).scalars().all()
        db.session.execute(db.delete(AnnouncementResource).where(AnnouncementResource.announcement_id.in_(cleared)))
        evicted += len(cleared)
    db.session.execute(db.update(UserAnnouncementState).where(UserAnnouncementState.starred.is_(False),
        UserAnnouncementState.trashed_at <= trash_cutoff).values(trashed_at=None))
    db.session.commit()
    return {'evicted': evicted, 'unpinned_bytes': total}
