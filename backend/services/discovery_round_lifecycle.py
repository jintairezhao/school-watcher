"""End billing rounds only after all work settles; renew on later observed redesign."""
from datetime import datetime
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from flask import current_app
from sqlalchemy import update

from backend.database.db import db
from backend.database.models import BackgroundTask, School
from backend.ai.models import AIBudget, AISchoolRound
from backend.ai.school_budget import payload_school_id


KINDS = ('discover', 'directory', 'navigation_review', 'onboard', 'source_review',
         'source_grouping', 'selectors', 'student_assessment')


def _scope_locked(school_id):
    # An actual write serializes SQLite workers as well as taking a row lock on
    # PostgreSQL. It precedes reads, avoiding a deferred SQLite read->write race.
    db.session.execute(update(AISchoolRound).where(AISchoolRound.school_id == school_id)
                       .values(budget_key=AISchoolRound.budget_key))
    return db.session.get(AISchoolRound, school_id, populate_existing=True)


def _idle_budget(key):
    budget = db.session.get(AIBudget, key, populate_existing=True)
    return not budget or not (budget.active_count or budget.reserved_tokens or budget.reserved_requests)


def _unfinished(school_id, *, completed_at=None):
    for task in BackgroundTask.query.filter(BackgroundTask.kind.in_(KINDS), BackgroundTask.state != 'done'):
        if payload_school_id(task.payload or {}) != school_id:
            continue
        if (completed_at is None or task.state not in ('pending', 'running')
                or (task.queued_at or task.created_at) <= completed_at):
            return True
    from backend.database.source_governance_models import DiscoveryWorkItem
    # A saved quota pause cannot disappear just because a parent was renewed.
    waiting = DiscoveryWorkItem.query.filter_by(school_id=school_id).filter(
        DiscoveryWorkItem.state.in_(('pending', 'running', 'retry_wait', 'paused')))
    if completed_at is None:
        return waiting.first() is not None
    return waiting.filter(DiscoveryWorkItem.state == 'paused').first() is not None


def _completed_structure(school_id):
    from backend.services.discovery_changes import baseline
    from backend.services.source_inventory import canonical_url, site_key
    path = current_app.config.get('DISCOVERY_CACHE_PATH')
    school = db.session.get(School, school_id)
    if not path or not school or not Path(path).is_file():
        return {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        pages = [dict(row) for row in connection.execute(
            'SELECT url,state,health,error,content_hash,notes_json FROM pages WHERE site_key=?', (site_key(school.url),))]
    if any(page['state'] in ('pending', 'running', 'failed') or
           page['state'] == 'blocked' and (page['health'] in ('unreachable', 'dynamic_content') or
               page['error'] not in ('directory_document_requires_adapter', 'document_reference_only'))
           for page in pages):
        # A failed entrance may have no child job yet. Its delayed network or
        # access recovery still belongs to this round, including after 6 hours.
        # Explicit static document references are terminal without a model.
        return {}
    return {canonical_url(page['url']): {'shape': baseline(page)['hash'], 'content_hash': page['content_hash']}
            for page in pages if page['state'] == 'fetched' and page['content_hash'] and baseline(page).get('hash')}


def mark_completed(school_id):
    """Called after a task commits; a done parent alone never closes the round."""
    if not school_id or not current_app.config.get('DISCOVERY_CACHE_PATH'):
        return False
    try:
        scope = _scope_locked(school_id)
        if not scope or scope.completed_at or not _idle_budget(scope.budget_key) or _unfinished(school_id):
            return False
        structure = _completed_structure(school_id)
        if not structure:
            return False
        scope.completed_at = datetime.utcnow()
        scope.completed_structure = structure
        db.session.flush()
        return True
    finally:
        db.session.commit()


def observe_structure(school_id, url, shape, content_hash):
    """Only executed HTTP evidence after a completed round can create a new key.

    Article changes without a structural change, parser upgrades over identical
    HTML, stale tasks and unfinished budget/network/user pauses retain the key.
    Existing ledgers and paid execution IDs are never deleted or reset.
    """
    from backend.services.source_inventory import canonical_url
    if not school_id or not shape or not content_hash:
        return False
    try:
        scope = _scope_locked(school_id)
        if not scope or not scope.completed_at or not _idle_budget(scope.budget_key):
            return False
        if _unfinished(school_id, completed_at=scope.completed_at):
            return False
        prior = (scope.completed_structure or {}).get(canonical_url(url), {})
        if prior.get('shape') == shape or prior.get('content_hash') == content_hash:
            return False
        old_key, completed = scope.budget_key, scope.completed_at
        suffix = hashlib.sha256((old_key + '\0' + canonical_url(url) + '\0' + shape).encode()).hexdigest()[:16]
        changed = db.session.execute(update(AISchoolRound).where(AISchoolRound.school_id == school_id,
            AISchoolRound.budget_key == old_key, AISchoolRound.completed_at == completed).values(
                budget_key=f'school-discovery:{school_id}:r:{suffix}', created_at=datetime.utcnow(),
                completed_at=None, completed_structure={}))
        return bool(changed.rowcount)
    finally:
        db.session.commit()
