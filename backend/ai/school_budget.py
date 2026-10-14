"""Persistent school exploration scope; queue refresh is not a budget reset."""
from sqlalchemy.exc import IntegrityError

from backend.database.db import db
from backend.ai.models import AIBudget, AISchoolRound


def payload_school_id(payload):
    if payload.get('school_id'):
        return payload['school_id']
    if payload.get('proposal_id'):
        from backend.database.source_governance_models import SourceProposal
        proposal = db.session.get(SourceProposal, payload['proposal_id'])
        return proposal.school_id if proposal else None
    if payload.get('department_id'):
        from backend.database.models import Department
        source = db.session.get(Department, payload['department_id'])
        return source.school_id if source else None
    return None


def school_budget_key(school_id, handle=None):
    scope = db.session.get(AISchoolRound, school_id, populate_existing=True)
    if scope:
        return scope.budget_key
    handle = handle or {}
    payload = handle.get('payload') or {}
    # Reuse the most recent pre-upgrade round instead of gifting a fresh allowance.
    prior = AIBudget.query.filter(AIBudget.key.like(f'school-discovery:{school_id}:%')).order_by(
        AIBudget.updated_at.desc()).first()
    parent = payload.get('parent_task_id') or handle.get('id', 0)
    generation = payload.get('parent_generation') or handle.get('generation', 1)
    key = prior.key if prior else f'school-discovery:{school_id}:{parent}:{generation}'
    try:
        with db.session.begin_nested():
            db.session.add(AISchoolRound(school_id=school_id, budget_key=key))
            db.session.flush()
    except IntegrityError:
        scope = db.session.get(AISchoolRound, school_id, populate_existing=True)
        key = scope.budget_key
    db.session.commit()
    return key


def school_usage(school_id):
    from backend.ai.models import AIExecution
    scope = db.session.get(AISchoolRound, school_id)
    row = db.session.get(AIBudget, scope.budget_key) if scope else None
    unconfirmed=AIExecution.query.filter(AIExecution.status=='uncertain',
        db.cast(AIExecution.budget_keys,db.Text).contains('"'+scope.budget_key+'"',autoescape=True)).count() if scope else 0
    return {'used_tokens': row.used_tokens if row else 0,
            'reserved_tokens': row.reserved_tokens if row else 0,
            'used_requests': row.used_requests if row else 0,
            'reserved_requests': row.reserved_requests if row else 0,
            'budget_key': scope.budget_key if scope else None,'unconfirmed_requests':unconfirmed}


def resume_budget_tasks():
    """An explicit settings change may release saved work, never reset its ledger."""
    from datetime import datetime, timedelta
    from backend.database.models import AppConfig, BackgroundTask
    from backend.services.discovery_policy import get_policy
    from backend.services.discovery_control import requested
    policy = get_policy()
    token_limit = int(AppConfig.get('ai_token_limit_school_discovery', str(policy['ai_school_token_limit'])) or 0)
    request_limit = int(AppConfig.get('ai_request_limit_school_discovery', str(policy['ai_school_request_limit'])) or 0)
    month = datetime.utcnow().strftime('%Y-%m')
    for name in ('total', 'directory'):
        limit = int(AppConfig.get('ai_token_limit_' + name, '0') or 0)
        row = db.session.get(AIBudget, name + ':' + month)
        if limit and row and row.used_tokens + row.reserved_tokens >= limit:
            return 0
    resumed = 0
    resumed_schools = set()
    for job in BackgroundTask.query.filter_by(state='waiting', phase='ai_budget'):
        if requested(job):
            continue
        school_id = payload_school_id(job.payload)
        usage = school_usage(school_id)
        if (token_limit and usage['used_tokens'] + usage['reserved_tokens'] >= token_limit or
                request_limit and usage['used_requests'] + usage['reserved_requests'] >= request_limit):
            continue
        job.state, job.phase = 'pending', 'ai_resume'
        job.available_at = datetime.utcnow()
        if job.deadline_at and job.deadline_at <= job.available_at:
            job.deadline_at = job.available_at + timedelta(hours=2)
        job.error = job.error_code = ''
        resumed_schools.add(school_id)
        resumed += 1
    from backend.database.source_governance_models import DiscoveryWorkItem
    if resumed_schools:
        for item in DiscoveryWorkItem.query.filter_by(state='paused', error_code='budget_exhausted').filter(
                DiscoveryWorkItem.school_id.in_(resumed_schools)):
            item.state, item.next_run_at, item.error_code = 'pending', None, ''
    db.session.commit()
    return resumed
