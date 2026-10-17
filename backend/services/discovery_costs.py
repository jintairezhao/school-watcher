"""Attribute existing discovery invoices without reading model input/output.

Only task metadata committed before dispatch may identify a page or branch.
Reservations and unknown results remain separate from confirmed provider usage.
No helper here creates a task, budget round, model request or source record.
"""
import json
import math

from flask import current_app, has_app_context

from backend.services.source_inventory import canonical_url


DISCOVERY_TASKS = frozenset(('discover', 'directory', 'navigation_review', 'onboard', 'source_review',
                           'student_assessment'))


def _url(value):
    if not isinstance(value, str):
        return ''
    try:
        return canonical_url(value)
    except (ValueError, UnicodeError):
        return ''


def _ident(value):
    return type(value) is int and value > 0


def capture_discovery_context(handle, school_id, budget_key):
    """Capture exact durable exploration metadata, not guesses from a prompt."""
    if not handle or not _ident(school_id) or not _ident(handle.get('id')):
        return {}
    from sqlalchemy import select
    from backend.database.db import db
    from backend.database.models import BackgroundTask, Department
    from backend.database.source_governance_models import SourceProposal
    with db.session.no_autoflush:
        row = db.session.execute(select(BackgroundTask.id, BackgroundTask.generation,
            BackgroundTask.kind, BackgroundTask.payload, BackgroundTask.checkpoint).where(
                BackgroundTask.id == handle['id'])).first()
        if not row or row.generation != handle.get('generation') or row.kind not in DISCOVERY_TASKS:
            return {}
        payload, checkpoint = row.payload or {}, row.checkpoint or {}
        if not isinstance(payload, dict) or not isinstance(checkpoint, dict):
            return {}
        if payload.get('school_id') not in (None, school_id):
            return {}
        page = payload.get('page') if isinstance(payload.get('page'), dict) else {}
        snapshot = payload.get('snapshot') if isinstance(payload.get('snapshot'), dict) else {}
        page_url = _url(page.get('url') or payload.get('url'))
        snapshot_url = _url(page.get('snapshot_url') or snapshot.get('url'))
        source_id = payload.get('department_id') or payload.get('repair_department_id')
        if row.kind == 'student_assessment':
            automatic = ((payload.get('subject_kind') == 'source' and payload.get('automatic_source') is True)
                         or (payload.get('subject_kind') == 'listing_batch' and payload.get('automatic_listing') is True))
            if not automatic or not _ident(payload.get('subject_id')):
                return {}
            # These optional value judgments share the exploration allowance.
            # Attribute the durable subject source, not article text or a name.
            source_id = payload['subject_id']
        if _ident(payload.get('proposal_id')):
            proposal = db.session.execute(select(SourceProposal.school_id, SourceProposal.department_id,
                SourceProposal.candidate_json).where(
                SourceProposal.id == payload['proposal_id'])).first()
            if not proposal or proposal.school_id != school_id:
                return {}
            source_id = source_id or proposal.department_id
            try:
                config = json.loads(proposal.candidate_json)
            except (ValueError, TypeError):
                config = {}
            if isinstance(config, dict):
                page_url = page_url or _url(config.get('list_url'))
        context = {'task_id': row.id, 'generation': row.generation, 'task_kind': row.kind,
                   'school_id': school_id, 'school_budget_key': budget_key}
        if _ident(source_id):
            source = db.session.execute(select(Department.school_id, Department.list_url).where(
                Department.id == source_id)).first()
            if source and source.school_id == school_id:
                context['source_id'] = source_id
                page_url = page_url or _url(source.list_url)
        if page_url:
            context['page_url'] = page_url
        if snapshot_url:
            context['snapshot_url'] = snapshot_url
        branch_url = _url(checkpoint.get('navigation_branch_url'))
        if branch_url:
            context['branch_url'] = branch_url
        return context


def preserve_discovery_context(previous, diagnostics):
    """Replace provider progress fields; preserve only our immutable context."""
    fresh = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    fresh.pop('discovery_context', None)
    context = (previous or {}).get('discovery_context') if isinstance(previous, dict) else None
    if isinstance(context, dict):
        fresh['discovery_context'] = dict(context)
    return fresh


def _bucket():
    return {'execution_count': 0, 'actual_requests': 0, 'confirmed_requests': 0,
            'confirmed_tokens': 0, 'reserved_requests': 0, 'reserved_tokens': 0,
            'unknown_requests': 0, 'reconciled_requests': 0, 'reconciled_tokens': 0,
            'provider_active_seconds': 0.0, 'timed_requests': 0, 'untimed_requests': 0}


