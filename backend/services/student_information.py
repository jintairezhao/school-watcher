"""Conservative, resumable student-value assessment with quoted evidence.

No age cutoff, no eligibility inference and no automatic unsubscription. Source
samples guide interpretation; detailed article analysis uses the body on demand.
"""
from datetime import datetime, time
from functools import lru_cache
import re
from zoneinfo import ZoneInfo

from backend.ai.skill_loader import digest, canonical
from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, Department
from backend.database.student_information_models import StudentAssessment
from backend.services import tasks

POLICY = 'student-value-1'
NOTICE_COLUMN = re.compile(r'通知|公告|公示|消息发布|信息发布')
VALUE_LABELS = {'relevant': '与学生需求相关', 'potential': '可能提供间接帮助',
                'mixed': '包含多类信息', 'low': '样本与学生需求关联较少', 'unknown': '价值待判断'}


@lru_cache(maxsize=1)
def policy_digest():
    from backend.ai.skill_loader import load_skill
    return digest([POLICY, load_skill('student-information', 'assess').resource_digest])


def material(kind, ident):
    if kind == 'article':
        subject = db.session.get(Announcement, ident)
        if not subject or not subject.content_text:
            return None
        # Every segment is visited. Budgets apply per task slice, not by silently
        # truncating a long article or declaring unseen material irrelevant.
        text = subject.content_text
        pieces = [text[i:i + 1800] for i in range(0, len(text), 1600)]
        candidate = {'candidate_id': 'subject', 'name': subject.title[:600],
                     'url': subject.url or '', 'kind': 'article', 'path': ''}
    else:
        subject = db.session.get(Department, ident)
        if not subject or not subject.list_selector:
            return None
        query = Announcement.query.filter(db.or_(Announcement.department_id == ident,
            Announcement.source_links.any(AnnouncementSource.department_id == ident)))
        # Recent + older + middle samples; publication age is not a rejection.
        count = query.count()
        positions = sorted({0, 1, 2, count // 2, max(0, count - 2), max(0, count - 1)})
        ordered = query.order_by(Announcement.published_at.desc(), Announcement.id.desc())
        samples = [ordered.offset(p).first() for p in positions if p < count]
        if not samples:
            return None
        pieces = ['标题样本（并非完整正文）：' + a.title + '\n发布时间：' +
                  (a.published_at.isoformat() if a.published_at else '未知') for a in samples]
        candidate = {'candidate_id': 'subject', 'name': subject.name[:600], 'url': subject.list_url or '',
                     'kind': 'notice_source' if NOTICE_COLUMN.search(subject.name) else 'source',
                     'path': (subject.group_name or '')[:600]}
    parts = [{'candidate_id': 'subject', 'evidence_id': 'p' + str(i), 'text': p}
             for i, p in enumerate(pieces)]
    data = {'school_id': subject.school_id, 'candidates': [candidate], 'evidence': parts}
    fingerprint = digest({'policy': policy_digest(), 'data': data})
    return subject, data, fingerprint


def queue_assessment(kind, ident, *, requested_by=None):
    if requested_by is None:
        return None
    from backend.ai.configuration import get_model_binding, AIConfigError
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        return None
    prepared = material(kind, ident)
    if not prepared:
        return None
    subject, _, fingerprint = prepared
    # Changing model configuration permits recovery; reading unchanged material
    # never causes another paid execution.
    key = kind + ':' + str(ident)
    row = db.session.get(StudentAssessment, key)
    if row and row.input_hash == fingerprint and row.state == 'ready':
        return None
    return tasks.enqueue('student_assessment', key + ':' + digest([fingerprint, binding['id'], binding['version']])[:24],
        {'school_id': subject.school_id, 'subject_kind': kind, 'subject_id': ident,
         'input_hash': fingerprint, 'requested_by': requested_by}, capability='directory', replace_finished=True)


def combine(rows):
    values = {r['value'] for r in rows}
    if 'mixed' in values or 'low' in values and values & {'relevant', 'potential'}:
        value = 'mixed'
    elif 'relevant' in values:
        value = 'relevant'
    elif 'potential' in values:
        value = 'potential'
    else:
        value = 'low' if values == {'low'} else 'unknown'
    history = {r['historical'] for r in rows}
    historical = next((v for v in ('high', 'possible') if v in history),
                      'low' if history == {'low'} else 'unknown')
    facts = list({(f['kind'], f['quote']): f for r in rows for f in r['facts']}.values())
    return {'value': value, 'historical': historical, 'facts': facts,
            'eligibility': 'unknown', 'policy_validity': 'unknown'}


def process(payload):
    # Old automatic jobs must not spend tokens after this upgrade.
    if payload.get('requested_by') is None:
        return {'state': 'skipped', 'reason': 'explicit_request_required'}
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.services.discovery_control import pause_if_requested
    pause_if_requested()
    kind, ident = payload['subject_kind'], payload['subject_id']
    prepared = material(kind, ident)
    if not prepared:
        return {'state': 'no_material'}
    subject, evidence, fingerprint = prepared
    if fingerprint != payload['input_hash']:
        return {'state': 'stale'}
    handle = tasks.current_execution() or {}
    checkpoint = dict(handle.get('checkpoint') or {})
    cursor, rows = checkpoint.get('value_cursor', 0), checkpoint.get('value_rows', [])
    # Max three 1800-character pieces stays inside the shared 24 KiB budget.
    batch = dict(evidence, evidence=evidence['evidence'][cursor:cursor + 3])
    while len(canonical(batch).encode()) > 24 * 1024 and len(batch['evidence']) > 1:
        batch['evidence'].pop()
    try:
        binding = get_model_binding('directory')
        execution = 'student-value:' + digest([policy_digest(), batch, binding['id'], binding['version']])
        response = run_skill('student-information', 'assess', batch, 'directory', execution, binding=binding)
    except AIConfigError as exc:
        if exc.code == 'concurrency_limit' and handle:
            tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲', checkpoint=checkpoint)
        return {'state': 'unknown', 'error_code': exc.code}
    if response['status'] == 'pending' and handle:
        tasks.defer(capability='directory', delay=10, reason='等待已提交的价值判断', checkpoint=checkpoint)
    if response['status'] != 'succeeded':
        return {'state': 'unknown', 'error_code': response.get('error_code', '')}
    rows += response['output']['results']
    cursor += len(batch['evidence'])
    if cursor < len(evidence['evidence']):
        checkpoint.update(value_cursor=cursor, value_rows=rows)
        if handle:
            tasks.defer(capability='directory', delay=1, reason='继续检查剩余正文材料', checkpoint=checkpoint)
        return {'state': 'partial'}
    # A network call may have raced with an article update. Never attach an old
    # assessment to new text, and never publish after losing a task lease.
    db.session.expire_all()
    latest = material(kind, ident)
    if not latest or latest[2] != fingerprint:
        return {'state': 'stale'}
    tasks.assert_owned()
    key = kind + ':' + str(ident)
    row = db.session.get(StudentAssessment, key)
    if row is None:
        row = StudentAssessment(subject_key=key, school_id=subject.school_id,
            department_id=ident if kind == 'source' else None,
            announcement_id=ident if kind == 'article' else None)
        db.session.add(row)
    row.input_hash, row.state, row.result = fingerprint, 'ready', combine(rows)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return {'state': 'ready'}


def deadline_view(facts, now=None):
    """Only fully dated, quoted deadlines. A date without time ends that day."""
    deadlines = []
    deadline_facts = [f for f in facts if f['kind'] == 'deadline']
    for fact in facts:
        if fact['kind'] != 'deadline':
            continue
        dates = re.findall(r'(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?', fact['quote'])
        if len(set(dates)) != 1:
            continue  # Missing year, date ranges, or multiple actions need reading.
        try:
            year, month, day = map(int, dates[0])
            clock = re.search(r'(\d{1,2})[:：时点](\d{1,2})(?:分)?', fact['quote'])
            hour = re.search(r'(\d{1,2})[时点]', fact['quote']) if not clock else None
            end = time(int(clock[1]), int(clock[2])) if clock else time(int(hour[1])) if hour else time(23, 59, 59)
            deadlines.append(datetime.combine(datetime(year, month, day).date(), end, ZoneInfo('Asia/Shanghai')))
        except ValueError:
            continue
    if not deadlines:
        return '参与时间待核实'
    if len(deadlines) < len(deadline_facts):
        return '部分参与时间待核实'
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    if len(set(deadlines)) > 1:
        return '含多个截止时间，请分别查看'
    return '文中事项已到截止时间' if deadlines[0] < now else '文中截止时间尚未到'


def article_view(ann):
    row = db.session.get(StudentAssessment, 'article:' + str(ann.id))
    if not row or row.state != 'ready':
        return None
    prepared = material('article', ann.id)
    if not prepared or prepared[2] != row.input_hash:
        return None
    result = dict(row.result)
    result.update(value_label=VALUE_LABELS[result['value']], time_label=deadline_view(result['facts']))
    return result


def source_view(ident):
    from flask import g
    if not hasattr(g, 'student_source_labels'):
        g.student_source_labels = {r.department_id: VALUE_LABELS.get(r.result.get('value'), '价值待判断')
            for r in StudentAssessment.query.filter(StudentAssessment.department_id.isnot(None), StudentAssessment.state == 'ready')}
        g.student_notice_sources = {r.id for r in Department.query.filter(Department.list_selector.isnot(None))
                                    if NOTICE_COLUMN.search(r.name)}
    if ident in g.student_notice_sources:
        return '通知发布栏目，默认完整接收'
    return g.student_source_labels.get(ident, '按官网栏目接收')


def collection_policy(departments, *, manual=False):
    """Value influences actual work; explicit subscriptions always override it.

    Unknown/mixed sources retain normal polling. Only consistently low samples
    without historical preparation value get a bounded, slower recheck.
    """
    from backend.database.models import Subscription
    from backend.services.directory_options import expand_directory_ids
    ids = {d.id for d in departments}
    if not ids or manual:
        return {ident: (0, 1) for ident in ids}
    explicit = set()
    for sub in Subscription.query.filter(Subscription.school_id.in_({d.school_id for d in departments}),
                                         Subscription.department_ids.isnot(None)):
        if sub.department_ids is not None:
            explicit.update(expand_directory_ids(sub.school_id, sub.department_ids))
    assessments = {r.department_id: r.result for r in StudentAssessment.query.filter(
        StudentAssessment.department_id.in_(ids), StudentAssessment.state == 'ready')}
    result = {}
    notice_ids = {d.id for d in departments if NOTICE_COLUMN.search(d.name)}
    for ident in ids:
        if ident in explicit:
            result[ident] = (0, 1)
            continue
        info = assessments.get(ident, {})
        if ident in notice_ids or info.get('value') == 'relevant' or info.get('historical') == 'high':
            result[ident] = (1, 1)
        elif info.get('value') == 'low' and info.get('historical') == 'low':
            result[ident] = (3, 4)
        else:
            result[ident] = (2, 1)
    return result
