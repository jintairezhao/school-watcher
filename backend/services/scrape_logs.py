"""Complete dated history in small pages, with independently configurable retention."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_, or_
from backend.database.db import db
from backend.database.models import AppConfig, School, ScrapeLog, BackgroundTask

PAGE_SIZE = 200
RETENTION_CHOICES = (7, 30, 90, 180, 365, 0)
STATUS_LABELS = {'running':'进行中', 'success':'成功', 'partial':'部分完成', 'failed':'失败', 'interrupted':'已中断', 'waiting':'等待处理'}


def retention_days():
    try:
        value = int(AppConfig.get('scrape_log_retention_days', '30'))
    except (TypeError, ValueError):
        return 30
    return value if value in RETENTION_CHOICES else 30


def prune_scrape_logs(*, now=None):
    days = retention_days()
    if days == 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=days)
    deleted = ScrapeLog.query.filter(ScrapeLog.started_at < cutoff, ScrapeLog.status != 'running').delete()
    db.session.commit()
    return deleted


def recover_interrupted_logs(*, now=None):
    """Only close attempts whose recorded owner can no longer be active."""
    now = now or datetime.utcnow()
    rows = ScrapeLog.query.filter_by(status='running').all()
    recovered = 0
    for log in rows:
        task = db.session.get(BackgroundTask, log.task_id) if log.task_id else None
        if (task and task.state == 'running' and task.generation == log.task_generation
                and task.token == log.task_token
                and task.lease_until and task.lease_until > now):
            continue
        log.status = 'interrupted'
        log.finished_at = log.finished_at or now
        reason = '上次采集进程已中断；未完成任务将由后台恢复。'
        log.error_message = (log.error_message + '\n' if log.error_message else '') + reason
        recovered += 1
    db.session.commit()
    return recovered


def parse_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('时间格式不正确')
    stamp = datetime.fromisoformat(value)
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def utc_stamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


def log_page(args):
    period = args.get('period', 'recent')
    if period not in ('recent', 'archive'):
        raise ValueError('请选择近一周或更早记录')
    anchor = parse_time(args['as_of']) if args.get('as_of') else datetime.utcnow()
    cutoff = anchor - timedelta(days=7)
    base = ScrapeLog.query.filter(ScrapeLog.started_at <= anchor)
    if args.get('school_id'):
        school_id = int(args['school_id'])
        if school_id <= 0:
            raise ValueError('学校编号不正确')
        base = base.filter(ScrapeLog.school_id == school_id)
    archive = base.filter(ScrapeLog.started_at < cutoff)
    query = base.filter(ScrapeLog.started_at >= cutoff) if period == 'recent' else archive
    total, older_total = query.count(), archive.count()
    if args.get('before_id') or args.get('before_time'):
        ident, before = int(args['before_id']), parse_time(args['before_time'])
        if ident <= 0:
            raise ValueError('分页位置不正确')
        query = query.filter(or_(ScrapeLog.started_at < before,
                                 and_(ScrapeLog.started_at == before, ScrapeLog.id < ident)))
    rows = (query.add_columns(School.name).outerjoin(School, School.id == ScrapeLog.school_id)
            .order_by(ScrapeLog.started_at.desc(), ScrapeLog.id.desc()).limit(PAGE_SIZE + 1).all())
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    items = []
    for log, school_name in rows:
        duration = max(0, (log.finished_at - log.started_at).total_seconds()) if log.finished_at and log.started_at else None
        items.append({**log.to_dict(),
            'school_name': school_name or (f'学校已移除（编号 {log.school_id}）' if log.school_id else '未归属学校'),
            'source_name': log.source_name or '历史任务（未记录具体栏目）',
            'started_at': utc_stamp(log.started_at), 'finished_at': utc_stamp(log.finished_at),
            'duration_seconds': round(duration, 1) if duration is not None else None,
            'status_label': STATUS_LABELS.get(log.status, log.status or '未知')})
    cursor = {'before_id': rows[-1][0].id, 'before_time': utc_stamp(rows[-1][0].started_at)} if more else None
    return {'rows': items, 'total': total, 'older_total': older_total,
            'next_cursor': cursor, 'as_of': utc_stamp(anchor), 'period': period}
