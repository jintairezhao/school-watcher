"""Versioned directory work, complete material partitions and bounded dispatch."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json

from bs4 import BeautifulSoup, NavigableString
from backend.database.db import db
from backend.database.source_governance_models import DiscoveryWorkItem

MATERIAL_BYTES = 24 * 1024
MAX_ATTEMPTS = 3
RETRY_DELAYS = (30, 120)
BLOCKING_CODES = {'authentication_failed', 'budget_exhausted', 'not_configured', 'configuration_changed',
                  'configuration_invalid', 'profile_disabled', 'restored_requires_review', 'not_found', 'material_requires_recovery'}


def describe_error(code):
    names = {'connect_timeout': '连接模型服务超时', 'read_timeout': '模型服务连续 120 秒没有返回数据',
        'connection_lost': '模型响应途中断开', 'stream_incomplete': '模型响应未完整结束',
        'response_deadline': '模型调用达到 10 分钟上限', 'incomplete_model_output': '模型输出被截断',
        'invalid_json': '模型返回的格式不完整', 'output_validation_failed': '模型结果未通过格式或证据检查',
        'invalid_stream_event': '模型返回了无法解析的数据片段', 'retry_limit_exhausted': '三次调用均未得到可用结果',
        'authentication_failed': '模型服务密钥验证失败，需要检查配置', 'budget_exhausted': '调用额度不足，已暂停',
        'not_configured': '尚未配置可用模型', 'configuration_changed': '模型配置已变化，需要恢复任务',
        'profile_disabled': '模型配置未启用或需要重新测试', 'network_result_unknown': '网络响应中断，用量仍待核对',
        'interrupted_result_unknown': '程序中断，原调用的结果仍未知', 'material_requires_recovery': '材料无法在单次限制内处理，已保留原材料',
        'department_entry_missing': '官方名录未提供该部门的可访问入口', 'department_entry_unreachable': '该部门主页尚未读取成功',
        'external_ownership_requires_review': '跨域部门的学校归属尚未确认',
        'publication_list_requires_adapter': '栏目尚未通过列表与提取规则检查',
        'publication_heading_requires_review': '发布区域的栏目名称尚未核实',
        'directory_document_requires_adapter': '机构名录以附件发布，尚未解析核对其中的部门',
        'access_verification_required': '该入口需要访问验证，其余公开入口继续检查',
        'roster_reference_changed_or_unavailable': '官方机构名录已变化或尚未读取成功',
        'missing_candidate_result': '模型漏掉了该候选编号'}
    for prefix, message in names.items():
        if str(code).startswith(prefix):
            return message
    return str(code) if any('\u4e00' <= char <= '\u9fff' for char in str(code)) else '该项处理未完成，已保留材料和调用记录'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def fragments(html, *, limit=16 * 1024):
    """Partition every cleaned DOM node; never discard the end of a document."""
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup.select('script,style,iframe,svg,template,noscript'):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.startswith('on') or attr in ('style', 'src', 'srcset'):
                del tag.attrs[attr]
    root = soup.html or soup
    result = []
    def visit(node, ancestors=()):
        raw = str(node)
        if not raw.strip():
            return
        prefix, suffix = '', ''
        for parent in ancestors:
            shell = soup.new_tag(parent.name, attrs=dict(parent.attrs))
            opening = str(shell).split('>', 1)[0] + '>'
            prefix += opening
            suffix = '</' + parent.name + '>' + suffix
        wrapped = prefix + raw + suffix
        if len(wrapped.encode()) <= limit:
            result.append(wrapped)
        elif isinstance(node, NavigableString):
            # A large text node is itself partitioned; UTF-8 characters stay intact.
            allowance = max(128, limit - len((prefix + suffix).encode()))
            piece = ''
            for char in raw:
                if len((piece + char).encode()) > allowance:
                    result.append(prefix + piece + suffix); piece = ''
                piece += char
            if piece:
                result.append(prefix + piece + suffix)
        else:
            children = list(node.children)
            if not children:
                # Keep oversized structural attributes as separate material too.
                visit(NavigableString(raw), ancestors)
            else:
                for child in children:
                    visit(child, (*ancestors, node))
    for child in root.children:
        visit(child)
    packed, group, size = [], [], 0
    for piece in result:
        length = len(piece.encode())
        if group and size + length > limit:
            packed.append(''.join(group)); group, size = [], 0
        group.append(piece); size += length
    if group:
        packed.append(''.join(group))
    return packed or ['<main></main>']


def partition(evidence, mode):
    """Bound the entire serialized request, retaining every material fragment."""
    from backend.ai.skill_loader import canonical
    size = lambda value: len(canonical(value).encode('utf-8'))
    candidate_limit = 1 if mode == 'extraction' else 8
    def explicit_urls(candidates, refs):
        return {url for url in [*(ref.get('url') for ref in refs),
            *(candidate.get('url') or candidate.get('config', {}).get('list_url') for candidate in candidates)]
            if isinstance(url, str) and url.startswith(('http://', 'https://'))}
    if size(evidence) <= MATERIAL_BYTES and len(evidence['candidates']) <= candidate_limit:
        request = deepcopy(evidence)
        request['observed_urls'] = sorted(set(request.get('observed_urls', [])) | explicit_urls(request['candidates'], request['evidence']))
        if size(request) <= MATERIAL_BYTES:
            return [request]
    base = {'school_id': evidence['school_id'], 'entities': evidence.get('entities', [])}
    pieces = []
    for ref in evidence['evidence']:
        if 'html' in ref:
            for index, html in enumerate(fragments(ref['html'], limit=8 * 1024)):
                pieces.append(dict(ref, html=html, fragment_number=index + 1))
        else:
            raw = canonical(ref)
            if len(raw.encode()) <= 10 * 1024:
                pieces.append(ref)
            else:
                for index, text in enumerate(text_fragments(raw)):
                    pieces.append({'evidence_id': ref['evidence_id'], 'text': text, 'fragment_number': index + 1})
    # Supplemental links and diagnostics are material too, not a hidden prefix.
    for field, value in evidence.items():
        if field in ('school_id', 'candidates', 'entities', 'evidence', 'observed_urls'):
            continue
        raw = canonical(value)
        # Feedback and controls accompany the document. They are not separate
        # pages requiring a second opinion about the same column.
        if len(raw.encode()) <= 3 * 1024 and size(dict(base, **{field: value})) <= 6 * 1024:
            base[field] = deepcopy(value)
            continue
        for index, text in enumerate(text_fragments(raw)):
            pieces.append({'evidence_id': 'context-' + field, 'text': text, 'fragment_number': index + 1})
    result = []
    for ref in pieces:
        for offset in range(0, len(evidence['candidates']), candidate_limit):
            candidates = evidence['candidates'][offset:offset + candidate_limit]
            request = dict(base, candidates=candidates, evidence=[ref])
            corpus = canonical(request)
            linked = set()
            if ref.get('html') and ref.get('url'):
                from backend.services.source_inventory import resolve_page_link
                for anchor in BeautifulSoup(ref['html'], 'lxml').select('a[href]'):
                    address = resolve_page_link(ref['url'], anchor['href'])
                    if address:
                        linked.add(address)
            request['observed_urls'] = sorted({url for url in evidence.get('observed_urls', []) if url in corpus or url in linked}
                | explicit_urls(candidates, [ref]))
            # Always retain explicit candidate addresses for evidence-bound operations.
            if size(request) > MATERIAL_BYTES:
                raise ValueError('material_metadata_exceeds_24kib')
            result.append(request)
    return result


def text_fragments(text, limit=8 * 1024):
    result, piece, size = [], [], 0
    for char in text:
        length = len(char.encode('utf-8'))
        if size + length > limit:
            result.append(''.join(piece)); piece, size = [], 0
        piece.append(char); size += length
    if piece:
        result.append(''.join(piece))
    return result or ['']


def ready_to_dispatch(rows):
    now = datetime.utcnow()
    return any(r.state == 'running' or r.state == 'pending' or
               r.state == 'retry_wait' and (not r.next_run_at or r.next_run_at <= now) for r in rows)


def waiting_result(rows):
    runnable = [r for r in rows if r.state in ('pending', 'running', 'retry_wait')]
    delays = [max(1, int((r.next_run_at - datetime.utcnow()).total_seconds())) if r.next_run_at else 1 for r in runnable]
    return {'status': 'pending' if runnable else 'needs_recovery',
        'next_delay': min(delays, default=0), 'error_code': next((r.error_code for r in rows if r.error_code), '')}


def extraction_suggestion(accepted, evidence, *, complete=False):
    """Use actionable evidence now; unrelated fragments do not authorize a verdict."""
    proposed = {fingerprint(row.get('config')): row for row in accepted if row.get('decision') == 'propose'}
    if len(proposed) > 1:
        return None, 'conflicting_fragment_results'
    if proposed:
        return next(iter(proposed.values())), ''
    obtained = {ref.get('url') for ref in evidence.get('evidence', [])}
    attempted = {result.get('url') for result in evidence.get('operation_results', [])}
    known_references = {ref['evidence_id'].removeprefix('reference-') for ref in evidence.get('evidence', [])
                        if ref['evidence_id'].startswith('reference-')}
    actions, owner, identities = [], None, set()
    for row in accepted:
        if row.get('decision') not in ('explore', 'review'):
            continue
        for action in row.get('actions', []):
            if action.get('type') == 'read_page' and action.get('url') in obtained | attempted:
                continue
            if action.get('type') == 'read_reference' and action.get('reference') in known_references:
                continue
            identity = fingerprint([action.get(key) for key in ('type', 'url', 'purpose', 'reference')])
            if identity not in identities:
                actions.append(action); identities.add(identity); owner = owner or row
    if actions:
        result = deepcopy(owner)
        result['decision'] = 'explore'
        result['actions'] = sorted(actions, key=lambda a: a.get('type') != 'read_page')[:3]
        return result, ''
    if complete and accepted:
        priority = {'choose': 0, 'review': 1, 'explore': 2, 'not_column': 3}
        return min(accepted, key=lambda row: priority.get(row.get('decision'), 1)), ''
    return None, ''


def run_material(school_id, generation, group_key, mode, evidence, binding, *, existing_attempts=0):
    batches = partition(evidence, mode)
    groups = [(group_key + ':fragment:' + str(i), request) for i, request in enumerate(batches)]
    for key, request in groups:
        prepare(school_id, generation, key, mode, request)
    reconcile_groups(school_id, generation, group_key + ':fragment:', {key for key, _ in groups})
    if mode == 'extraction':
        saved = DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation, kind='extraction', state='succeeded').filter(
            DiscoveryWorkItem.group_key.in_([key for key, _ in groups])).all()
        suggestion, error = extraction_suggestion([row.result_json for row in saved], evidence)
        if error:
            return {'status': 'needs_recovery', 'error_code': error, 'next_delay': 0}
        if suggestion:
            return {'status': 'partial', 'output': {'proposals': [suggestion]}, 'next_delay': 0,
                    'material_complete': False}
    results = []
    dispatched = False
    for key, request in groups:
        rows = DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation, group_key=key).filter(DiscoveryWorkItem.state != 'superseded').all()
        if all(r.state == 'succeeded' for r in rows):
            name = 'proposals' if mode == 'extraction' else 'results'
            results.append({'status': 'succeeded', 'output': {name: [r.result_json for r in rows]}})
        elif not dispatched and ready_to_dispatch(rows):
            results.append(run_batch(school_id, generation, key, mode, request, binding, existing_attempts=existing_attempts))
            dispatched = True
        else:
            results.append(waiting_result(rows))
    key = 'proposals' if mode == 'extraction' else 'results'
    accepted = [row for result in results for row in (result.get('output') or {}).get(key, [])]
    complete = all(r['status'] == 'succeeded' for r in results)
    if mode == 'extraction':
        suggestion, error = extraction_suggestion(accepted, evidence, complete=complete)
        if error:
            return {'status': 'needs_recovery', 'error_code': 'conflicting_fragment_results', 'next_delay': 0}
        if suggestion:
            return {'status': 'succeeded' if complete else 'partial', 'output': {'proposals': [suggestion]},
                    'material_complete': complete, 'next_delay': 0}
    runnable = [r for r in results if r['status'] == 'pending']
    return {'status': 'succeeded' if complete else 'pending' if runnable else 'needs_recovery',
        'output': {key: accepted}, 'next_delay': min((r.get('next_delay') or 1 for r in runnable), default=0),
        'error_code': next((r.get('error_code') for r in results if r.get('error_code')), '')}


def reconcile_groups(school_id, generation, prefix, active):
    for row in DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation).filter(
            DiscoveryWorkItem.group_key.startswith(prefix)).all():
        if row.group_key not in active:
            row.state = 'superseded'
    db.session.commit()


def resolve_column_material(proposal):
    """Native validation settles the column's fragments and preserves call history."""
    from backend.database.models import BackgroundTask
    from backend.database.source_governance_models import SourceReviewEvent
    parent = BackgroundTask.query.filter_by(identity=f'discover:{proposal.school_id}').first()
    generation = parent.generation if parent else 1
    rows = DiscoveryWorkItem.query.filter_by(school_id=proposal.school_id, generation=generation, kind='extraction').filter(
        DiscoveryWorkItem.group_key.startswith(f'source:{proposal.id}:fragment:'),
        DiscoveryWorkItem.state.notin_(('superseded', 'covered'))).all()
    if not rows:
        return
    history = [{'item_id': row.id, 'previous_state': row.state, 'error_code': row.error_code,
                'execution_id': row.execution_id, 'attempts': row.attempts} for row in rows]
    for row in rows:
        row.state = 'covered'; row.next_run_at = None
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='resolve_column_material', detail_json=json.dumps({
        'basis': 'native_column_validation', 'evidence_hash': proposal.evidence_hash,
        'validated_hash': proposal.validated_hash, 'items': history}, ensure_ascii=False)))


