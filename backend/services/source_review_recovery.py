"""Correct article/search candidates using observed category links and retain history."""
import json

from bs4 import BeautifulSoup
from flask import current_app
from backend.database.db import db
from backend.database.models import School
from backend.database.source_governance_models import SourceProposal, SourceReviewEvent
from backend.services.source_inventory import canonical_url, site_key

RECOVERY_ACTION = 'automatic_review_v3'


def page_problem(html):
    from backend.scraper.discovery.wordpress_publications import page_kind
    soup = BeautifulSoup(html, 'lxml')
    kind = page_kind(soup)
    if kind:
        return kind + '_instead_of_column'
    restricted = soup.select_one('main.restricted')
    if restricted and '受限资源' in restricted.get_text() and '登录' in restricted.get_text():
        return 'source_login_required'
    return ''


def cached_page(proposal, catalog):
    from backend.services.source_governance import read_snapshot
    reference = json.loads(proposal.evidence_json).get('list')
    if reference:
        try:
            return read_snapshot(reference)
        except (ValueError, OSError):
            pass
    school = db.session.get(School, proposal.school_id)
    config = json.loads(proposal.candidate_json)
    key = site_key(school.url) if school else None
    return catalog.snapshot(key, config['list_url']) if key and catalog.report(key) else None


def resolve_non_column(proposal, html):
    from backend.services import source_governance as governance, tasks
    from backend.scraper.discovery.wordpress_publications import column_links
    problem = page_problem(html)
    if not problem:
        return False
    config = json.loads(proposal.candidate_json)
    bundle = json.loads(proposal.evidence_json)
    bundle.update(schema=1, school_id=proposal.school_id, config_hash=governance._hash(config),
                  list=governance._snapshot(html, getattr(html, 'final_url', config['list_url'])))
    proposal.evidence_json = governance._json(bundle)
    proposal.evidence_hash = governance._hash(bundle)
    # Existing active departments require a new explicit revision, not retargeting.
    if problem == 'source_login_required' or proposal.department_id:
        proposal.state = 'needs_review'
        proposal.validation_json = governance._json({'passed': False, 'errors': [problem],
                                                     'validator_version': governance.VERSION})
        proposal.validator_version = governance.VERSION
        db.session.commit()
        return True
    candidates = {item['url']: item for item in column_links(BeautifulSoup(html, 'lxml'), config['list_url'])}
    related = []
    related_columns = []
    for url, item in candidates.items():
        target = next((p for p in SourceProposal.query.filter_by(school_id=proposal.school_id).order_by(SourceProposal.id).all()
                       if canonical_url(json.loads(p.candidate_json)['list_url']) == url), None)
        if target is None:
            target = governance.propose_source(proposal.school_id, {'name': item['name'], 'list_url': url}, origin='submitted_entry')
        related.append(target.id)
        related_columns.append({'id': target.id, 'name': item['name'], 'url': url})
        # Preserve explicit rejection and share one queue item across all articles.
        if target.state in ('proposed', 'validated'):
            tasks.enqueue('source_review', target.id, {'proposal_id': target.id}, capability='directory', replace_finished=False)
        subscriptions = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='subscribe_on_activation').all()
        for event in subscriptions:
            if not SourceReviewEvent.query.filter_by(proposal_id=target.id, actor_id=event.actor_id, action=event.action).first():
                db.session.add(SourceReviewEvent(proposal_id=target.id, actor_id=event.actor_id, action=event.action))
    proposal.state = 'superseded'
    proposal.validator_version = governance.VERSION
    proposal.validated_hash = None
    proposal.validation_json = governance._json({'passed': False, 'errors': [problem],
        'related_proposal_ids': related, 'related_columns': related_columns, 'validator_version': governance.VERSION})
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='resolved_to_columns',
        detail_json=governance._json({'related_proposal_ids': related})))
    db.session.commit()
    return True


def schedule_recovery():
    """One bounded retry after upgrading; finished discoveries recover as well."""
    from backend.services import source_governance as governance, tasks
    from backend.services.runtime_catalog import RuntimeCatalog
    marked = db.session.query(SourceReviewEvent.proposal_id).filter_by(action=RECOVERY_ACTION)
    rows = SourceProposal.query.filter(SourceProposal.state == 'needs_review',
        ~SourceProposal.id.in_(marked)).order_by(SourceProposal.id).all()
    catalog = RuntimeCatalog(current_app.config.get('SOURCE_CATALOG_PATH'))
    count = 0
    for proposal in rows:
        validation = json.loads(proposal.validation_json)
        if validation.get('validator_version') == governance.VERSION:
            continue
        checksum_error = '网页证据已变化，请重新检查' in validation.get('errors', [])
        html = None if checksum_error else cached_page(proposal, catalog)
        if not checksum_error and (not html or page_problem(html) not in ('article_instead_of_column', 'search_instead_of_column')):
            continue
        tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id}, capability='directory')
        proposal.state = 'proposed'
        db.session.add(SourceReviewEvent(proposal_id=proposal.id, action=RECOVERY_ACTION))
        db.session.commit()
        count += 1
        if count >= 20:
            break
    return count
