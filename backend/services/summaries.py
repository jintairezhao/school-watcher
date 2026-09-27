"""Explicit, shared summary jobs with content-version and worker ownership fences.

No function used by a read path invokes a model or enqueues paid work. Existing
Announcement.summary values remain unversioned history, never current results.
"""
from datetime import datetime
import hashlib
import json
import re

from flask import current_app
from sqlalchemy import func, select, update

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask
from backend.ai.summary_models import AnnouncementSummary
from backend.services import tasks

ACTIVE = ('pending', 'waiting_content', 'running')
SKILL_ID = 'summarize-university-notice'
MAX_CHARACTERS = 120000
CHUNK_CHARACTERS = 8000


def input_hash(title, body):
    return hashlib.sha256(json.dumps([title or '', body or ''], ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _matches(row, ann):
    return (row.title_snapshot == (ann.title or '') and row.body_snapshot == (ann.content_text or '')
            and bool(row.body_snapshot))


def _valid_conditions():
    return (AnnouncementSummary.announcement_id == Announcement.id,
            AnnouncementSummary.state == 'succeeded',
            AnnouncementSummary.title_snapshot == func.coalesce(Announcement.title, ''),
            AnnouncementSummary.body_snapshot == func.coalesce(Announcement.content_text, ''),
            AnnouncementSummary.body_snapshot != '')


def summary_expression():
    """Portable SQL expression for current-only search/list/export projections."""
    return (select(AnnouncementSummary.summary).where(*_valid_conditions())
            .order_by(AnnouncementSummary.id.desc()).limit(1).correlate(Announcement).scalar_subquery())


def current_summaries(announcements):
    """One query, even for large inbox pages. Values are summary text."""
    ids = [ann.id for ann in announcements]
    if not ids:
        return {}
    return dict(db.session.execute(select(Announcement.id, summary_expression()).where(
        Announcement.id.in_(ids))).all())


def current_summary(ann):
    return current_summaries([ann]).get(ann.id) or ''


def _record_dict(row):
    task_id = row.task_id
    if task_id is None and row.state in ACTIVE:
        task_id = db.session.scalar(select(BackgroundTask.id).where(BackgroundTask.identity == f'summary:{row.id}'))
    return {'id': row.id, 'status': row.state, 'summary': row.summary if row.state in ('succeeded', 'historical') else '',
            'task_id': task_id, 'input_hash': row.input_hash, 'input_scope': row.input_scope or {},
            'error': row.error, 'error_code': row.error_code, 'provenance': row.provenance or {},
            'created_at': row.created_at.isoformat(),
            'completed_at': row.completed_at.isoformat() if row.completed_at else None}


def export_summary(ann):
    """Portable current version; no credentials, local task IDs or full input copy."""
    row = AnnouncementSummary.query.filter(*_valid_conditions(), Announcement.id == ann.id).order_by(
        AnnouncementSummary.id.desc()).first()
    if row is None:
        return None
    return {'input_hash': row.input_hash, 'title': row.title_snapshot, 'summary': row.summary,
            'output': row.output, 'input_scope': row.input_scope, 'skill_version': row.skill_version,
            'provenance': {k: v for k, v in (row.provenance or {}).items()
                           if k in ('skill_id', 'skill_version', 'provider', 'model', 'resource_hash')},
            'generated_at': row.completed_at.isoformat() if row.completed_at else None}


def import_summary(ann, data):
    """Add a portable version within the caller's import transaction; never overwrite."""
    if not isinstance(data, dict) or not isinstance(data.get('summary'), str) or not data['summary'].strip():
        return False
    if len(data['summary']) > 20000 or len(json.dumps(data, ensure_ascii=False)) > 512000:
        raise ValueError('导入的摘要内容过长')
    stamp = data.get('input_hash')
    if not isinstance(stamp, str) or len(stamp) != 64:
        stamp = input_hash('unverified-import', data['summary'])
    portable = {k: data.get(k) for k in ('input_hash', 'title', 'summary', 'skill_version', 'output')}
    identity = 'import:' + str(ann.id) + ':' + hashlib.sha256(
        json.dumps(portable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if AnnouncementSummary.query.filter_by(identity=identity).first():
        return False
    verified = bool(ann.content_text and data.get('input_hash') == input_hash(ann.title, ann.content_text)
                    and data.get('title') == ann.title)
    if verified:
        try:
            _validate_output(data.get('output'), _paragraphs(ann.content_text))
            verified = data['output']['summary'].strip() == data['summary'].strip()
        except (ValueError, TypeError, KeyError):
            verified = False
    if verified and current_summary(ann):
        return False
    revision = 1 + (db.session.query(func.max(AnnouncementSummary.revision)).filter_by(
        announcement_id=ann.id, input_hash=stamp).scalar() or 0)
    db.session.add(AnnouncementSummary(announcement_id=ann.id, identity=identity, revision=revision,
        input_hash=stamp, title_snapshot=ann.title if verified else str(data.get('title') or ''),
        body_snapshot=ann.content_text if verified else '', state='succeeded' if verified else 'historical',
        summary=data['summary'].strip(), output=data.get('output') if isinstance(data.get('output'), dict) else {},
        input_scope=data.get('input_scope') if isinstance(data.get('input_scope'), dict) else {},
        skill_version=str(data.get('skill_version') or '')[:100],
        provenance={'origin': 'import', 'version_known': verified}, completed_at=datetime.utcnow()))
    db.session.flush()
    return True


def summary_status(ann):
    """Read-only status, including clearly labelled historical summaries."""
    records = AnnouncementSummary.query.filter_by(announcement_id=ann.id).order_by(
        AnnouncementSummary.id.desc()).all()
    valid = next((r for r in records if r.state == 'succeeded' and _matches(r, ann)), None)
    pending = next((r for r in records if r.state in ACTIVE and
                    (r.title_snapshot == (ann.title or '')) and
                    (not r.body_snapshot or _matches(r, ann))), None)
    latest = next((r for r in records if r.state != 'historical' and (_matches(r, ann) or
        (not r.body_snapshot and r.title_snapshot == (ann.title or '')))), None)
    current = pending or latest or valid
    history = [_record_dict(r) for r in records if r.state in ('succeeded', 'historical') and r is not valid]
    if ann.summary and not any(item['summary'] == ann.summary for item in history):
        history.append({'id': None, 'status': 'historical', 'summary': ann.summary,
                        'input_hash': None, 'provenance': {'origin': 'legacy', 'version_known': False}})
    result = _record_dict(current) if current else {'status': 'none', 'summary': '', 'task_id': None,
        'input_hash': input_hash(ann.title, ann.content_text), 'error': '', 'error_code': ''}
    if valid:
        result['summary'] = valid.summary
        result['current_id'] = valid.id
    if pending and pending.task_id:
        task = db.session.get(BackgroundTask, pending.task_id)
        if task and task.state == 'failed':
            result.update(status='failed', error=task.error or '摘要任务未完成',
                          error_code=task.error_code or 'task_failed')
    result['historical'] = history
    return result


def _lock_article(ann_id):
    # A no-op write is a short row lock in PostgreSQL and serializes SQLite
    # writers. The lock remains held until the summary and queue are committed.
    result = db.session.execute(update(Announcement).where(Announcement.id == ann_id).values(
        id=Announcement.id).execution_options(synchronize_session=False))
    if not result.rowcount:
        raise ValueError('通知不存在')
    ann = db.session.get(Announcement, ann_id)
    db.session.refresh(ann)
    return ann


def request_summary(ann, *, requested_by=None, force=False):
    """Called only from an explicit authenticated action (or an admin batch)."""
    from backend.ai.configuration import get_model_binding
    ann_id = ann.id
    # Fast cached reads never require a working API configuration.
    status = summary_status(ann)
    if status['summary'] and not force:
        return status
    binding = get_model_binding('summary')
    db.session.commit()
    ann = _lock_article(ann_id)
    stamp = input_hash(ann.title, ann.content_text)
    rows = AnnouncementSummary.query.filter_by(announcement_id=ann.id).order_by(
        AnnouncementSummary.id.desc()).all()
    for row in rows:
        matches = _matches(row, ann)
        if not force and row.state == 'succeeded' and matches:
            db.session.commit()
            return summary_status(ann)
        if row.state in ACTIVE:
            task = db.session.get(BackgroundTask, row.task_id) if row.task_id else None
            if task and task.state == 'failed':
                row.state, row.error = 'failed', task.error or '摘要任务未完成'
            elif matches or (not row.body_snapshot and row.title_snapshot == (ann.title or '')):
                db.session.commit()
                return summary_status(ann)
            else:
                row.state = 'stale'
        if row.state == 'uncertain' and matches and not force:
            db.session.commit()
            return summary_status(ann)
    revision = 1 + max((r.revision for r in rows if r.input_hash == stamp), default=0)
    row = AnnouncementSummary(announcement_id=ann.id, identity=f'{ann.id}:{stamp}:{revision}',
        revision=revision, input_hash=stamp, title_snapshot=ann.title or '',
        body_snapshot=ann.content_text or '', source_content_hash=ann.content_hash,
        binding=binding, requested_by=requested_by, state='pending')
    db.session.add(row)
    db.session.flush()
    # enqueue commits the summary reservation and unique task in one transaction.
    task = tasks.enqueue('summary', row.id, {'summary_id': row.id}, replace_finished=False)
    row.task_id = task.id
    db.session.commit()
    return summary_status(ann)


def enqueue_batch(ids, *, requested_by=None, force=False):
    if not isinstance(ids, list) or not ids or len(ids) > 50 or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError('请选择 1–50 条通知')
    from backend.services.announcement_sources import source_expression
    rows = Announcement.query.filter(Announcement.id.in_(set(ids)), source_expression()).all()
    results = [request_summary(ann, requested_by=requested_by, force=force) for ann in rows]
    return {'count': len(results), 'task_ids': sorted({r['task_id'] for r in results if r.get('task_id')}),
            'results': results}


def _paragraphs(text):
    # Large single-paragraph notices are split too; no trailing text is dropped.
    result = []
    for paragraph in re.split(r'\n\s*\n|\r?\n', text):
        if not paragraph.strip():
            continue
        for start in range(0, len(paragraph), CHUNK_CHARACTERS):
            part = paragraph[start:start + CHUNK_CHARACTERS].strip()
            if part:
                result.append({'id': f'p{len(result) + 1}', 'text': part})
    return result


def _chunks(paragraphs):
    groups, group, size = [], [], 0
    for paragraph in paragraphs:
        if group and size + len(paragraph['text']) > CHUNK_CHARACTERS:
            groups.append(group)
            group, size = [], 0
        group.append(paragraph)
        size += len(paragraph['text'])
    if group:
        groups.append(group)
    return groups


def _failure(row, code, message, state='failed'):
    row.state, row.error_code, row.error = state, code, str(message)[:300]
    row.completed_at = datetime.utcnow()
    db.session.commit()
    return _record_dict(row)


def _validate_output(output, paragraphs, *, extract=False):
    if not isinstance(output, dict) or not isinstance(output.get('summary'), str):
        raise ValueError('摘要返回格式无效')
    if not extract and not output['summary'].strip():
        raise ValueError('摘要为空')
    if output['summary'].startswith(('（AI摘要生成失败', '（内容过短', '生成失败')):
        raise ValueError('失败提示不能保存为摘要')
    ids = {p['id'] for p in paragraphs}
    coverage = output.get('coverage') or {}
    if set(coverage.get('paragraph_ids') or []) != ids or coverage.get('attachments_included') is not False:
        raise ValueError('摘要未覆盖全部输入段落或误称读取了附件')
    if not isinstance(output.get('facts'), list):
        raise ValueError('摘要缺少事实引用')
    for fact in output['facts']:
        if not isinstance(fact, dict) or not fact.get('text') or not fact.get('evidence_ids'):
            raise ValueError('摘要事实缺少正文引用')
        if not set(fact['evidence_ids']).issubset(ids):
            raise ValueError('摘要引用了不存在的段落')
    return output


def _call(row_id, mode, evidence, stage):
    from backend.ai.runtime import run_skill
    row = db.session.get(AnnouncementSummary, row_id)
    binding = dict(row.binding)
    execution_id = f'summary:{row.identity}:{stage}'
    db.session.commit()
    tasks.assert_owned()
    result = run_skill(SKILL_ID, mode=mode, evidence=evidence, purpose='summary',
                       binding=binding, expected_version=binding.get('version'), execution_id=execution_id)
    tasks.assert_owned()
    if result.get('status') == 'failed' and result.get('error_code') in (
            'invalid_json', 'output_validation_failed', 'incomplete_model_output'):
        db.session.commit()
        result = run_skill(SKILL_ID, mode=mode, evidence=evidence, purpose='summary', binding=binding,
            expected_version=binding.get('version'), execution_id=execution_id + ':repair',
            repair_feedback=result['error_code'])
        tasks.assert_owned()
    # Always reload after network I/O so a superseded row is not overwritten.
    db.session.expire_all()
    row = db.session.get(AnnouncementSummary, row_id)
    if result.get('status') != 'succeeded':
        status = result.get('status')
        message = result.get('error') or '摘要暂时无法生成，请查看实例 AI 配置与调用记录'
        state = 'uncertain' if status in ('uncertain', 'pending') else 'failed'
        _failure(row, result.get('error_code') or 'ai_failed', message, state)
        return None
    return result


def generate_summary(summary_id):
    """Durable worker handler. A missing body yields to the shared content job."""
    tasks.assert_owned()
    row = db.session.get(AnnouncementSummary, summary_id)
    if not row:
        return {'removed': True}
    if row.state not in ACTIVE:
        return _record_dict(row)
    ann = db.session.get(Announcement, row.announcement_id)
    if not ann:
        return _failure(row, 'removed', '通知不存在')
    if ann.title != row.title_snapshot:
        return _failure(row, 'content_changed', '通知已更新，请重新生成', 'stale')
    if not ann.content_text or not ann.content_text.strip():
        from backend.services.content_cache import request_content
        body_task = BackgroundTask.query.filter_by(identity=f'content:{ann.id}').first()
        if body_task and body_task.state in ('failed', 'waiting'):
            return _failure(row, 'content_unavailable', body_task.error or '正文暂时无法读取，请先处理访问问题')
        if ann.content_cached_at:
            return _failure(row, 'content_empty', '正文没有可供概括的文字', 'not_needed')
        row.state = 'waiting_content'
        db.session.commit()
        request_content(ann)
        raise tasks.TaskDeferred(phase='summary_content', delay=3, checkpoint={'summary_id': summary_id},
                                 reason='正在读取官网正文')
    body = ann.content_text
    if row.body_snapshot and row.body_snapshot != body:
        return _failure(row, 'content_changed', '通知正文已更新，请重新生成', 'stale')
    if len(body.strip()) < 20:
        return _failure(row, 'content_short', '正文较短，无需生成摘要', 'not_needed')
    if len(body) > int(current_app.config.get('SUMMARY_MAX_CHARACTERS', MAX_CHARACTERS)):
        return _failure(row, 'content_too_large', '正文超出本次摘要处理范围，请阅读原文；未截断生成摘要')
    if not row.body_snapshot:
        ann = _lock_article(ann.id)
        if ann.title != row.title_snapshot or ann.content_text != body:
            return _failure(row, 'content_changed', '通知正文已更新，请重新生成', 'stale')
        actual_hash = input_hash(ann.title, body)
        row.revision = 1 + (db.session.query(func.max(AnnouncementSummary.revision)).filter(
            AnnouncementSummary.announcement_id == ann.id, AnnouncementSummary.input_hash == actual_hash,
            AnnouncementSummary.id != row.id).scalar() or 0)
        row.body_snapshot, row.input_hash = body, actual_hash
        row.source_content_hash = ann.content_hash
    paragraphs = _paragraphs(body)
    groups = _chunks(paragraphs)
    row.input_scope = {'kind': 'full_text', 'paragraphs': len(paragraphs), 'characters': len(body),
                       'attachments_included': False}
    row.state = 'running'
    db.session.commit()
    try:
        if len(groups) == 1:
            result = _call(summary_id, 'summary', {'title': row.title_snapshot,
                'paragraphs': paragraphs, 'attachments_unread': True}, 'direct')
            if result is None:
                return _record_dict(db.session.get(AnnouncementSummary, summary_id))
            output = _validate_output(result['output'], paragraphs)
        else:
            extracted = dict(row.progress or {}).get('chunks', {})
            for index, group in enumerate(groups):
                key = str(index)
                if key not in extracted:
                    result = _call(summary_id, 'extract', {'title': row.title_snapshot,
                        'paragraphs': group, 'attachments_unread': True}, 'chunk-' + key)
                    if result is None:
                        return _record_dict(db.session.get(AnnouncementSummary, summary_id))
                    extracted[key] = _validate_output(result['output'], group, extract=True)
                    row = db.session.get(AnnouncementSummary, summary_id)
                    row.progress = {'chunks': extracted}
                    db.session.commit()
            facts = [fact for chunk in extracted.values() for fact in chunk['facts']]
            compact = [{'id': p['id'], 'text': '\n'.join(dict.fromkeys(
                f['text'] for f in facts if p['id'] in f['evidence_ids']))} for p in paragraphs]
            if sum(len(p['text']) for p in compact) > 30000:
                return _failure(row, 'facts_too_large', '已提取的事实较多，暂未生成完整摘要，请阅读原文')
            result = _call(summary_id, 'synthesize', {'title': row.title_snapshot,
                'paragraphs': compact, 'facts': facts, 'attachments_unread': True}, 'synthesis')
            if result is None:
                return _record_dict(db.session.get(AnnouncementSummary, summary_id))
            output = _validate_output(result['output'], paragraphs)
        row = db.session.get(AnnouncementSummary, summary_id)
        ann = _lock_article(row.announcement_id)
        if row.state != 'running' or not _matches(row, ann):
            return _failure(row, 'content_changed', '通知已更新，旧正文的摘要已停用', 'stale')
        row.state, row.summary, row.output = 'succeeded', output['summary'].strip(), output
        row.provenance = result.get('provenance') or {}
        row.skill_version = str(row.provenance.get('skill_version') or '')
        row.completed_at = datetime.utcnow()
        row.error, row.error_code = '', ''
        db.session.commit()  # The task module's commit fence checks lease and generation.
        return _record_dict(row)
    except (tasks.TaskDeferred, tasks.LeaseLost):
        raise
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(AnnouncementSummary, summary_id)
        if getattr(exc, 'code', '') == 'concurrency_limit':
            row.state = 'pending'
            db.session.commit()
            raise tasks.TaskDeferred(phase='summary_resource', delay=3,
                checkpoint={'summary_id': summary_id}, reason='正在等待摘要执行资源') from None
        # No error placeholders enter the successful summary column.
        return _failure(row, getattr(exc, 'code', 'summary_failed'), str(exc))