def prepare(school_id, generation, group_key, mode, evidence):
    """Manifest all inputs before dispatch, sharing the counter across repairs."""
    digest = fingerprint(evidence)
    rows = []
    previous = DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation, group_key=group_key).all()
    for candidate in evidence['candidates']:
        identity = fingerprint([school_id, generation, group_key, candidate['candidate_id'], digest])
        row = DiscoveryWorkItem.query.filter_by(identity=identity).first()
        if row is None:
            attempts = max((r.attempts for r in previous if r.candidate_id == candidate['candidate_id']), default=0)
            row = DiscoveryWorkItem(identity=identity, school_id=school_id, generation=generation,
                group_key=group_key, candidate_id=candidate['candidate_id'], kind=mode,
                reference_url=(evidence.get('evidence') or [{}])[0].get('url', ''),
                material_hash=digest, input_json=evidence,
                state='pending' if attempts < MAX_ATTEMPTS else 'needs_recovery', attempts=attempts,
                error_code='' if attempts < MAX_ATTEMPTS else 'retry_limit_exhausted')
            db.session.add(row)
        rows.append(row)
    for old in previous:
        if old.material_hash != digest:
            old.state = 'superseded'
    db.session.commit()
    return rows


def run_batch(school_id, generation, group_key, mode, evidence, binding, *, existing_attempts=0):
    from backend.ai.runtime import run_skill, _result
    from backend.ai.models import AIExecution
    from backend.ai.configuration import AIConfigError
    from backend.ai.skill_loader import SkillValidationError
    rows = prepare(school_id, generation, group_key, mode, evidence)
    now = datetime.utcnow()
    key = 'proposals' if mode == 'extraction' else 'results'
    pending = [r for r in rows if r.state != 'succeeded']
    delay = 0
    active = [r for r in pending if r.state == 'running' and r.execution_id]
    if active:
        execution = db.session.get(AIExecution, active[0].execution_id)
        if execution and execution.status in ('sending', 'reserved'):
            return {'status': 'pending', 'next_delay': 5, 'output': None}
        selected = active
        if execution:
            result = _result(execution)
        else:
            # Reservation always precedes provider I/O. No ledger means the
            # worker exited before dispatch; recover the same immutable ID.
            request = deepcopy(evidence)
            request['candidates'] = [c for c in evidence['candidates'] if c['candidate_id'] in {r.candidate_id for r in selected}]
            request.setdefault('entities', []).extend({'id': r.candidate_id, 'name': r.result_json.get('name') or r.candidate_id}
                for r in rows if r.state == 'succeeded' and r.result_json.get('kind') == 'unit')
            try:
                result = run_skill('university-source-onboarding', mode, request, 'directory', active[0].execution_id,
                    expected_version=binding['version'], binding=binding,
                    repair_feedback=next((r.error_code for r in selected if r.error_code in
                        ('invalid_json', 'output_validation_failed', 'incomplete_model_output')), None))
            except (AIConfigError, SkillValidationError) as exc:
                result = {'status': 'failed', 'error_code': getattr(exc, 'code', 'material_requires_recovery')}
    else:
        selected = [r for r in pending if r.state not in ('needs_recovery', 'paused') and
                    (r.next_run_at is None or r.next_run_at <= now)]
        if selected:
            attempt = max(max(r.attempts, existing_attempts) for r in selected) + 1
            if attempt > MAX_ATTEMPTS:
                for row in selected:
                    row.state = 'needs_recovery'
                    row.attempts = max(row.attempts, existing_attempts)
                    row.error_code = 'retry_limit_exhausted'
                db.session.commit()
                selected = []
            else:
                request = deepcopy(evidence)
                request['candidates'] = [c for c in evidence['candidates'] if c['candidate_id'] in {r.candidate_id for r in selected}]
                # Previously accepted unit rows may be referenced by remaining rows.
                request.setdefault('entities', []).extend({'id': r.candidate_id, 'name': r.result_json.get('name') or r.candidate_id}
                    for r in rows if r.state == 'succeeded' and r.result_json.get('kind') == 'unit')
                execution_id = 'directory:' + fingerprint([group_key, generation, request])[:40] + ':' + str(attempt)
                for row in selected:
                    claimed = db.session.execute(db.update(DiscoveryWorkItem).where(
                        DiscoveryWorkItem.id == row.id, DiscoveryWorkItem.state == row.state,
                        DiscoveryWorkItem.attempts == row.attempts, DiscoveryWorkItem.material_hash == row.material_hash
                    ).values(state='running', attempts=attempt, execution_id=execution_id, next_run_at=None))
                    if not claimed.rowcount:
                        db.session.rollback()
                        return {'status': 'pending', 'next_delay': 5, 'output': None}
                db.session.commit()
                try:
                    result = run_skill('university-source-onboarding', mode, request, 'directory', execution_id,
                        expected_version=binding['version'], binding=binding,
                        repair_feedback=next((r.error_code for r in selected if r.error_code in
                            ('invalid_json', 'output_validation_failed', 'incomplete_model_output')), None))
                except (AIConfigError, SkillValidationError) as exc:
                    for row in selected:
                        row.attempts -= 1
                    result = {'status': 'failed', 'error_code': getattr(exc, 'code', 'material_requires_recovery')}
        if not selected:
            result = None
    if selected and result is not None:
        accepted = {r['candidate_id']: r for r in (result.get('output') or {}).get(key, [])}
        expected = [(r.id, r.execution_id, r.material_hash) for r in selected]
        current_result = False
        for row in selected:
            saved = next(x for x in expected if x[0] == row.id)
            db.session.refresh(row)
            if row.state != 'running' or row.execution_id != saved[1] or row.material_hash != saved[2] or row.material_hash != fingerprint(evidence):
                continue
            current_result = True
            if row.candidate_id in accepted:
                row.result_json, row.state, row.error_code = accepted[row.candidate_id], 'succeeded', ''
            elif result.get('status') == 'pending':
                row.state = 'running'
                delay = 5
            else:
                row.error_code = result.get('error_code') or 'missing_candidate_result'
                if row.error_code in BLOCKING_CODES:
                    row.state = 'paused'
                elif row.attempts >= MAX_ATTEMPTS:
                    row.state = 'needs_recovery'
                else:
                    row.state = 'retry_wait'
                    wait = RETRY_DELAYS[max(0, row.attempts - 1)] if row.attempts else 5
                    row.next_run_at = datetime.utcnow() + timedelta(seconds=wait)
                    delay = max(delay, wait)
        if current_result:
            for gap in (result.get('output') or {}).get('coverage_gaps', []):
                identity = fingerprint([school_id, generation, group_key, 'gap', gap])
                if DiscoveryWorkItem.query.filter_by(identity=identity).first() is None:
                    db.session.add(DiscoveryWorkItem(identity=identity, school_id=school_id, generation=generation,
                        group_key=group_key, candidate_id='gap-' + fingerprint(gap)[:24], kind='coverage_gap',
                        reference_url=(evidence.get('evidence') or [{}])[0].get('url', ''),
                        material_hash=fingerprint(evidence), input_json=gap, state='needs_recovery', attempts=0,
                        error_code=gap.get('reason') or 'model_coverage_gap'))
        db.session.commit()
        if current_result:
            from backend.services import tasks
            handle = tasks.current_execution()
            if handle:
                data = dict(handle.get('checkpoint') or {})
                data['directory_work_saved'] = data.get('directory_work_saved', 0) + 1
                tasks.checkpoint(data)
    complete = all(r.state == 'succeeded' for r in rows)
    output = {key: [r.result_json for r in rows if r.state == 'succeeded']}
    if mode != 'extraction':
        output.update(school_id=evidence['school_id'], coverage_gaps=[])
    runnable = [r for r in rows if r.state in ('pending', 'retry_wait', 'running')]
    if runnable and not delay:
        delay = max(1, min(int((r.next_run_at - now).total_seconds()) if r.next_run_at else 5 for r in runnable))
    return {'status': 'succeeded' if complete else 'pending' if runnable else 'needs_recovery',
            'output': output, 'next_delay': delay, 'error_code': next((r.error_code for r in rows if r.error_code), ''),
            'error_detail': (result or {}).get('error_detail', '')}


