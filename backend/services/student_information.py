"""Bounded automatic value hints and explicit, resumable article assessment.

No age cutoff, no eligibility inference and no automatic unsubscription. Source
samples guide interpretation; detailed article analysis uses the body on demand.
"""
from datetime import datetime, time
from functools import lru_cache
import re
from zoneinfo import ZoneInfo
from sqlalchemy.orm import load_only

from backend.ai.skill_loader import digest, canonical
from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, BackgroundTask, Department, School, Subscription
from backend.database.student_information_models import StudentAssessment
from backend.services import tasks

POLICY = 'student-value-2'
SOURCE_SAMPLES = 6
LISTING_BATCH_SIZE = 8
LISTING_WINDOW = 24
NOTICE_COLUMN = re.compile(r'通知|公告|公示|消息发布|信息发布')
VALUE_LABELS = {'relevant': '与学生需求相关', 'potential': '可能提供间接帮助',
                'mixed': '包含多类信息', 'low': '样本与学生需求关联较少', 'unknown': '价值待判断'}


@lru_cache(maxsize=1)
def policy_digest():
    from backend.ai.skill_loader import load_skill
    return digest([POLICY, load_skill('student-information', 'assess').resource_digest])


def _source_announcements(ident, school_id):
    return Announcement.query.filter(Announcement.school_id == school_id, db.or_(Announcement.department_id == ident,
        Announcement.source_links.any(AnnouncementSource.department_id == ident)))


def _titles_only(query):
    return query.options(load_only(Announcement.id, Announcement.school_id, Announcement.department_id,
                                   Announcement.title, Announcement.url, Announcement.published_at))


def _school_active(ident):
    school = db.session.get(School, ident)
    return bool(school and school.is_effectively_active())


def _source_active(source):
    """Respect the current subscription scope and a persisted school AI opt-out."""
    if (not source or not source.list_selector or not source.list_url
            or source.kind in ('unit', 'group') or source.name.upper() == 'DAILY NEWS'
            or not _school_active(source.school_id)):
        return False
    discovery = BackgroundTask.query.with_entities(BackgroundTask.payload).filter_by(
        identity='discover:' + str(source.school_id)).first()
    if discovery and discovery[0].get('ai_assist') is False:
        return False
    from backend.services.directory_options import directory_entries_for, expand_directory_ids
    entries = directory_entries_for(source.school_id)
    if any(entry.parent_id == source.id for entry in entries):
        return False
    scopes = Subscription.query.with_entities(Subscription.department_ids).filter_by(school_id=source.school_id)
    return any(ids is None or source.id in expand_directory_ids(source.school_id, ids, entries) for ids, in scopes)


def _fingerprint(data):
    return digest({'policy': policy_digest(), 'data': data})


def _listing_material(ann, source):
    """Titles alone establish relevance, never eligibility or a deadline."""
    ident = 'listing:' + str(ann.id)
    title = ann.title[:600]
    data = {'school_id': ann.school_id, 'candidates': [{
        'candidate_id': ident, 'name': title, 'url': (ann.url or '')[:2000],
        'kind': 'listing', 'path': ((source.group_name or '') + ' / ' + source.name)[:600]}],
        'evidence': [{'candidate_id': ident, 'evidence_id': ident + ':title',
            'text': '标题（未读取正文）：' + title + '\n发布日期：' +
                    (ann.published_at.isoformat() if ann.published_at else '未知') +
                    '\n发布栏目：' + source.name[:200]}]}
    return ann, data, _fingerprint(data)


