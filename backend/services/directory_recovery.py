"""Resume old incomplete reviews once, retaining their paid execution history."""
import json
import hashlib

from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.database.source_governance_models import SourceProposal, SourceReviewEvent, DiscoveryWorkItem

UPGRADE_ACTION = 'reliable_directory_upgrade_v1'


def recover_saved_results():
    """Apply current-revision saved proposals once, with no new paid dispatch."""
    from backend.services import tasks
    from backend.services.directory_work import extraction_suggestion
    action = 'usable_fragment_recovery_v2'
    count = 0
    for proposal in SourceProposal.query.filter(SourceProposal.state.in_(('proposed', 'needs_review'))).all():
        if SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action=action).first():
            continue
        journal = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='exploration_step_v2').order_by(SourceReviewEvent.id.desc()).all()
        usable = None
        for event in journal:
            detail = json.loads(event.detail_json)
            if detail.get('applied') or detail.get('revision') != proposal.revision:
                continue
            result = detail.get('result') or {}
            suggestion, error = extraction_suggestion((result.get('output') or {}).get('proposals', []), detail.get('input') or {})
            if not error and suggestion and suggestion.get('decision') == 'propose':
                usable = event.id
                break
        if not usable:
            continue
        current = BackgroundTask.query.filter_by(identity=f'source_review:{proposal.id}').first()
        if current and current.state in ('pending', 'running', 'waiting'):
            continue
        tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id, 'saved_results_only': True}, capability='directory')
        db.session.add(SourceReviewEvent(proposal_id=proposal.id, action=action,
            detail_json=json.dumps({'source_event_id': usable, 'new_paid_calls': False, 'revision': proposal.revision})))
        db.session.commit(); count += 1
    return count


def resume_document_waiters():
    """An attachment's verification must not hold the structural frontier shut."""
    from datetime import datetime, timedelta
    from backend.database.models import VerificationSession, VerificationWaiter
    from backend.scraper.discovery.structure import document_reference
    count = 0
    for task in BackgroundTask.query.filter_by(kind='discover', state='waiting', phase='verification').all():
        checkpoint = dict(task.checkpoint or {})
        session = db.session.get(VerificationSession, checkpoint.get('verification_id')) if checkpoint.get('verification_id') else None
        if not session or not document_reference(session.url):
            continue
        checkpoint.pop('verification_id', None)
        checkpoint['document_verification_outside_structure'] = {'session_id': session.id, 'url': session.url}
        now = datetime.utcnow()
        changed = db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == task.id,
            BackgroundTask.generation == task.generation, BackgroundTask.state == 'waiting',
            BackgroundTask.phase == 'verification').values(state='pending', phase='fetch', capability='directory',
            checkpoint=checkpoint, available_at=now, deadline_at=now + timedelta(hours=2), error='', error_code=''))
        if changed.rowcount:
            db.session.execute(db.delete(VerificationWaiter).where(VerificationWaiter.task_id == task.id,
                VerificationWaiter.session_id == session.id))
            count += 1
        db.session.commit()
    return count


def restore_inventory():
    """Bring pre-upgrade public page and roster records into the checklist."""
    from flask import current_app
    from backend.database.models import School
    from backend.database.source_governance_models import SchoolOnboarding
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services.source_inventory import site_key
    from backend.services.source_relationships import ROSTER_RELATIONS
    from backend.services.directory_work import sync_inventory
    catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
    restored = 0
    for onboarding in SchoolOnboarding.query.all():
        parent = BackgroundTask.query.filter_by(identity=f'discover:{onboarding.school_id}').first()
        generation = parent.generation if parent else 1
        if DiscoveryWorkItem.query.filter_by(school_id=onboarding.school_id, generation=generation, kind='page').first():
            continue
        school = db.session.get(School, onboarding.school_id)
        if not school:
            continue
        key = site_key(school.url)
        report = catalog.report(key)
        if not report or not report.get('pages'):
            continue
        report['official_units'] = [n for n in catalog.structure(key)
            if n['kind'] == 'unit' and n['relation'] in ROSTER_RELATIONS]
        sync_inventory(school.id, report)
        restored += 1
    return restored


def recover_legacy():
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.services import tasks
    count = resume_document_waiters() + restore_inventory() + recover_saved_results()
    try:
        get_model_binding('directory')
    except AIConfigError:
        return count
    for proposal in SourceProposal.query.filter(SourceProposal.state.in_(('proposed', 'needs_review'))).all():
        if SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action=UPGRADE_ACTION).first():
            continue
        records = SourceReviewEvent.query.filter(SourceReviewEvent.proposal_id == proposal.id,
            SourceReviewEvent.action.in_(('exploration_step_v2', 'skill_attempt'))).all()
        incomplete = [r for r in records if json.loads(r.detail_json).get('status') in ('uncertain', 'failed', 'partial', 'pending')]
        if not incomplete:
            continue
        current = BackgroundTask.query.filter_by(identity=f'source_review:{proposal.id}').first()
        if current and current.state in ('pending', 'running', 'waiting'):
            continue
        if DiscoveryWorkItem.query.filter(DiscoveryWorkItem.school_id == proposal.school_id,
                DiscoveryWorkItem.group_key.like(f'source:{proposal.id}:fragment:%')).first():
            continue
        tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id}, capability='directory')
        db.session.add(SourceReviewEvent(proposal_id=proposal.id, action=UPGRADE_ACTION,
            detail_json=json.dumps({'retained_event_ids': [r.id for r in records],
                'reason': 'resume_missing_results', 'retry_limit': 2})))
        db.session.commit(); count += 1
    for task in BackgroundTask.query.filter(BackgroundTask.kind == 'navigation_review',
            BackgroundTask.state.in_(('done', 'failed'))).all():
        payload = dict(task.payload or {})
        if payload.get('full_material_recovery_version'):
            continue
        parent = db.session.get(BackgroundTask, payload.get('parent_task_id'))
        if not parent or parent.generation != payload.get('parent_generation') or parent.payload.get('discovery_pause_requested'):
            continue
        page_key = hashlib.sha256(payload['page']['url'].encode()).hexdigest()[:20]
        if DiscoveryWorkItem.query.filter_by(school_id=payload.get('school_id'), generation=payload['parent_generation']).filter(
                DiscoveryWorkItem.group_key.startswith('page:' + page_key + ':')).first():
            continue
        payload.update(full_material_recovery_version=1, legacy_ai_attempts=1)
        tasks.enqueue('navigation_review', task.identity.split(':', 1)[1], payload, capability='directory')
        count += 1
    return count