def sync_inventory(school_id, report):
    """Persist the native department/page checklist beside model fragments."""
    from backend.database.models import BackgroundTask, School
    parent = BackgroundTask.query.filter_by(identity=f'discover:{school_id}').first()
    generation = parent.generation if parent else 1
    pages = {p['url']: p for p in report.get('pages', [])}
    def save(kind, ident, url, value, state, reason=''):
        identity = fingerprint([school_id, generation, kind, ident])
        row = DiscoveryWorkItem.query.filter_by(identity=identity).first()
        if row is None:
            row = DiscoveryWorkItem(identity=identity, school_id=school_id, generation=generation,
                group_key='inventory:' + kind, candidate_id=fingerprint(ident), kind=kind, reference_url=url,
                attempts=0, input_json=value, material_hash=fingerprint(value))
            db.session.add(row)
        row.input_json, row.material_hash = value, fingerprint(value)
        row.state, row.error_code = state, reason
        row.result_json = value if state == 'succeeded' else None
    for page in pages.values():
        state = {'fetched': 'succeeded', 'reference_only': 'succeeded', 'running': 'running',
                 'failed': 'needs_recovery', 'blocked': 'needs_recovery'}.get(page['state'], 'pending')
        notes = json.loads(page.get('notes_json') or '[]')
        unresolved_notes = [n for n in notes if any(mark in n for mark in ('requires_review', 'requires_adapter', 'requires_browser', 'content_pending'))]
        if state == 'succeeded' and unresolved_notes:
            state = 'needs_recovery'
        save('page', page['url'], page['url'], page, state, page.get('error') or '; '.join(unresolved_notes))
    rosters = {}
    for unit in report.get('official_units', []):
        key = unit.get('node_key') or unit.get('key')
        if not key:
            continue
        url = unit.get('url', '')
        page = pages.get(url)
        unresolved_owner = page and 'external_ownership_requires_review' in (page.get('notes_json') or '')
        state = 'succeeded' if page and page['state'] == 'fetched' and not unresolved_owner else 'pending' if page and page['state'] in ('pending', 'running') else 'needs_recovery'
        save('department', key, url or unit.get('reference_url', ''), unit, state,
             '' if state == 'succeeded' else 'department_entry_unreachable' if url else 'department_entry_missing')
        rosters.setdefault(unit.get('reference_url', ''), []).append(unit)
    for url, entries in rosters.items():
        page = pages.get(url)
        valid = bool(page and page['state'] == 'fetched' and all(
            not u.get('content_hash') or u['content_hash'] == page.get('content_hash') for u in entries))
        save('roster', url, url, {'url': url, 'entries': entries, 'snapshot_hash': (page or {}).get('content_hash')},
             'succeeded' if valid else 'needs_recovery', '' if valid else 'roster_reference_changed_or_unavailable')
    db.session.commit()


