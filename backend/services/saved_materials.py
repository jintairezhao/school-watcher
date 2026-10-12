"""Local favorite snapshots: body plus observed resources, with shared retention."""
from datetime import datetime, timedelta
from email.message import Message
import hashlib
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from flask import current_app
from sqlalchemy import exists, or_, select

from backend.database.db import db
from backend.database.dialect import insert
from backend.database.models import Announcement, AnnouncementResource, BackgroundTask, UserAnnouncementState

TRASH_DAYS = 30
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
FILES = re.compile(r'\.(?:pdf|docx?|xlsx?|pptx?|odt|ods|odp|txt|csv|zip|rar|7z|gz|png|jpe?g|gif|webp|avif|bmp)$', re.I)


def trash_expression(user_id, now=None):
    cutoff = (now or datetime.utcnow()) - timedelta(days=TRASH_DAYS)
    return exists().where(UserAnnouncementState.announcement_id == Announcement.id,
        UserAnnouncementState.user_id == user_id, UserAnnouncementState.starred.is_(False),
        UserAnnouncementState.trashed_at > cutoff)


def pin_expression(now=None):
    cutoff = (now or datetime.utcnow()) - timedelta(days=TRASH_DAYS)
    return exists().where(UserAnnouncementState.announcement_id == Announcement.id,
        or_(UserAnnouncementState.starred.is_(True), UserAnnouncementState.trashed_at > cutoff))


def is_pinned(ann_id):
    return bool(Announcement.query.filter(Announcement.id == ann_id, pin_expression()).first())


def user_favorite_state(ann_id, user_id):
    state = db.session.get(UserAnnouncementState, (user_id, ann_id))
    return state if state and (state.starred or state.trashed_at and state.trashed_at > datetime.utcnow() - timedelta(days=TRASH_DAYS)) else None


def readable_expression(user_id=None):
    from backend.services.announcement_sources import source_expression
    if user_id is None:
        return source_expression()
    from backend.services.inbox import state_expression
    return or_(source_expression(), state_expression(user_id, 'starred'), trash_expression(user_id))


def resource_key(url):
    return hashlib.sha256(url.encode('utf8')).hexdigest()


def resource_url(value, base=''):
    from backend.scraper.http_client import validate_public_url
    try:
        parts = urlsplit(urljoin(base, (value or '').strip()))
        return validate_public_url(urlunsplit(parts._replace(fragment='')), resolve=False)
    except (ValueError, TypeError, UnicodeError):
        return ''


def observed_resources(ann):
    from backend.scraper.sanitizer import sanitize_html
    soup = BeautifulSoup(sanitize_html(ann.content_html or ''), 'lxml')
    resources = {}
    for node in soup.select('a[href], img[src]'):
        url = resource_url(node.get('href') or node.get('src'), ann.url)
        if not url:
            continue
        kind = 'image' if node.name == 'img' else 'attachment'
        label = node.get_text(' ', strip=True) if kind == 'attachment' else node.get('alt', '')
        if kind == 'attachment' and not (FILES.search(urlsplit(url).path) or node.has_attr('download')
            or re.search('附件|下载', label) or re.search(r'/(?:download|attach|attachment)(?:/|\.|\?|$)', url, re.I)):
            continue
        filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
        resources.setdefault(resource_key(url), {'url': url, 'kind': kind,
            'filename': (label or filename or '官网附件')[:1000]})
    return resources


def queue_materials(ann, *, retry=False):
    """Enqueue all observed files once. Reading status never restarts paid or I/O work."""
    if not is_pinned(ann.id):
        return
    from backend.services import tasks
    if not (ann.content_cached_at or ann.content_html):
        job = BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
        if not job or retry or job.state not in ('failed', 'done'):
            from backend.services.content_cache import request_content
            request_content(ann)
        elif job.next_run_at and job.next_run_at <= datetime.utcnow():
            tasks.enqueue('content', ann.id, {'announcement_id': ann.id})
        return
    for key, values in observed_resources(ann).items():
        db.session.execute(insert(AnnouncementResource).values(announcement_id=ann.id,
            url_key=key, state='pending', **values).on_conflict_do_nothing(index_elements=['announcement_id','url_key']))
    ann.resources_scanned_at = datetime.utcnow()
    db.session.flush()
    for row in AnnouncementResource.query.filter_by(announcement_id=ann.id).all():
        if row.state == 'ready':
            continue
        identity = f'saved_resource:{ann.id}:{row.url_key}'
        job = BackgroundTask.query.filter_by(identity=identity).first()
        if not job or retry or (row.state == 'pending' and job.state == 'done'):
            row.state = 'pending'
            tasks.enqueue('saved_resource', f'{ann.id}:{row.url_key}',
                {'announcement_id': ann.id, 'url_key': row.url_key}, commit=False)
    db.session.commit()


def material_status(ann):
    rows = AnnouncementResource.query.filter_by(announcement_id=ann.id).all()
    state = 'saving'
    failed = [row for row in rows if row.state == 'failed']
    pending = BackgroundTask.query.filter(BackgroundTask.identity.in_(
        [f'saved_resource:{ann.id}:{row.url_key}' for row in failed] + [f'content:{ann.id}']),
        BackgroundTask.state.in_(('pending', 'running', 'waiting'))).count()
    if ((ann.content_error and not (ann.content_html or ann.content_cached_at)) or failed) and not pending:
        state = 'failed'
    elif (ann.content_cached_at or ann.content_html) and ann.resources_scanned_at and all(row.state == 'ready' for row in rows):
        state = 'ready'
    return {'state': state, 'attachments': sum(row.kind == 'attachment' for row in rows),
        'saved': sum(row.state == 'ready' for row in rows), 'total': len(rows),
        'bytes': sum(row.byte_size for row in rows), 'resources': [{'key': row.url_key,
            'filename': row.filename, 'kind': row.kind, 'state': row.state, 'error': row.error,
            'url': f'/api/announcements/{ann.id}/resources/{row.url_key}' if row.state == 'ready' else ''}
            for row in rows]}


