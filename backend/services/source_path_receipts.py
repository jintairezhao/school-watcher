"""Read executed source receipts for scheduling, without granting ownership.

No HTML, network operation or source write is needed here. A missing receipt is
unknown, not evidence that an entrance is irrelevant or that a source failed.
"""
import json
import re

from flask import current_app, has_app_context

from backend.services.source_inventory import canonical_url


def _object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _url(value):
    if not isinstance(value, str):
        return ''
    try:
        return canonical_url(value)
    except (ValueError, UnicodeError):
        return ''


def _material(reference, region, url):
    if not isinstance(reference, dict) or not isinstance(region, dict):
        return None
    digest = reference.get('hash')
    if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
            or _url(reference.get('url')) != url
            or reference.get('outcome', 'usable') != 'usable'
            or region.get('empty')
            or any(not isinstance(region.get(key), str) or not region[key].strip()
                   for key in ('container', 'heading'))):
        return None
    return digest


def _native_receipt(event, source, config_hash, url):
    detail = _object(event.validation_json)
    articles = detail.get('articles')
    if (event.department_id != source.id or event.config_hash != config_hash
            or detail.get('status') not in ('full', 'partial', 'limited')
            or not isinstance(articles, dict) or articles.get('passed') is not True
            or detail.get('errors')
            or detail.get('school_id', source.school_id) != source.school_id
            or detail.get('department_id', source.id) != source.id
            or detail.get('config_hash', config_hash) != config_hash
            or _url(detail.get('list_url', url)) != url):
        return None
    digest = _material(detail.get('list_snapshot'), detail.get('region'), url)
    # page_hash belongs to recognition/cache input and may normalize markup.
    # The retained list_snapshot hash is the original HTML's raw SHA-256.
    if not digest:
        return None
    return {'source_id': source.id, 'url': url, 'content_hash': digest,
            'receipt_id': 'native:' + str(event.id), 'config_hash': config_hash,
            'region': dict(detail['region'])}


def _public_receipt(proposal, source, config, config_hash, url):
    from backend.services.source_governance import VERSION, _hash
    bundle, validation = _object(proposal.evidence_json), _object(proposal.validation_json)
    count = validation.get('item_count')
    if (proposal.state != 'activated' or proposal.validator_version != VERSION
            or validation.get('passed') is not True or validation.get('errors')
            or isinstance(count, bool) or not isinstance(count, int) or count < 1
            or _object(proposal.candidate_json) != config
            or bundle.get('config') != config or bundle.get('config_hash') != config_hash
            or bundle.get('school_id') != source.school_id
            or _url(bundle.get('root_url')) != _url(source.school.url)
            or _hash(bundle) != proposal.evidence_hash
            or proposal.validated_hash != _hash([
                proposal.candidate_json, proposal.evidence_hash, VERSION])):
        return None
    digest = _material(bundle.get('list'), bundle.get('region'), url)
    if not digest:
        return None
    return {'source_id': source.id, 'url': url, 'content_hash': digest,
            'receipt_id': 'proposal:' + str(proposal.id), 'config_hash': config_hash,
            'region': dict(bundle['region'])}


def load_source_receipts(site):
    """One latest current-config receipt per stable source, scoped to one school.

    Repeated validations do not multiply reward. A later unsuccessful record
    for this exact config hides an older success; a draft is never a receipt.
    Invalid data is skipped, while database and programming errors propagate.
    """
    if not has_app_context() or 'sqlalchemy' not in current_app.extensions:
        return []
    from backend.database.db import db
    from backend.database.models import Department, School
    from backend.database.source_governance_models import SourceProposal, SourceValidation
    from backend.services.source_governance import source_config, _hash

    site = _object(site)
    root_url = _url(site.get('root_url'))
    if not root_url or not isinstance(site.get('name'), str):
        return []
    with db.session.no_autoflush:
        schools = [school for school in School.query.filter_by(name=site['name'])
                   if _url(school.url) == root_url]
        if len(schools) != 1:
            return []
        school_id = schools[0].id
        sources = Department.query.filter_by(school_id=school_id, kind='column').order_by(Department.id).all()
        validations = SourceValidation.query.filter_by(school_id=school_id).order_by(
            SourceValidation.created_at.desc(), SourceValidation.id.desc()).all()
        proposals = SourceProposal.query.filter_by(school_id=school_id, state='activated').order_by(
            SourceProposal.updated_at.desc(), SourceProposal.id.desc()).all()
        latest_native, latest_unassigned, latest_public = {}, {}, {}
        for event in validations:
            if event.department_id is None:
                latest_unassigned.setdefault(event.config_hash, event)
            else:
                latest_native.setdefault(event.department_id, event)
        for proposal in proposals:
            latest_public.setdefault(proposal.department_id, proposal)
        receipts = []
        for source in sources:
            config = source_config(source)
            url, config_hash = _url(config['list_url']), _hash(config)
            if not url or not config['list_selector']:
                continue
            candidates = (latest_native.get(source.id), latest_unassigned.get(config_hash))
            native = max((event for event in candidates if event is not None),
                         key=lambda event: (event.created_at, event.id), default=None)
            public = latest_public.get(source.id)
            # Compare the two real writer paths before checking validity: an
            # invalid newest event must not fall through to old positive proof.
            if native and (not public or native.created_at >= public.updated_at):
                receipt = _native_receipt(native, source, config_hash, url)
            elif public:
                receipt = _public_receipt(public, source, config, config_hash, url)
            else:
                receipt = None
            if receipt:
                receipts.append(receipt)
        return receipts