def coverage(school_id):
    from backend.database.models import BackgroundTask, School
    from backend.database.source_governance_models import SchoolOnboarding, SourceProposal
    onboarding = db.session.get(SchoolOnboarding, school_id)
    discover = BackgroundTask.query.filter_by(identity=f'discover:{school_id}').first()
    generation = discover.generation if discover else 1
    rows = DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation).filter(DiscoveryWorkItem.state != 'superseded').all()
    scopes = json.loads(onboarding.scope_json or '[]') if onboarding else []
    counts = {state: sum(r.state == state for r in rows) for state in
        ('pending', 'running', 'retry_wait', 'paused', 'needs_recovery', 'succeeded', 'covered')}
    proposals = SourceProposal.query.filter_by(school_id=school_id).all()
    proposal_ids = {p.id for p in proposals}
    active_tasks = [t for t in BackgroundTask.query.filter(BackgroundTask.state.in_(('pending', 'running'))).all()
        if (t.kind in ('discover', 'directory', 'navigation_review', 'source_grouping') and t.payload.get('school_id') == school_id
            and t.payload.get('school_generation', t.payload.get('parent_generation', generation)) == generation)
        or (t.kind == 'source_review' and t.payload.get('proposal_id') in proposal_ids)]
    from backend.services.source_inventory import canonical_url
    proposed_urls = {canonical_url(json.loads(p.candidate_json).get('list_url', '')) for p in proposals}
    missing_columns = []
    missing_directories = []
    processed_pages = {canonical_url(r.reference_url) for r in rows if r.kind == 'page' and r.state == 'succeeded' and r.input_json.get('state') == 'fetched'}
    known_units = {canonical_url(r.reference_url) for r in rows if r.kind == 'department'}
    school = db.session.get(School, school_id)
    root_url = canonical_url(school.url) if school else ''
    suggested_units, missing_units = {}, []
    for row in rows:
        result = row.result_json or {}
        candidate = next((c for c in row.input_json.get('candidates', []) if c['candidate_id'] == row.candidate_id), {})
        if row.kind == 'classify' and result.get('kind') == 'directory' and result.get('decision') == 'propose' and canonical_url(candidate.get('url', '')) not in processed_pages:
            missing_directories.append((row, candidate.get('url', row.reference_url)))
        if row.kind == 'classify' and result.get('kind') == 'unit' and result.get('decision') == 'propose':
            address = canonical_url(candidate.get('url', ''))
            if address not in known_units and address != root_url:
                suggested_units[(address, result.get('name') or candidate.get('name'))] = result
                missing_units.append((row, candidate.get('url', row.reference_url)))
        if row.kind != 'classify' or result.get('kind') != 'channel' or result.get('decision') != 'propose':
            continue
        if canonical_url(candidate.get('url', '')) not in proposed_urls:
            missing_columns.append((row, candidate.get('url', row.reference_url)))
    unresolved = [p for p in proposals if p.state not in ('activated', 'not_applicable', 'rejected', 'superseded')]
    resolved_proposals = {p.id for p in proposals if p.state in ('activated', 'not_applicable', 'rejected', 'superseded')}
    def fragment_resolved(row):
        parts = row.group_key.split(':', 2)
        return row.kind == 'extraction' and len(parts) >= 2 and parts[0] == 'source' and parts[1].isdigit() and int(parts[1]) in resolved_proposals
    rosters = [r for r in rows if r.kind == 'roster']
    roster_confirmed = bool(rosters and all(r.state == 'succeeded' for r in rosters))
    semantic_gaps = [r for r in rows if r.kind in ('classify', 'extraction') and r.state == 'succeeded' and
        (r.result_json or {}).get('decision') in ('review', 'explore', 'choose') and not fragment_resolved(r)]
    modeled_pages = {r.reference_url for r in rows if r.kind == 'classify'}
    missing_fragments = [r for r in rows if r.kind == 'page' and r.state == 'succeeded' and
        r.input_json.get('state') == 'fetched' and r.input_json.get('kind') != 'article' and r.reference_url not in modeled_pages]
    changed_scopes = [s for s in scopes if s.get('reference_changed')]
    failed_pages = sum(json.loads(onboarding.checkpoint_json or '{}').get('states', {}).get(s, 0)
        for s in ('failed', 'blocked')) if onboarding else 0
    from backend.services.source_grouping import placement_gaps
    grouping_gaps = placement_gaps(school_id)
    complete = bool(onboarding and roster_confirmed and rows and not grouping_gaps and not missing_units and not missing_columns and not missing_directories and not missing_fragments and not changed_scopes and not semantic_gaps and not unresolved and not failed_pages and
        not onboarding.pending_pages and counts['succeeded'] + counts['covered'] == len(rows) and
        discover and discover.state == 'done' and not active_tasks)
    gaps = [{'item_id': r.id, 'url': r.reference_url, 'candidate_id': r.candidate_id,
             'state': r.state, 'reason': describe_error(r.error_code), 'error_code': r.error_code, 'attempts': r.attempts}
            for r in rows if r.state in ('paused', 'needs_recovery')]
    gaps.extend({'item_id': r.id, 'url': r.reference_url, 'candidate_id': r.candidate_id,
        'state': 'evidence_missing', 'reason': (r.result_json or {}).get('reason') or '识别结果仍需补充材料'} for r in semantic_gaps)
    gaps.extend({'proposal_id': p.id, 'url': json.loads(p.candidate_json).get('list_url', ''),
        'state': 'validation_pending', 'reason': (json.loads(p.validation_json).get('workflow') or {}).get('reason') or '栏目尚未通过网页与归属验证'} for p in unresolved)
    gaps.extend({'item_id': r.id, 'url': r.reference_url, 'state': 'material_unprocessed',
        'reason': '页面材料尚未逐片处理'} for r in missing_fragments)
    gaps.extend({'url': s.get('reference_url', ''), 'state': 'reference_changed',
        'reason': '部门名录已变化，原覆盖结论需要重新核对'} for s in changed_scopes)
    gaps.extend({'item_id': r.id, 'url': url, 'state': 'column_validation_missing',
        'reason': '已识别栏目尚未进入网页与提取规则验证'} for r, url in missing_columns)
    gaps.extend({'item_id': r.id, 'url': url, 'state': 'department_validation_missing',
        'reason': '已识别部门尚未通过官方名录与学校归属核对'} for r, url in missing_units)
    gaps.extend({'item_id': r.id, 'url': url, 'state': 'directory_material_unprocessed',
        'reason': '已识别的机构目录入口尚未读取和处理'} for r, url in missing_directories)
    if not roster_confirmed:
        gaps.append({'state': 'roster_unverified', 'reason': '官方机构名录范围尚未完成核对'})
    if active_tasks:
        gaps.append({'state': 'processing', 'reason': '范围内仍有入口或栏目正在处理'})
    gaps.extend(grouping_gaps)
    return {'state': 'complete' if complete else 'incomplete', 'complete': complete,
            'department_count': len({(canonical_url(s.get('url', '')), s.get('name')) for s in scopes} | set(suggested_units)), 'work_counts': counts, 'work_total': len(rows),
            'active_task_count': len(active_tasks),
            'ai_running_count': sum(r.state == 'running' and r.kind in ('classify', 'extraction') for r in rows),
            'failed_count': counts['needs_recovery'], 'unresolved_count': len(unresolved) + len(semantic_gaps) + len(grouping_gaps),
            'placement_gap_count': len(grouping_gaps),
            'waiting_recovery_count': counts['needs_recovery'] + counts['paused'], 'gaps': gaps}
