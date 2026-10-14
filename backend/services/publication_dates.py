"""Verify article publication metadata; keep event dates and URL paths out of it.

The existing durable task stores the small date evidence/result for local reuse.
No model is called, and reconciling saved HTML never refreshes the saved body.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re

from bs4 import BeautifulSoup
from sqlalchemy import select
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask
from backend.scraper.article_observation import article_heading, publication_nodes

DATE = re.compile(r'(?<!\d)(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})日?(?!\d)')
PUBLISH = re.compile(r'^(?:发布日期|发布时间|发文日期|发布于)\s*[：:]?\s*')
META_KEYS = {'pubdate','publishdate','publicationdate','article:published_time','datepublished'}
EDITOR = re.compile(r'责任编辑|编辑[:：]|作者[:：]|来源[:：]|浏览(?:量|次数)?[:：]')
PARSER_VERSION = 'title-bound-2'


def publication_date_stamp(value):
    if not value:return ''
    return value.isoformat() if any((value.hour,value.minute,value.second,value.microsecond)) else value.strftime('%Y-%m-%d')


def publication_date_label(value):
    if not value:return '发布时间未知'
    return value.strftime('%Y年%m月%d日 %H:%M' if any((value.hour,value.minute,value.second,value.microsecond)) else '%Y年%m月%d日')


def title_key(value):
    return re.sub(r'\W+','',value or '').casefold()


def _date(value):
    match = DATE.search(value or '')
    if not match:
        return None
    try:return datetime(*map(int,match.groups()))
    except ValueError:return None


def inspect_article_date(html, url, title):
    unknown = {'state':'unknown','url':url,'title_key':title_key(title),'parser_version':PARSER_VERSION}
    if not html or not unknown['title_key']:
        return unknown
    soup = BeautifulSoup(html,'lxml')
    for node in soup.select('script,style,nav,aside,footer,blockquote'):
        node.decompose()
    for node in soup.select('form'):
        if node.select('input:not([type="hidden"]),textarea,select,button'):
            node.decompose()
    heading = article_heading(soup,title)
    if heading is None:
        return dict(unknown,reason='article_heading_not_unique_or_matched')
    candidates=[]
    nodes, _ = publication_nodes(soup,heading)
    for node,text in nodes:
        date=_date(text)
        if date:
            from backend.scraper.discovery.structure import locator
            candidates.append({'date':date.strftime('%Y-%m-%d'),'basis':'visible_publication_metadata',
                'text':text,'locator':locator(node)})
    if not candidates:
        for node in soup.select('head meta[name],head meta[property],head meta[itemprop], [itemprop="datePublished"]'):
            if (node.get('name') or node.get('property') or node.get('itemprop') or '').casefold() in META_KEYS:
                value=node.get('content','');date=_date(value)
                if date:candidates.append({'date':date.strftime('%Y-%m-%d'),'basis':'publication_meta',
                    'text':value[:220],'locator':str(node)[:500]})
    unique={candidate['date'] for candidate in candidates}
    if len(unique)!=1:
        return dict(unknown,reason='publication_metadata_missing_or_conflicting')
    return dict(unknown,**candidates[0],state='verified',content_sha256=hashlib.sha256(html.encode()).hexdigest())


def _job(ann):
    return BackgroundTask.query.filter_by(identity=f'publication_date:{ann.id}').populate_existing().first()


def _valid(proof,ann):
    return bool(proof and proof.get('state')=='verified' and proof.get('url')==ann.url
        and proof.get('title_key')==title_key(ann.title) and ann.published_at
        and proof.get('date')==ann.published_at.strftime('%Y-%m-%d'))


def _record(ann,proof):
    from backend.database.dialect import insert
    now=datetime.utcnow()
    cached_hash=hashlib.sha256((ann.content_html or '').encode()).hexdigest()
    result=dict(proof,cached_body_hash=cached_hash)
    db.session.execute(insert(BackgroundTask).values(identity=f'publication_date:{ann.id}',kind='publication_date',
        payload={'announcement_id':ann.id},result=result,checkpoint={},state='done',phase='complete',
        capability='http',generation=1,attempts=0,claim_count=0,created_at=now,queued_at=now,
        updated_at=now,checked_at=now,finished_at=now,available_at=now).on_conflict_do_update(
            index_elements=['identity'],set_={'result':result,'updated_at':now}))


def apply_article_date(ann,html,reference_url=None,*,cached_only=False):
    if reference_url and reference_url!=ann.url:
        from backend.services.source_inventory import canonical_url
        if canonical_url(reference_url)!=canonical_url(ann.url):
            return False
    proof=dict(inspect_article_date(html,ann.url,ann.title),checked_full=not cached_only)
    if proof['state']!='verified':
        previous=_job(ann)
        if previous and _valid(previous.result,ann):
            return False  # Incomplete new material cannot revoke verified metadata.
    changed=False
    if proof['state']=='verified':
        value=datetime.fromisoformat(proof['date'])
        changed=ann.published_at!=value
        proof['corrected']=changed
        proof['previous_date']=ann.published_at.isoformat() if ann.published_at else None
        ann.published_at=value
    _record(ann,proof)
    return changed


def reconcile_cached_date(ann):
    if not ann.content_html:return False
    job=_job(ann)
    if job and (_valid(job.result,ann) or ((job.result or {}).get('parser_version')==PARSER_VERSION
            and (job.result or {}).get('cached_body_hash')==hashlib.sha256(ann.content_html.encode()).hexdigest())):
        return False
    apply_article_date(ann,ann.content_html,cached_only=True)
    return True  # Persist the new evidence marker even if the existing date matched.


def queue_date_check(ann):
    from backend.services import tasks
    job=_job(ann)
    if job and (_valid(job.result,ann) or job.state in ('pending','running','waiting')):
        return job
    if job and job.state=='done' and (job.result or {}).get('parser_version')==PARSER_VERSION and (job.result or {}).get('checked_full'):
        return job  # A complete unknown observation is not reread every collection.
    if job and job.next_run_at and job.next_run_at>datetime.utcnow():return job
    return tasks.enqueue('publication_date',ann.id,{'announcement_id':ann.id},commit=False)


def apply_listing_date(ann,department,observation,candidate_date):
    if ann.department_id!=department.id:return
    reconcile_cached_date(ann)
    local_today=datetime.now(timezone(timedelta(hours=8))).date()
    uncertain=(not ann.published_at and observation['role'] in ('missing','ambiguous')) or observation['role']=='non_publication' or bool(candidate_date and (
        observation['role']=='ambiguous' or candidate_date.date()>local_today))
    if not uncertain:return
    proof=_job(ann)
    if proof and _valid(proof.result,ann):return
    # This is an actual observed list field, not a rule for clearing unrelated
    # historical dates. Preserve a primary date which differs from that hint.
    if ann.published_at and candidate_date and ann.published_at.date()==candidate_date.date():
        ann.published_at=None
    # A cached failed-to-recognize header needs one actual metadata read; it is
    # not a reason to refresh or replace permanently saved bodies.
    if proof and proof.state=='done' and proof.result.get('state')=='unknown' and not proof.result.get('checked_full'):
        proof.state='failed';proof.next_run_at=datetime.utcnow()
    queue_date_check(ann)


def check_date(ann_id):
    from backend.scraper.engine import _fetch_html
    ann=db.session.get(Announcement,ann_id)
    if not ann:return {'state':'removed'}
    reconcile_cached_date(ann)
    job=_job(ann)
    if job and _valid(job.result,ann):return dict(job.result,cached=True)
    content=BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
    if not ann.content_html and content and content.state in ('pending','running','waiting'):
        from backend.services.tasks import defer
        defer(delay=2,phase='await_body',reason='等待正文读取以复用发布日期依据')
    url,title=ann.url,ann.title
    started=datetime.utcnow()
    db.session.commit()
    html=_fetch_html(url,raise_fetch_errors=True,purpose='article',source_id='publication-date:'+str(ann_id),
        policy={'expected_article_title':title,'publication_metadata_only':True})
    final=getattr(html,'final_url',url)
    from backend.services.source_inventory import canonical_url
    if canonical_url(final)!=canonical_url(url):return {'state':'unknown','reason':'different_article_address'}
    proof=dict(inspect_article_date(html,url,title),checked_full=True)
    ann=db.session.execute(select(Announcement).where(Announcement.id==ann_id).with_for_update()).scalar_one_or_none()
    if not ann or ann.url!=url or ann.title!=title:return {'state':'stale'}
    current=_job(ann)
    if current and current.updated_at and current.updated_at>started and _valid(current.result,ann):
        return dict(current.result,cached=True)
    if proof['state']=='verified':
        value=datetime.fromisoformat(proof['date'])
        proof['corrected']=ann.published_at!=value
        proof['previous_date']=ann.published_at.isoformat() if ann.published_at else None
        ann.published_at=value
    _record(ann,proof);db.session.commit()
    return proof


def resume_date_checks(limit=20):
    """Reconcile existing saved headers in slices, with no outbound requests."""
    exists=db.select(BackgroundTask.id).where(BackgroundTask.identity==db.literal('publication_date:')+db.cast(Announcement.id,db.String)).exists()
    for ann in Announcement.query.filter(Announcement.content_html.isnot(None),Announcement.content_html!='',~exists).order_by(Announcement.id).limit(limit):
        reconcile_cached_date(ann)
    # Retry old unrecognized metadata once after a parser revision. Keep verified
    # dates and saved bodies intact, and never reset the task's retry cooldown.
    old=BackgroundTask.query.filter_by(kind='publication_date',state='done').filter(
        BackgroundTask.result['state'].as_string()=='unknown',
        BackgroundTask.result['parser_version'].as_string().is_distinct_from(PARSER_VERSION)).order_by(BackgroundTask.id).limit(limit).all()
    for job in old:
        ann=db.session.get(Announcement,job.payload.get('announcement_id'))
        if ann:
            reconcile_cached_date(ann)
            if not _valid(job.result,ann):queue_date_check(ann)
    jobs=BackgroundTask.query.filter_by(kind='publication_date',state='failed').filter(BackgroundTask.next_run_at<=datetime.utcnow()).limit(limit).all()
    for job in jobs:
        ann=db.session.get(Announcement,job.payload.get('announcement_id'))
        if ann:queue_date_check(ann)
    db.session.commit()