def _add(bucket, row):
    bucket['execution_count'] += 1
    reserve = row.reserved_tokens if type(row.reserved_tokens) is int and row.reserved_tokens > 0 else 0
    bucket['reserved_tokens'] += reserve
    if not row.request_sent:
        bucket['reserved_requests'] += int(row.status in ('reserved', 'sending'))
        return
    bucket['actual_requests'] += 1
    seconds = (row.diagnostics or {}).get('elapsed_seconds') if isinstance(row.diagnostics, dict) else None
    if (row.status in ('succeeded', 'failed') and type(seconds) in (int, float)
            and math.isfinite(seconds) and seconds >= 0):
        bucket['provider_active_seconds'] += seconds
        bucket['timed_requests'] += 1
    else:
        bucket['untimed_requests'] += 1
    usage = row.usage if isinstance(row.usage, dict) else {}
    tokens = usage.get('total_tokens')
    if usage.get('known') is True and type(tokens) is int and tokens >= 0:
        if usage.get('source') == 'administrator_reconciliation':
            bucket['reconciled_requests'] += 1
            bucket['reconciled_tokens'] += tokens
        else:
            bucket['confirmed_requests'] += 1
            bucket['confirmed_tokens'] += tokens
    else:
        bucket['unknown_requests'] += 1


def load_discovery_costs(site):
    """Read current school-round billing metadata, with explicit attribution gaps."""
    result = {'school_id': None, 'round_key': None, 'scope': 'unavailable',
              'totals': _bucket(), 'by_page': {}, 'by_branch': {}, 'unattributed': _bucket(),
              'unattributed_branch': _bucket(),
              'attribution_complete': False, 'ledger_gap': None,
              'elapsed_seconds': None, 'elapsed_state': 'unknown'}
    if not has_app_context() or 'sqlalchemy' not in current_app.extensions or not isinstance(site, dict):
        return result
    from sqlalchemy import select, cast, Text
    from backend.database.db import db
    from backend.database.models import School
    from backend.ai.models import AIExecution, AISchoolRound, AIBudget
    root = _url(site.get('root_url'))
    if not root or not isinstance(site.get('name'), str):
        return result
    with db.session.no_autoflush:
        schools = [row for row in db.session.execute(select(School.id, School.url).where(
            School.name == site['name'])) if _url(row.url) == root]
        if len(schools) != 1:
            return result
        school_id = schools[0].id
        round_key = db.session.execute(select(AISchoolRound.budget_key).where(
            AISchoolRound.school_id == school_id)).scalar_one_or_none()
        result.update(school_id=school_id, round_key=round_key,
                      scope='current_school_round' if round_key else 'recorded_school_history')
        query = select(AIExecution.execution_id, AIExecution.status, AIExecution.usage,
            AIExecution.reserved_tokens, AIExecution.request_sent, AIExecution.diagnostics,
            AIExecution.budget_keys).where(AIExecution.purpose == 'directory')
        marker = '"' + (round_key if round_key else 'school-discovery:' + str(school_id) + ':')
        if round_key:
            marker += '"'
        query = query.where(cast(AIExecution.budget_keys, Text).contains(marker, autoescape=True))
        seen = set()
        for row in db.session.execute(query):
            if row.execution_id in seen:
                continue
            seen.add(row.execution_id)
            keys = row.budget_keys if isinstance(row.budget_keys, list) else []
            matching_keys = [key for key in keys if isinstance(key, str)
                             and (key == round_key if round_key else key.startswith(
                                 'school-discovery:' + str(school_id) + ':'))]
            if not matching_keys:
                continue
            _add(result['totals'], row)
            diagnostics = row.diagnostics if isinstance(row.diagnostics, dict) else {}
            context = diagnostics.get('discovery_context')
            exact = (isinstance(context, dict) and context.get('school_id') == school_id
                     and context.get('school_budget_key') in matching_keys
                     and context.get('task_kind') in DISCOVERY_TASKS
                     and _ident(context.get('task_id')) and _ident(context.get('generation')))
            page = _url(context.get('snapshot_url') or context.get('page_url')) if exact else ''
            branch = _url(context.get('branch_url')) if exact else ''
            if page:
                _add(result['by_page'].setdefault(page, _bucket()), row)
            else:
                _add(result['unattributed'], row)
            if branch:
                _add(result['by_branch'].setdefault(branch, _bucket()), row)
            else:
                _add(result['unattributed_branch'], row)
        ledger = db.session.get(AIBudget, round_key) if round_key else None
        if ledger:
            requests = ledger.used_requests - result['totals']['actual_requests']
            tokens = ledger.used_tokens - result['totals']['confirmed_tokens'] - result['totals']['reconciled_tokens']
            result['ledger_gap'] = {'actual_requests': max(0, requests), 'tokens': max(0, tokens),
                                   'counter_mismatch': bool(requests or tokens)}
            # A seeded/restored historical counter is a real allowance debit,
            # but does not provide the missing page attribution or provider
            # invoice. Concurrent changes also stay incomplete until next read.
            result['attribution_complete'] = not requests and not tokens
        return result