class ResourceReadError(ValueError):
    retryable = False


def download_resource(url, article_url, kind='attachment'):
    from backend.scraper.acquisition import FetchRequest
    from backend.scraper.acquisition.http_transport import fetch_http
    from backend.services.discovery_policy import get_policy
    from backend.scraper.article_images import MAX_IMAGE_BYTES, _image_type
    limit = MAX_IMAGE_BYTES if kind == 'image' else int(current_app.config.get('SAVED_ATTACHMENT_MAX_BYTES', MAX_ATTACHMENT_BYTES))
    result = fetch_http(FetchRequest(url, purpose='article', source_id='saved-resource:' + resource_key(url),
        timeout_seconds=get_policy()['http_total_seconds'], browser_allowed=False), capture_binary=True,
        max_bytes=limit, extra_headers={'Referer': article_url, 'Accept': '*/*'})
    if result.outcome == 'busy':
        from backend.services.tasks import defer
        defer(delay=result.retry_after or 1, phase='origin_wait', reason='附件保存等待官网读取', error_code='origin_busy')
    if not result.ok:
        error = ResourceReadError(result.message or '官网附件未能完整读取')
        error.result, error.error_code = result, result.error_code
        error.retryable = result.outcome == 'network_error'
        raise error
    payload = result.attachment.get('payload', b'')
    mime = (result.headers.get('Content-Type') or 'application/octet-stream').split(';', 1)[0].lower()
    if not payload or mime in ('text/html', 'application/xhtml+xml') or re.match(br'\s*(?:<!doctype\s+html|<html\b)', payload[:512], re.I):
        raise ResourceReadError('官网返回了网页或空文件，附件尚未保存')
    if kind == 'image':
        mime = _image_type(payload)
    elif mime == 'application/pdf' or urlsplit(url).path.lower().endswith('.pdf'):
        from backend.scraper.acquisition.attachments import pdf_observation
        if not pdf_observation(payload, result.final_url, result.headers):
            raise ResourceReadError('官网未返回完整 PDF，附件尚未保存')
        mime = 'application/pdf'
    disposition = Message(); disposition['Content-Disposition'] = result.headers.get('Content-Disposition', '')
    filename = disposition.get_filename() or unquote(urlsplit(result.final_url).path.rsplit('/', 1)[-1]) or '官网附件'
    filename = re.sub(r'[\x00-\x1f\x7f]', '', str(filename).replace('\\','/').rsplit('/',1)[-1])[:1000]
    return payload, mime, filename


def save_resource(ann_id, key):
    row = db.session.get(AnnouncementResource, (ann_id, key))
    ann = db.session.get(Announcement, ann_id)
    if not row or not ann or not is_pinned(ann_id):
        return {'skipped': True}
    if row.state == 'ready':
        return {'cached': True}
    url, article_url, kind = row.url, ann.url, row.kind
    db.session.commit()
    try:
        payload, mime, filename = download_resource(url, article_url, kind)
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(AnnouncementResource, (ann_id, key))
        if row:
            row.state, row.error = 'failed', str(exc)[:500]
            db.session.commit()
        raise
    # A long request cannot resurrect a file after its last retention pin expired.
    ann = db.session.execute(select(Announcement).where(Announcement.id == ann_id).with_for_update()).scalar_one_or_none()
    row = db.session.get(AnnouncementResource, (ann_id, key))
    if not ann or not row or row.url != url or not is_pinned(ann_id):
        db.session.rollback()
        return {'skipped': True}
    row.payload, row.mime_type, row.filename = payload, mime, filename
    row.byte_size, row.sha256 = len(payload), hashlib.sha256(payload).hexdigest()
    row.state, row.error, row.saved_at = 'ready', '', datetime.utcnow()
    db.session.commit()
    return {'saved': True, 'bytes': len(payload)}


def resume_saved_materials(limit=20):
    """Bounded startup recovery for existing favorites and overdue failed reads."""
    busy_bodies = select(BackgroundTask.payload['announcement_id'].as_integer()).where(BackgroundTask.kind == 'content',
        or_(BackgroundTask.state.in_(('pending','running','waiting')),
            (BackgroundTask.state == 'failed') & (BackgroundTask.next_run_at > datetime.utcnow())))
    pending = Announcement.query.filter(pin_expression(), Announcement.resources_scanned_at.is_(None),
        or_(Announcement.content_html.isnot(None) & (Announcement.content_html != ''),
            Announcement.content_cached_at.isnot(None), ~Announcement.id.in_(busy_bodies))).order_by(Announcement.id).limit(limit).all()
    for ann in pending:
        queue_materials(ann)
    jobs = BackgroundTask.query.filter_by(kind='saved_resource', state='failed').filter(
        BackgroundTask.next_run_at <= datetime.utcnow()).order_by(BackgroundTask.next_run_at).limit(limit).all()
    for job in jobs:
        ann = db.session.get(Announcement, job.payload.get('announcement_id'))
        if ann and is_pinned(ann.id):
            queue_materials(ann, retry=True)