def material(kind, ident):
    if kind == 'listing':
        ann = _titles_only(Announcement.query).filter_by(id=ident).first()
        source = db.session.get(Department, ann.department_id) if ann else None
        return _listing_material(ann, source) if ann and source else None
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
    elif kind == 'source':
        subject = db.session.get(Department, ident)
        if not subject or not subject.list_selector:
            return None
        query = _source_announcements(ident, subject.school_id)
        # Recent + older + middle samples; publication age is not a rejection.
        count = query.count()
        positions = sorted({0, 1, 2, count // 2, max(0, count - 2), max(0, count - 1)})
        ordered = query.with_entities(Announcement.title, Announcement.published_at).order_by(
            Announcement.published_at.desc(), Announcement.id.desc())
        samples = [ordered.offset(p).first() for p in positions if p < count]
        if not samples:
            return None
        pieces = ['标题样本（并非完整正文）：' + a.title[:600] + '\n发布时间：' +
                  (a.published_at.isoformat() if a.published_at else '未知') for a in samples]
        candidate = {'candidate_id': 'subject', 'name': subject.name[:600], 'url': subject.list_url or '',
                     'kind': 'notice_source' if NOTICE_COLUMN.search(subject.name) else 'source',
                     'path': (subject.group_name or '')[:600]}
    else:
        return None
    parts = [{'candidate_id': 'subject', 'evidence_id': 'p' + str(i), 'text': p}
             for i, p in enumerate(pieces)]
    data = {'school_id': subject.school_id, 'candidates': [candidate], 'evidence': parts}
    return subject, data, _fingerprint(data)


def _binding_ref(binding):
    return {'id': binding['id'], 'version': binding['version']}


def _enabled_binding():
    from backend.ai.configuration import get_model_binding, AIConfigError
    try:
        return get_model_binding('directory')
    except AIConfigError:
        return None


def _matches(row, fingerprint, binding):
    return bool(row and row.state == 'ready' and row.input_hash == fingerprint
                and binding and row.result.get('_binding') == _binding_ref(binding))


def _reopen_automatic(key):
    prior = BackgroundTask.query.filter_by(identity='student_assessment:' + key).first()
    return bool(prior and prior.state in ('done', 'failed')
                and (prior.result or {}).get('state') in ('skipped', 'stale', 'no_material'))


def queue_assessment(kind, ident, *, requested_by=None, _automatic_source=False):
    if requested_by is None and not (kind == 'source' and _automatic_source):
        return None
    binding = _enabled_binding()
    if not binding:
        return None
    prepared = material(kind, ident)
    if not prepared:
        return None
    subject, _, fingerprint = prepared
    if _automatic_source and not _source_active(subject):
        return None
    # Changing model configuration permits recovery; reading unchanged material
    # never causes another paid execution.
    key = kind + ':' + str(ident)
    row = db.session.get(StudentAssessment, key)
    if _matches(row, fingerprint, binding):
        return None
    job_key = key + ':' + digest([fingerprint, binding['id'], binding['version']])[:24]
    return tasks.enqueue('student_assessment', job_key,
        {'school_id': subject.school_id, 'subject_kind': kind, 'subject_id': ident,
         'input_hash': fingerprint, 'requested_by': requested_by, 'binding': _binding_ref(binding),
         'automatic_source': _automatic_source}, capability='directory',
         replace_finished=not _automatic_source or _reopen_automatic(job_key))


def queue_source_assessment(ident, *, ai_assist=True):
    """One call over six existing samples; opt-out and disabled AI cost nothing."""
    if not ai_assist:
        return None
    return queue_assessment('source', ident, _automatic_source=True)


def queue_listing_assessment(ident, *, ai_assist=True, _remaining_batches=2):
    """Assess the newest 24 known notices in at most three eight-title calls.

    A completed batch may enqueue the next slice. Reads never schedule work, and
    old archives do not start an unbounded automatic article-analysis pipeline.
    """
    if not ai_assist:
        return None
    binding = _enabled_binding()
    source = db.session.get(Department, ident) if binding else None
    if not _source_active(source):
        return None
    notices = _titles_only(_source_announcements(ident, source.school_id)).order_by(
        Announcement.published_at.desc(), Announcement.id.desc()).limit(LISTING_WINDOW).all()
    if not notices:
        return None
    rows = {row.announcement_id: row for row in StudentAssessment.query.filter(
        StudentAssessment.subject_key.in_(['listing:' + str(a.id) for a in notices]))}
    sources = {d.id: d for d in Department.query.filter(Department.id.in_({a.department_id for a in notices}))}
    pending = []
    for ann in notices:
        prepared = _listing_material(ann, sources[ann.department_id])
        if not _matches(rows.get(ann.id), prepared[2], binding):
            pending.append(prepared)
        if len(pending) == LISTING_BATCH_SIZE:
            break
    if not pending:
        return None
    data = _listing_batch(pending)
    # Unusually long UTF-8 URLs or titles still stay inside the shared budget.
    while len(canonical(data).encode()) > 24 * 1024:
        pending.pop()
        if not pending:
            return None
        data = _listing_batch(pending)
    fingerprint = _fingerprint(data)
    job_key = 'listing:' + str(ident) + ':' + digest([fingerprint, _binding_ref(binding)])[:24]
    return tasks.enqueue('student_assessment', job_key, {
            'school_id': source.school_id, 'subject_kind': 'listing_batch', 'subject_id': ident,
            'announcement_ids': [row[0].id for row in pending], 'input_hash': fingerprint,
            'binding': _binding_ref(binding), 'automatic_listing': True,
            'remaining_batches': max(0, min(2, _remaining_batches))},
        capability='directory', replace_finished=_reopen_automatic(job_key))


def _listing_batch(prepared):
    return {'school_id': prepared[0][0].school_id,
            'candidates': [p[1]['candidates'][0] for p in prepared],
            'evidence': [p[1]['evidence'][0] for p in prepared]}


def _listing_payload_material(payload):
    ids = payload.get('announcement_ids', [])
    if not ids or len(ids) > LISTING_BATCH_SIZE or len(set(ids)) != len(ids):
        return None
    source = db.session.get(Department, payload['subject_id'])
    if not source or not source.list_selector or source.school_id != payload['school_id']:
        return None
    notices = _titles_only(_source_announcements(source.id, source.school_id)).filter(Announcement.id.in_(ids)).all()
    if len(notices) != len(ids):
        return None
    by_id = {ann.id: ann for ann in notices}
    sources = {d.id: d for d in Department.query.filter(Department.id.in_({a.department_id for a in notices}))}
    if any(ann.department_id not in sources for ann in notices):
        return None
    prepared = [_listing_material(by_id[ident], sources[by_id[ident].department_id]) for ident in ids]
    data = _listing_batch(prepared)
    return prepared, data, _fingerprint(data)


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
    automatic = ((payload.get('subject_kind') == 'source' and payload.get('automatic_source') is True)
                 or (payload.get('subject_kind') == 'listing_batch' and payload.get('automatic_listing') is True))
    # Only the bounded automatic entry points opt in. Legacy jobs and ordinary
    # article reads cannot silently turn into paid full-body analysis.
    if payload.get('requested_by') is None and not automatic:
        return {'state': 'skipped', 'reason': 'explicit_request_required'}
    if automatic and not _school_active(payload.get('school_id')):
        return {'state': 'skipped', 'reason': 'school_inactive'}
    if automatic and not _source_active(db.session.get(Department, payload.get('subject_id'))):
        return {'state': 'skipped', 'reason': 'source_not_selected'}
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.services.discovery_control import pause_if_requested
    pause_if_requested()
    if payload['subject_kind'] == 'listing_batch':
        return _process_listing(payload)
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
    # Six short source titles fit in one call; long requested articles retain
    # the resumable three-piece body analysis.
    size = SOURCE_SAMPLES if kind == 'source' else 3
    batch = dict(evidence, evidence=evidence['evidence'][cursor:cursor + size])
    while len(canonical(batch).encode()) > 24 * 1024 and len(batch['evidence']) > 1:
        batch['evidence'].pop()
    try:
        binding = get_model_binding('directory')
        if payload.get('binding') and payload['binding'] != _binding_ref(binding):
            return {'state': 'stale', 'reason': 'model_changed'}
        if _matches(db.session.get(StudentAssessment, kind + ':' + str(ident)), fingerprint, binding):
            return {'state': 'ready'}
        execution = 'student-value:' + digest([policy_digest(), batch, binding['id'], binding['version']])
        response = run_skill('student-information', 'assess', batch, 'directory', execution, binding=binding)
    except AIConfigError as exc:
        if exc.code == 'concurrency_limit' and handle:
            tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲', checkpoint=checkpoint)
        if automatic and exc.code in ('not_configured', 'profile_disabled', 'restored_requires_review'):
            return {'state': 'skipped', 'reason': 'ai_disabled', 'error_code': exc.code}
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
    if automatic and not _source_active(latest[0]):
        return {'state': 'skipped', 'reason': 'source_not_selected'}
    current_binding = _enabled_binding()
    if not current_binding or _binding_ref(current_binding) != _binding_ref(binding):
        return {'state': 'stale', 'reason': 'model_changed'}
    tasks.assert_owned()
    key = kind + ':' + str(ident)
    row = db.session.get(StudentAssessment, key)
    if row is None:
        row = StudentAssessment(subject_key=key, school_id=subject.school_id,
            department_id=ident if kind == 'source' else None,
            announcement_id=ident if kind == 'article' else None)
        db.session.add(row)
    row.input_hash, row.state, row.result = fingerprint, 'ready', dict(combine(rows), _binding=_binding_ref(binding))
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return {'state': 'ready'}


def _process_listing(payload):
    from backend.ai.runtime import run_skill
    from backend.ai.configuration import AIConfigError
    prepared = _listing_payload_material(payload)
    if not prepared:
        return {'state': 'no_material'}
    items, evidence, fingerprint = prepared
    if fingerprint != payload.get('input_hash'):
        return {'state': 'stale'}
    binding = _enabled_binding()
    if not binding:
        return {'state': 'skipped', 'reason': 'ai_disabled'}
    if payload.get('binding') != _binding_ref(binding):
        return {'state': 'stale', 'reason': 'model_changed'}
    if all(_matches(db.session.get(StudentAssessment, 'listing:' + str(item[0].id)), item[2], binding)
           for item in items):
        return {'state': 'ready'}
    handle = tasks.current_execution() or {}
    try:
        execution = 'student-listing:' + digest([policy_digest(), evidence, _binding_ref(binding)])
        response = run_skill('student-information', 'assess', evidence, 'directory', execution, binding=binding)
    except AIConfigError as exc:
        if exc.code == 'concurrency_limit' and handle:
            tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲')
        return {'state': 'unknown', 'error_code': exc.code}
    if response['status'] == 'pending' and handle:
        tasks.defer(capability='directory', delay=10, reason='等待已提交的信息价值判断')
    if response['status'] != 'succeeded':
        return {'state': 'unknown', 'error_code': response.get('error_code', '')}
    db.session.expire_all()
    latest = _listing_payload_material(payload)
    current_binding = _enabled_binding()
    if not latest or latest[2] != fingerprint:
        return {'state': 'stale'}
    if not _source_active(db.session.get(Department, payload['subject_id'])):
        return {'state': 'skipped', 'reason': 'source_not_selected'}
    if not current_binding or _binding_ref(current_binding) != _binding_ref(binding):
        return {'state': 'stale', 'reason': 'model_changed'}
    tasks.assert_owned()
    results = {row['candidate_id']: row for row in response['output']['results']}
    for ann, _, input_hash in latest[0]:
        key = 'listing:' + str(ann.id)
        assessed = results[key]
        result = combine([assessed])
        # The title preview never presents a date, eligibility or policy claim
        # as a fact derived from a body that has not been read.
        result['facts'] = [f for f in result['facts'] if f['kind'] in {'relevance', 'audience', 'history'}]
        if not result['facts']:
            result.update(value='unknown', historical='unknown')
        result.update(reason=assessed['reason'] if result['facts'] else '标题信息有限，已保留原文',
                      _binding=_binding_ref(binding), basis='title')
        row = db.session.get(StudentAssessment, key)
        if row is None:
            row = StudentAssessment(subject_key=key, school_id=ann.school_id, announcement_id=ann.id)
            db.session.add(row)
        row.input_hash, row.state, row.result = input_hash, 'ready', result
        row.updated_at = datetime.utcnow()
    db.session.commit()
    remaining = max(0, min(2, payload.get('remaining_batches', 0)))
    if remaining:
        queue_listing_assessment(payload['subject_id'], _remaining_batches=remaining - 1)
    return {'state': 'ready', 'assessed_count': len(latest[0])}


def listing_views(announcements):
    """Read verified title hints in bulk; reading never queues or runs AI."""
    notices = list(announcements)
    binding = _enabled_binding() if notices else None
    if not binding:
        return {}
    sources = {d.id: d for d in Department.query.filter(Department.id.in_({a.department_id for a in notices}))}
    rows = {row.announcement_id: row for row in StudentAssessment.query.filter(
        StudentAssessment.subject_key.in_(['listing:' + str(a.id) for a in notices]))}
    result = {}
    for ann in notices:
        source, row = sources.get(ann.department_id), rows.get(ann.id)
        if source and _matches(row, _listing_material(ann, source)[2], binding):
            info = {key: val for key, val in row.result.items() if not key.startswith('_')}
            info['value_label'] = ('与学生需求关联较少' if info['value'] == 'low'
                                   else VALUE_LABELS.get(info['value'], VALUE_LABELS['unknown']))
            result[ann.id] = info
    return result


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
    if row.result.get('_binding') and not _matches(row, prepared[2], _enabled_binding()):
        return None
    result = {key: val for key, val in row.result.items() if not key.startswith('_')}
    result.update(value_label=VALUE_LABELS[result['value']], time_label=deadline_view(result['facts']))
    return result


def source_view(ident):
    source = db.session.get(Department, ident)
    row = db.session.get(StudentAssessment, 'source:' + str(ident))
    binding = _enabled_binding() if row else None
    prepared = material('source', ident) if binding else None
    if prepared and _matches(row, prepared[2], binding):
        return VALUE_LABELS.get(row.result.get('value'), VALUE_LABELS['unknown'])
    if source and source.list_selector and NOTICE_COLUMN.search(source.name):
        return '官网通知发布栏目，已保留原文'
    return '按官网栏目接收'


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
    assessments = {r.department_id: r for r in StudentAssessment.query.filter(
        StudentAssessment.department_id.in_(ids), StudentAssessment.state == 'ready')}
    binding = _enabled_binding() if assessments else None
    result = {}
    notice_ids = {d.id for d in departments if NOTICE_COLUMN.search(d.name)}
    for ident in ids:
        if ident in explicit:
            result[ident] = (0, 1)
            continue
        row = assessments.get(ident)
        prepared = material('source', ident) if row and binding else None
        info = row.result if prepared and _matches(row, prepared[2], binding) else {}
        if ident in notice_ids or info.get('value') == 'relevant' or info.get('historical') == 'high':
            result[ident] = (1, 1)
        elif info.get('value') == 'low' and info.get('historical') == 'low':
            result[ident] = (3, 4)
        else:
            result[ident] = (2, 1)
    return result
