"""Evidence-bound candidate → validation → activation for every source writer.

Public routes pass candidate fields only. Evidence is captured by this module,
never supplied as a client's ``verified`` flag. No network operation holds a
database transaction, and publication checks the original config and task fence.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from flask import current_app
from sqlalchemy import select
from backend.database.db import db
from backend.database.models import Department, School
from backend.database.source_governance_models import (
    SourceProposal, SourceConfigVersion, SourceReviewEvent, SchoolOnboarding)
from backend.services.source_inventory import canonical_url, site_key

VERSION = 'source-governance-3'
FIELDS = ('name', 'list_url', 'list_selector', 'title_selector', 'link_selector',
          'date_selector', 'content_selector', 'group_name')
ACTION_LABEL = re.compile(r'^(?:read(?:\s+more)?|more|learn\s+more|了解|更多|查看|详情)$', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256((_json(value) if not isinstance(value, str) else value).encode()).hexdigest()


def source_config(department):
    return {field: (getattr(department, field, '') or '') for field in FIELDS} if department else {}


def _candidate(value, *, allow_missing_list=False):
    from backend.scraper.http_client import validate_public_url
    if not isinstance(value, dict):
        raise ValueError('来源配置格式不正确')
    result = {field: ('' if value.get(field) is None else value.get(field)) for field in FIELDS}
    if any(not isinstance(v, str) for v in result.values()):
        raise ValueError('来源字段必须是文字')
    result = {k: v.strip() for k, v in result.items()}
    if not result['name'] or len(result['name']) > 200 or ACTION_LABEL.fullmatch(result['name']):
        raise ValueError('栏目名称应使用官网正式标题')
    if len(result['group_name']) > 200 or any(len(result[k]) > 500 for k in FIELDS if k.endswith('selector')):
        raise ValueError('来源字段过长')
    validate_public_url(result['list_url'], resolve=False)
    if not result['list_selector'] and not allow_missing_list:
        raise ValueError('尚未确定通知列表区域')
    # Parse every selector before storing; empty optional selectors are allowed.
    soup = BeautifulSoup('<main></main>', 'lxml')
    for key in FIELDS:
        if key.endswith('selector') and result[key] and result[key] != ':scope':
            try:
                soup.select(result[key])
            except Exception as exc:
                raise ValueError('网页选择规则格式不正确') from exc
    return result


def _evidence_root():
    configured = current_app.config.get('SOURCE_GOVERNANCE_EVIDENCE_PATH')
    if configured:
        return Path(configured)
    inventory = current_app.config.get('SOURCE_INVENTORY_PATH')
    return (Path(inventory).parent if inventory else Path(current_app.instance_path)) / 'source-governance-evidence'


def _snapshot(html, url, *, outcome='usable', role='list'):
    html = str(html)
    if len(html.encode()) > 8 * 1024 * 1024:
        raise ValueError('网页证据超过单页限制')
    digest = _hash(html)
    root = _evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + '.html.gz')
    if not path.exists():
        # Exclusive creation is safe for simultaneous identical snapshots.
        try:
            with path.open('xb') as stream:
                stream.write(gzip.compress(html.encode()))
        except FileExistsError:
            pass
    return {'hash': digest, 'url': canonical_url(url), 'role': role, 'outcome': outcome,
            'captured_at': datetime.utcnow().isoformat()}


def read_snapshot(reference):
    digest = reference.get('hash', '')
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('网页证据标识无效')
    path = _evidence_root() / (digest + '.html.gz')
    try:
        # Hashes cover the original HTML, including CRLF and lone CR characters.
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
            html = stream.read(8 * 1024 * 1024 + 1)
    except (OSError, EOFError) as exc:
        raise ValueError('网页证据不可用，请重新检查') from exc
    if _hash(html) != digest:
        raise ValueError('网页证据已变化，请重新检查')
    return html


@dataclass(frozen=True)
class CapturedEvidence:
    """Internal return type; HTTP clients cannot assert captured evidence."""
    bundle: dict


def _fetch(url, purpose):
    from backend.scraper.engine import _fetch_html
    if purpose == 'independent_list':
        return _fetch_html(url, purpose='list', raise_fetch_errors=True,
                           policy={'verification_pass': 'independent'})
    return _fetch_html(url, purpose=purpose, raise_fetch_errors=True)


def _fetch_snapshot(url, purpose, fetcher):
    html = fetcher(url, purpose)
    result = getattr(html, 'result', None)
    if result is not None and not result.ok:
        raise ValueError('官网返回了无效页面')
    outcome = getattr(result, 'outcome', 'usable')
    if outcome not in ('usable', 'empty'):
        raise ValueError('网页内容尚未就绪，不能审核来源')
    return _snapshot(html, getattr(html, 'final_url', url), outcome=outcome, role=purpose)


def _normal(text):
    return re.sub(r'[\s\-—_|·：:]+', '', text or '')


def _extract(config, reference):
    from backend.scraper.discovery.publication_lists import select_node
    from backend.scraper.detectors.title_quality import is_junk_title, clean_title
    from backend.scraper.date_elements import publication_date_text
    from backend.scraper.change_detector import parse_date
    soup = BeautifulSoup(read_snapshot(reference), 'lxml')
    items = soup.select(config['list_selector'])
    records, errors = [], []
    for item in items:
        anchor = select_node(item, config['link_selector'] or 'a[href]')
        title_node = select_node(item, config['title_selector']) if config['title_selector'] else item
        href = anchor.get('href', '') if anchor else ''
        url = canonical_url(urljoin(reference['url'], href)) if href and not href.startswith(('javascript:', 'mailto:', 'tel:')) else ''
        title = clean_title((title_node.get('title') or title_node.get('data-title') or title_node.get_text(' ', strip=True)) if title_node else '')
        if not url or not title or len(title) > 300 or is_junk_title(title, article_url=url):
            errors.append('invalid_list_item')
            continue
        date_node = select_node(item, config['date_selector']) if config['date_selector'] else None
        date_text = publication_date_text(date_node)
        if date_text and not parse_date(date_text):
            errors.append('invalid_publication_date')
        records.append({'url': url, 'title': title, 'date': date_text})
    if len({r['url'] for r in records}) != len(records):
        errors.append('duplicate_list_items')
    return soup, items, records, errors


def _column_scope(config, reference):
    """A list must remain within one actual named publishing region."""
    from backend.scraper.discovery.publication_lists import publication_lists
    html = read_snapshot(reference)
    from backend.services.source_review_recovery import page_problem
    problem = page_problem(html)
    if problem:
        return [], [problem], {}
    soup, items, records, errors = _extract(config, reference)
    if not items:
        if reference.get('outcome') == 'empty':
            expected = _normal(config['name'].split(' / ')[-1])
            headings = [_normal(node.get_text(' ', strip=True)) for node in soup.select('h1,h2,h3,h4')]
            return records, errors + ([] if expected in headings else ['empty_column_identity_unconfirmed']), {'empty': True}
        return records, errors + ['list_missing'], {}
    expected = _normal(config['name'].split(' / ')[-1])
    regions = []
    for feed in publication_lists(html, reference['url']):
        if not feed.get('name') or feed.get('heading_ambiguous'):
            continue
        if _normal(feed['name']) != expected:
            continue
        region_items = soup.select(feed['list_selector'])
        if all(any(item is node or any(parent is node for parent in item.parents)
                   or any(parent is item for parent in node.parents) for node in region_items) for item in items):
            # Requiring equal item identities prevents a broad adjacent widget
            # selector from being approved because its first few samples match.
            if len(region_items) == len(items):
                regions.append({'name': feed['name'], 'container': feed.get('container_locator', ''),
                                'heading': feed.get('heading_locator', '')})
    if not regions:
        # Small genuine lists frequently have just one or two entries. A common
        # nearest section with its own exact heading is sufficient evidence.
        for parent in [items[0], *items[0].parents]:
            if getattr(parent, 'name', None) in ('html', '[document]'):
                break
            if not all(item is parent or any(ancestor is parent for ancestor in item.parents) for item in items):
                continue
            headings = parent.select('h1,h2,h3,h4,h5,h6')
            if len(headings) == 1 and _normal(headings[0].get_text(' ', strip=True)) == expected:
                regions.append({'name': headings[0].get_text(' ', strip=True), 'container': parent.get('id', ''), 'heading': headings[0].name})
                break
    if not regions:
        errors.append('column_identity_or_scope_unconfirmed')
    return records, sorted(set(errors)), regions[0] if regions else {}


def capture_source_evidence(school_id, candidate, *, department_id=None, seed_html=None,
                            inventory=None, fetcher=None):
    """Acquire bounded fresh checks. Called by trusted workers, never model output."""
    config = _candidate(candidate)
    school = db.session.get(School, school_id)
    if not school:
        raise ValueError('学校不存在')
    school_name, root_url = school.name, school.url
    department = db.session.get(Department, department_id) if department_id else None
    if department and department.school_id != school_id:
        raise ValueError('来源不属于这所学校')
    previous = source_config(department)
    db.session.commit()
    fetcher = fetcher or _fetch
    first = (_snapshot(seed_html, getattr(seed_html, 'final_url', config['list_url']),
                       outcome=getattr(getattr(seed_html, 'result', None), 'outcome', 'usable'))
             if seed_html is not None else _fetch_snapshot(config['list_url'], 'list', fetcher))
    bundle = {'schema': 1, 'school_id': school_id, 'school_name': school_name, 'root_url': root_url,
              'config_hash': _hash(config), 'expected_config': previous,
              'list': first, 'articles': [], 'identity_paths': [], 'identity_snapshots': []}
    # Capture reference DOMs along with derived paths, so later validation does
    # not rely on candidate-supplied ownership or a mutable discovery cache.
    if inventory is not None:
        from backend.services.source_relationships import SourceRelationships
        key = site_key(root_url)
        report = inventory.report(key)
        if report:
            relations = SourceRelationships(report, inventory.structure(key))
            paths = relations.paths_for(first['url'])
            owners = relations.publication_owners(first['url'], paths)
            bundle['identity_paths'] = [p for p in paths if p.get('unit_name') in owners]
            references = {r['url'] for p in bundle['identity_paths'] for r in p.get('references', []) if r.get('url')}
            for ref_url in references:
                html = inventory.snapshot(key, ref_url)
                if html:
                    bundle['identity_snapshots'].append(_snapshot(html, ref_url, role='identity'))
    db.session.commit()
    records, errors, region = _column_scope(config, first)
    bundle['region'] = region
    bundle['preflight_errors'] = errors
    # Do not spend body requests on a clearly wrong adjacent list.
    if not errors:
        for record in records[:3]:
            article = _fetch_snapshot(record['url'], 'body', fetcher)
            article['listed_title'] = record['title']
            article['listed_url'] = record['url']
            bundle['articles'].append(article)
        from backend.scraper.engine import _next_page_url
        next_url = _next_page_url(read_snapshot(first), first['url'], 1)
        if next_url:
            bundle['pagination'] = _fetch_snapshot(next_url, 'list', fetcher)
            bundle['pagination']['observed_url'] = next_url
        bundle['independent'] = _fetch_snapshot(config['list_url'], 'independent_list', fetcher)
    return CapturedEvidence(bundle)


def propose_source(school_id, candidate, evidence=None, *, department_id=None, origin='discovery', expected_config=None, commit=True):
    config = _candidate(candidate, allow_missing_list=origin == 'submitted_entry')
    department = db.session.get(Department, department_id) if department_id else None
    if department_id and (not department or department.school_id != school_id):
        raise ValueError('来源不属于这所学校')
    if evidence is not None and not isinstance(evidence, CapturedEvidence):
        raise ValueError('网页证据必须由服务器检查取得')
    bundle = evidence.bundle if evidence else {}
    if bundle and (bundle.get('school_id') != school_id or bundle.get('config_hash') != _hash(config)):
        raise ValueError('网页证据与候选配置不一致')
    expected = expected_config if expected_config is not None else bundle.get('expected_config', source_config(department))
    expected_hash = _hash(expected)
    identity = _hash([school_id, department_id, canonical_url(config['list_url']), config['list_selector']])
    evidence_hash = _hash(bundle)
    key = _hash([identity, config, expected_hash, evidence_hash])
    from backend.database.dialect import insert
    statement = insert(SourceProposal).values(school_id=school_id, department_id=department_id,
        identity_key=identity, proposal_key=key, origin=origin, state='proposed', candidate_json=_json(config),
        expected_config_hash=expected_hash, evidence_json=_json(bundle), evidence_hash=evidence_hash,
        validation_json='{}', revision=1, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.session.execute(statement.on_conflict_do_nothing(index_elements=['proposal_key']))
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return SourceProposal.query.filter_by(proposal_key=key).one()


def _inside(url, root):
    a, b = urlsplit(url), urlsplit(root)
    if b.query or b.fragment:
        return canonical_url(url) == canonical_url(root)
    return a.netloc == b.netloc and (a.path == b.path.rstrip('/') or a.path.startswith(b.path.rstrip('/') + '/'))


def _identity_errors(proposal, config, bundle):
    from backend.scraper.http_client import same_school_url
    group = config['group_name']
    expected = bundle.get('expected_config', {})
    paths = bundle.get('identity_paths', [])
    owners = set()
    snapshots = {r['url']: r for r in bundle.get('identity_snapshots', [])}
    for path in paths:
        unit = next((n for n in reversed(path.get('nodes', [])) if n.get('kind') == 'unit'), None)
        if not unit or not _inside(bundle['list']['url'], unit.get('url', '')):
            continue
        refs = path.get('references', [])
        if not refs or any(ref.get('url') not in snapshots for ref in refs):
            continue
        if any(_hash(read_snapshot(snapshots[ref['url']])) != ref.get('content_hash') for ref in refs):
            continue
        owners.add(path.get('unit_name'))
    if owners:
        return [] if len(owners) == 1 and group in owners else ['publisher_conflict']
    # A selector copied from YAML/import/legacy data is not ownership evidence.
    # Only a prior gated version can supply continuity when current evidence is
    # silent; contrary official ownership above always wins and blocks it.
    if proposal.department_id and expected:
        latest = SourceConfigVersion.query.filter_by(department_id=proposal.department_id).order_by(SourceConfigVersion.version.desc()).first()
        if (latest and latest.config_hash == _hash(expected) and config['name'] == expected.get('name')
                and group == expected.get('group_name', '')
                and canonical_url(config['list_url']) == canonical_url(expected.get('list_url', ''))):
            return []
    # A school-root list can belong to the school itself. A shared domain alone
    # cannot assert a college publisher for its internal pages.
    if canonical_url(config['list_url']) == canonical_url(bundle.get('root_url', '')) and group in ('', bundle.get('school_name')):
        return []
    confirmations = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='confirm_identity').all()
    if confirmations and same_school_url(config['list_url'], bundle.get('root_url', '')):
        return []
    return ['publisher_requires_review']


def validate_proposal(proposal_id, *, independent_evidence=None):
    proposal = db.session.get(SourceProposal, proposal_id)
    if not proposal:
        raise ValueError('待核实来源不存在')
    if proposal.state == 'activated':
        return json.loads(proposal.validation_json)
    config, bundle = json.loads(proposal.candidate_json), json.loads(proposal.evidence_json)
    if independent_evidence is not None:
        if not isinstance(independent_evidence, CapturedEvidence):
            raise ValueError('复测证据必须来自服务器')
        bundle = independent_evidence.bundle
        if bundle.get('config_hash') != _hash(config) or bundle.get('school_id') != proposal.school_id:
            raise ValueError('复测证据不属于此配置')
        proposal.evidence_json, proposal.evidence_hash = _json(bundle), _hash(bundle)
    errors, records = [], []
    if not bundle or not bundle.get('list'):
        errors.append('evidence_missing')
    else:
        try:
            if _hash(bundle) != proposal.evidence_hash or bundle.get('config_hash') != _hash(config):
                raise ValueError('evidence_changed')
            records, errors, region = _column_scope(config, bundle['list'])
            errors += _identity_errors(proposal, config, bundle)
            independent = bundle.get('independent')
            if not independent:
                errors.append('independent_sample_missing')
            else:
                _, independent_errors, _ = _column_scope(config, independent)
                errors += ['independent_' + e for e in independent_errors]
            required = {record['url']: record for record in records[:3]}
            articles = {ref.get('listed_url'): ref for ref in bundle.get('articles', [])}
            for url, record in required.items():
                ref = articles.get(url)
                if not ref:
                    errors.append('article_sample_missing')
                    continue
                soup = BeautifulSoup(read_snapshot(ref), 'lxml')
                visible = _normal(soup.get_text(' ', strip=True))
                title = _normal(record['title'])
                if ref.get('outcome') != 'usable' or title[:min(len(title), 20)] not in visible:
                    errors.append('article_identity_mismatch')
                if len(visible) < len(title) + 15:
                    errors.append('article_body_missing')
            from backend.scraper.engine import _next_page_url
            next_url = _next_page_url(read_snapshot(bundle['list']), bundle['list']['url'], 1)
            if next_url:
                pagination = bundle.get('pagination')
                if not pagination or canonical_url(pagination.get('observed_url', '')) != canonical_url(next_url):
                    errors.append('pagination_sample_missing')
                else:
                    next_records, next_errors, _ = _column_scope(config, pagination)
                    errors += ['pagination_' + e for e in next_errors]
                    if next_records and {r['url'] for r in next_records} == {r['url'] for r in records}:
                        errors.append('pagination_repeats_first_page')
        except (ValueError, OSError) as exc:
            errors.append(str(exc)[:160])
    errors = sorted(set(errors))
    result = {'passed': not errors, 'errors': errors, 'item_count': len(records),
              'samples': records[:3], 'validator_version': VERSION}
    proposal.validation_json = _json(result)
    proposal.validator_version = VERSION
    proposal.validated_hash = _hash([proposal.candidate_json, proposal.evidence_hash, VERSION]) if not errors else None
    proposal.state = 'validated' if not errors else 'needs_review'
    proposal.updated_at = datetime.utcnow()
    db.session.commit()
    return result


def activate_proposal(proposal_id):
    from backend.services import tasks
    proposal = db.session.execute(select(SourceProposal).where(SourceProposal.id == proposal_id).with_for_update()).scalar_one_or_none()
    if not proposal:
        raise ValueError('待核实来源不存在')
    if proposal.state == 'activated':
        return db.session.get(Department, proposal.department_id)
    if proposal.state != 'validated' or proposal.validator_version != VERSION or proposal.validated_hash != _hash([proposal.candidate_json, proposal.evidence_hash, VERSION]):
        raise ValueError('来源尚未通过统一检查')
    # Evidence corruption/cleanup after validation cannot bypass the gate.
    bundle = json.loads(proposal.evidence_json)
    if _hash(bundle) != proposal.evidence_hash:
        raise ValueError('来源证据已经变化')
    for reference in [bundle['list'], bundle['independent'], *bundle.get('articles', []), *bundle.get('identity_snapshots', [])]:
        read_snapshot(reference)
    if bundle.get('pagination'):
        read_snapshot(bundle['pagination'])
    # Serialize activation per school as well as per proposal, including two
    # different proposals for the same URL/widget. SQLite obtains a write lock.
    db.session.execute(db.update(School).where(School.id == proposal.school_id).values(name=School.name))
    department = (db.session.execute(select(Department).where(Department.id == proposal.department_id)
                  .execution_options(populate_existing=True).with_for_update()).scalar_one_or_none()
                  if proposal.department_id else None)
    previous = source_config(department)
    if _hash(previous) != proposal.expected_config_hash:
        proposal.state = 'stale'
        db.session.commit()
        raise ValueError('来源配置已经更新，请重新检查此建议')
    config = json.loads(proposal.candidate_json)
    tasks.assert_owned()
    if department is None:
        existing = Department.query.filter_by(school_id=proposal.school_id, list_url=config['list_url'], list_selector=config['list_selector']).first()
        if existing:
            if source_config(existing) != config:
                raise ValueError('同一列表已有配置，请作为现有来源修改审核')
            department = existing
        else:
            department = Department(school_id=proposal.school_id, **config)
            db.session.add(department)
            db.session.flush()
    else:
        from backend.services.tasks import invalidate_source
        invalidate_source(department.id)
        for field, value in config.items():
            setattr(department, field, value)
    latest = db.session.query(db.func.max(SourceConfigVersion.version)).filter_by(department_id=department.id).scalar() or 0
    db.session.add(SourceConfigVersion(department_id=department.id, proposal_id=proposal.id,
        version=latest + 1, config_json=_json(config), config_hash=_hash(config), previous_json=_json(previous)))
    proposal.department_id, proposal.state = department.id, 'activated'
    # Submission is a pending intent, never an early subscription to a guessed
    # source. Only still-subscribed users receive the now-validated column.
    from backend.database.models import Subscription
    for event in SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='subscribe_on_activation'):
        sub = Subscription.query.filter_by(school_id=proposal.school_id, user_id=event.actor_id).first()
        if sub and sub.department_ids is not None:
            sub.department_ids = sorted(set(sub.department_ids + [department.id]))
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='activate', detail_json=_json({'version': latest + 1})))
    # Re-check immediately before committing all source/version/result changes.
    tasks.assert_owned()
    db.session.commit()
    return department


def review_proposal(proposal_id, action, actor_id, note=''):
    from backend.database.models import User
    actor = db.session.get(User, actor_id)
    if not actor or not actor.is_admin:
        raise ValueError('仅管理员可以处理来源核实')
    proposal = db.session.get(SourceProposal, proposal_id)
    if not proposal:
        raise ValueError('待核实来源不存在')
    if action not in ('confirm_identity', 'reject', 'recheck'):
        raise ValueError('未知核实操作')
    if proposal.state == 'activated':
        raise ValueError('已发布配置应通过新的修订处理')
    if proposal.state == 'superseded':
        raise ValueError('这条文章已整理到所属栏目，请查看对应栏目')
    if action == 'confirm_identity' and not str(note).strip():
        raise ValueError('请记录核实的官网机构与依据')
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, actor_id=actor_id, action=action, note=str(note)[:2000]))
    if action == 'reject':
        proposal.state = 'rejected'
    else:
        proposal.state = 'proposed'
        proposal.validated_hash = None
    db.session.commit()
    return serialize_proposal(proposal)


def serialize_proposal(proposal):
    return {'id': proposal.id, 'school_id': proposal.school_id, 'department_id': proposal.department_id,
            'state': proposal.state, 'origin': proposal.origin, 'candidate': json.loads(proposal.candidate_json),
            'validation': json.loads(proposal.validation_json), 'revision': proposal.revision,
            'updated_at': proposal.updated_at.isoformat()}


def record_onboarding_slice(school_id, report, *, error=''):
    """Progress is persisted independently from completeness; no candidate-count gate."""
    from backend.services.source_relationships import ROSTER_RELATIONS
    row = db.session.get(SchoolOnboarding, school_id)
    if not row:
        row = SchoolOnboarding(school_id=school_id)
        db.session.add(row)
    states = report.get('states', {})
    row.pending_pages = states.get('pending', 0) + states.get('running', 0)
    row.checked_pages = states.get('fetched', 0)
    row.last_error = str(error)[:800]
    old_scopes = json.loads(row.scope_json or '[]')
    # Keep independent scope conclusions until their own reference changes.
    scopes = {scope['key']: scope for scope in old_scopes}
    for item in report.get('official_units', []):
        key = item.get('node_key') or item.get('key')
        if key and key not in scopes:
            scopes[key] = {'key': key, 'name': item['name'], 'url': item.get('url', ''),
                           'state': 'not_checked', 'reference_url': item.get('reference_url', ''),
                           'reference_hash': item.get('content_hash', ''), 'priority_scopes': {}}
        elif key and scopes[key].get('reference_hash') and item.get('content_hash') != scopes[key]['reference_hash']:
            scopes[key]['state'] = 'needs_review'
            scopes[key]['reference_changed'] = True
    row.scope_json = _json(list(scopes.values()))
    active = SourceConfigVersion.query.join(Department, SourceConfigVersion.department_id == Department.id).filter(Department.school_id == school_id).count()
    row.state = 'discovering' if row.pending_pages else ('partial' if active else 'needs_review')
    checkpoint = json.loads(row.checkpoint_json or '{}')
    reference_checks = {check['id']: check for check in checkpoint.get('reference_checks', [])}
    reference_checks.update({check['id']: check for check in report.get('reference_checks', [])})
    pages = {page['url']: page for page in report.get('pages', [])}
    for roster in checkpoint.get('independent_rosters', []):
        page = pages.get(roster['reference']['url'])
        if page and (page.get('state') != 'fetched' or page.get('content_hash') != roster['reference']['hash']):
            reference_checks[roster['id']] = {'id': roster['id'], 'scope_passed': False,
                'reference_current': False, 'status': 'reference_changed_or_unavailable'}
    checkpoint.update(states=states, reference_checks=list(reference_checks.values()))
    row.checkpoint_json = _json(checkpoint)
    row.next_check_at = datetime.utcnow() + timedelta(days=7 if (db.session.get(School, school_id).subscriber_count or 0) else 30)
    db.session.commit()
    evaluate_school_readiness(school_id)
    return school_governance_status(school_id)


def school_governance_status(school_id):
    row = db.session.get(SchoolOnboarding, school_id)
    proposals = SourceProposal.query.filter_by(school_id=school_id).all()
    counts = {}
    for proposal in proposals:
        counts[proposal.state] = counts.get(proposal.state, 0) + 1
    return {'school_id': school_id, 'state': row.state if row else 'not_checked',
            'pending_pages': row.pending_pages if row else 0, 'checked_pages': row.checked_pages if row else 0,
            'official_units': json.loads(row.scope_json or '[]') if row else [],
            'coverage_verified': bool(row and row.state == 'ready'), 'proposals': counts,
            'last_error': row.last_error if row else '',
            'updated_at': row.updated_at.isoformat() if row else None}


SCOPE_STATES = {'verified', 'no_independent_column', 'not_applicable', 'unreachable', 'needs_review', 'not_checked'}
COMPLETE_SCOPE_STATES = {'verified', 'no_independent_column', 'not_applicable'}
PRIORITY_SCOPES = ('general_notices', 'undergraduate', 'postgraduate', 'admissions', 'student_affairs')


def _require_admin(actor_id):
    from backend.database.models import User
    user = db.session.get(User, actor_id)
    if not user or not user.is_admin:
        raise ValueError('仅管理员可以核实官方名录范围')


def register_school_baselines(school_id, baselines, inventory, actor_id, *, complete_roster=False):
    """Bind independent reviewed official rosters, never detector output counts.

    complete_roster records an explicit human scope attestation. It is not a
    crawler/AI confidence flag and does not by itself make the school ready.
    """
    _require_admin(actor_id)
    school = db.session.get(School, school_id)
    if not school or not isinstance(baselines, list) or not baselines:
        raise ValueError('请提供独立复核的完整官方名录')
    from backend.services.source_baselines import check_baseline, save_baseline_check
    checks, scopes, results = [], {}, []
    for baseline in baselines:
        if canonical_url(baseline.get('root_url', '')) != canonical_url(school.url):
            raise ValueError('独立名录不属于这所学校')
        if baseline.get('category') not in ('academic_units', 'institutional_units', 'administrative_units'):
            raise ValueError('学校机构基准必须是官方机构名录')
        check = check_baseline(inventory, baseline)
        if not check['scope_passed'] or not check['reference_current']:
            raise ValueError('独立名录与当前官网证据不一致，不能确认范围')
        results.append(check)
        proof = _snapshot(inventory.snapshot(site_key(school.url), baseline['reference_url']), baseline['reference_url'], role='roster')
        checks.append({'id': baseline['id'], 'category': baseline['category'], 'reference': proof,
                       'baseline': baseline, 'scope_passed': True})
        for item, result in zip(baseline['entries'], check['entries']):
            if item.get('kind', 'unit') != 'unit':
                continue
            key = result['keys'][0]
            scopes[key] = {'key': key, 'name': item['name'], 'url': item.get('url', ''),
                'state': 'not_checked', 'reference_url': baseline['reference_url'],
                'reference_hash': baseline['reference_hash'], 'baseline_id': baseline['id'],
                'priority_scopes': {category: {'state': 'not_checked'} for category in PRIORITY_SCOPES}}
    if not scopes:
        raise ValueError('独立名录没有机构条目')
    row = db.session.get(SchoolOnboarding, school_id)
    if not row:
        row = SchoolOnboarding(school_id=school_id)
        db.session.add(row)
    previous = {item['key']: item for item in json.loads(row.scope_json or '[]')}
    for key, scope in scopes.items():
        old = previous.get(key)
        if old and old.get('reference_hash') == scope['reference_hash']:
            scope.update(state=old.get('state', 'not_checked'), priority_scopes=old.get('priority_scopes', scope['priority_scopes']))
    checkpoint = json.loads(row.checkpoint_json or '{}')
    checkpoint['independent_rosters'] = checks
    checkpoint['reference_checks'] = results
    checkpoint['complete_roster_review'] = {'confirmed': complete_roster is True, 'actor_id': actor_id,
                                           'reviewed_at': datetime.utcnow().isoformat()}
    row.scope_json, row.checkpoint_json = _json(list(scopes.values())), _json(checkpoint)
    row.state = 'needs_review'
    db.session.commit()
    for baseline in baselines:
        save_baseline_check(inventory, baseline)
    return school_governance_status(school_id)


def review_unit_scope(school_id, unit_key, state, actor_id, *, evidence_reference, priority_scopes, note):
    _require_admin(actor_id)
    if state not in SCOPE_STATES or not str(note).strip():
        raise ValueError('请提供机构核实结论与依据')
    if not isinstance(priority_scopes, dict) or set(priority_scopes) != set(PRIORITY_SCOPES):
        raise ValueError('请逐项说明通知、本科、研究生、招生和学生事务的检查情况')
    if any(not isinstance(value, dict) or value.get('state') not in SCOPE_STATES for value in priority_scopes.values()):
        raise ValueError('重点信息范围结论格式不正确')
    row = db.session.get(SchoolOnboarding, school_id)
    scopes = json.loads(row.scope_json or '[]') if row else []
    scope = next((item for item in scopes if item['key'] == unit_key), None)
    if not scope or not scope.get('baseline_id'):
        raise ValueError('请先核实该机构在官方完整名录中的身份')
    if scope.get('reference_changed'):
        raise ValueError('官方名录已经变化，请先复核独立基准')
    if state == 'verified' and not any(value['state'] == 'verified' for value in priority_scopes.values()):
        raise ValueError('已接通机构必须有至少一项实际接通的范围')
    known_references = {}
    for proposal in SourceProposal.query.filter_by(school_id=school_id):
        bundle = json.loads(proposal.evidence_json)
        for reference in [bundle.get('list'), bundle.get('independent'), *bundle.get('identity_snapshots', [])]:
            if reference:
                known_references[(reference.get('url'), reference.get('hash'))] = reference
    def reference_in_unit(reference):
        actual = known_references.get((reference.get('url'), reference.get('hash')))
        if (not actual or actual.get('role') not in ('list', 'identity', 'independent_list')
                or not scope.get('url') or not _inside(actual['url'], scope['url'])):
            raise ValueError('检查依据必须来自服务器已读取的本机构栏目或导航页面')
        read_snapshot(actual)
        return actual
    evidence_reference = reference_in_unit(evidence_reference)
    for category, conclusion in priority_scopes.items():
        if conclusion['state'] not in COMPLETE_SCOPE_STATES:
            continue
        proof = conclusion.get('evidence_reference')
        if not isinstance(proof, dict) or not str(conclusion.get('note', '')).strip():
            raise ValueError('每项已完成结论都需要对应入口证据与核实说明')
        reference_in_unit(proof)
        if conclusion['state'] == 'verified':
            ids = conclusion.get('source_ids')
            if not isinstance(ids, list) or not ids or any(type(ident) is not int for ident in ids):
                raise ValueError('每项已接通范围必须指定实际通过检查的栏目')
            for ident in ids:
                department = db.session.get(Department, ident)
                latest = SourceConfigVersion.query.filter_by(department_id=ident).order_by(SourceConfigVersion.version.desc()).first()
                if (not department or department.school_id != school_id or department.group_name != scope['name']
                        or not latest or latest.config_hash != _hash(source_config(department))):
                    raise ValueError('范围中的栏目未通过检查或不属于本机构')
                active_proposal = db.session.get(SourceProposal, latest.proposal_id)
                source_bundle = json.loads(active_proposal.evidence_json) if active_proposal else {}
                allowed = [source_bundle.get('list'), source_bundle.get('independent')]
                if not any(ref and (ref['url'], ref['hash']) == (proof['url'], proof['hash']) for ref in allowed):
                    raise ValueError('范围证据与指定栏目不匹配')
    scope.update(state=state, priority_scopes=priority_scopes, conclusion_evidence=evidence_reference,
                 note=str(note)[:2000], reviewed_by=actor_id, reviewed_at=datetime.utcnow().isoformat())
    row.scope_json = _json(scopes)
    db.session.commit()
    evaluate_school_readiness(school_id)
    return school_governance_status(school_id)


def rollback_source_version(department_id, version, actor_id):
    """Rollback is a newly verified proposal, never a direct selector overwrite."""
    _require_admin(actor_id)
    department = db.session.get(Department, department_id)
    previous = SourceConfigVersion.query.filter_by(department_id=department_id, version=version).first()
    if not department or not previous:
        raise ValueError('来源版本不存在')
    config = json.loads(previous.config_json)
    proposal = propose_source(department.school_id, config, department_id=department_id, origin='rollback')
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, actor_id=actor_id, action='request_rollback',
                   detail_json=_json({'version': version})))
    db.session.commit()
    from backend.services.tasks import enqueue
    task = enqueue('source_review', proposal.id, {'proposal_id': proposal.id})
    return {'proposal_id': proposal.id, 'task_id': task.id, 'state': proposal.state,
            'message': '历史配置已加入重新检查，通过后恢复生效'}


def prune_governance_evidence(days=30, *, all_cache=False):
    """Only expired orphans may be removed; review/active/history proofs survive."""
    import time
    protected = set()
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('hash', 'reference_hash') and isinstance(item, str) and re.fullmatch('[0-9a-f]{64}', item):
                    protected.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    version_proposals = {ident for (ident,) in db.session.query(SourceConfigVersion.proposal_id) if ident is not None}
    for proposal in SourceProposal.query.all():
        if proposal.state not in ('rejected', 'stale') or proposal.id in version_proposals:
            collect(json.loads(proposal.evidence_json))
    for school in SchoolOnboarding.query.all():
        collect(json.loads(school.checkpoint_json))
        collect(json.loads(school.scope_json))
    # A just-captured snapshot may not be bound to a proposal until another
    # network check finishes. Even "clear caches" respects this safety window.
    cutoff = time.time() - max(1, 1 if all_cache else int(days or 365000)) * 86400
    root, removed, size, files = _evidence_root(), 0, 0, 0
    if root.exists():
        for path in root.glob('*.html.gz'):
            digest = path.name.removesuffix('.html.gz')
            if not re.fullmatch('[0-9a-f]{64}', digest) or path.is_symlink():
                continue
            if digest not in protected and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
            else:
                size += path.stat().st_size
                files += 1
    return {'removed': removed, 'files': files, 'bytes': size, 'protected': len(protected)}


def evaluate_school_readiness(school_id):
    row = db.session.get(SchoolOnboarding, school_id)
    if not row:
        return False
    checkpoint = json.loads(row.checkpoint_json or '{}')
    scopes = json.loads(row.scope_json or '[]')
    rosters = checkpoint.get('independent_rosters', [])
    confirmed = checkpoint.get('complete_roster_review', {}).get('confirmed') is True
    complete = bool(confirmed and rosters and scopes and not row.pending_pages)
    for scope in scopes:
        complete = complete and scope['state'] in COMPLETE_SCOPE_STATES and not scope.get('reference_changed')
        priorities = scope.get('priority_scopes', {})
        complete = complete and set(priorities) == set(PRIORITY_SCOPES) and all(
            value.get('state') in COMPLETE_SCOPE_STATES for value in priorities.values())
    latest_checks = {check['id']: check for check in checkpoint.get('reference_checks', [])}
    for roster in rosters:
        reference = latest_checks.get(roster['id'])
        if reference is None or not reference.get('scope_passed') or not reference.get('reference_current'):
            complete = False
        try:
            read_snapshot(roster['reference'])
        except ValueError:
            complete = False
    if complete:
        row.state, row.reviewed_at = 'ready', datetime.utcnow()
    elif row.state == 'ready':
        row.state = 'partial'
    db.session.commit()
    return bool(complete)


def propose_detected_source(department, config, html, *, origin='repair'):
    """Collection can record suggestions, but may not ingest an unvalidated list."""
    candidate = dict(source_config(department), **{k: v for k, v in config.items() if k in FIELDS and k != 'name'})
    evidence = CapturedEvidence({'schema': 1, 'school_id': department.school_id,
        'school_name': department.school.name, 'root_url': department.school.url,
        'config_hash': _hash(candidate), 'expected_config': source_config(department),
        'list': _snapshot(html, getattr(html, 'final_url', department.list_url)),
        'articles': [], 'identity_paths': [], 'identity_snapshots': []})
    proposal = propose_source(department.school_id, candidate, evidence, department_id=department.id, origin=origin)
    validate_proposal(proposal.id)
    return proposal


def source_skill_suggestions(school_id, evidence_bundle, execution_id, expected_version=None):
    from backend.ai.runtime import run_skill
    result = run_skill(skill_id='university-source-onboarding', mode='extraction',
        evidence=evidence_bundle, purpose='directory', execution_id=execution_id, expected_version=expected_version)
    created = []
    if result.get('status') == 'succeeded':
        for item in (result.get('output') or {}).get('proposals', []):
            if item.get('decision') == 'propose':
                proposal = propose_source(school_id, item['config'], origin='skill')
                created.append(proposal.id)
    # These are unvalidated suggestions, even when the model labels them propose.
    return dict(result, proposal_ids=created)


def process_discovered_candidates(school_id, inventory, key, *, limit=5):
    """Bound verification separately from the twenty-page discovery slice."""
    from backend.services.source_catalog import publication_candidates
    report = inventory.report(key)
    candidates = publication_candidates(report, inventory.structure(key), focus='all')
    candidate_urls = {canonical_url(item['list_url']) for item in candidates}
    for page in report['pages']:
        address = page.get('final_url') or page['url']
        if (page['state'] == 'fetched' and page['kind'] == 'channel'
                and not page.get('feed_json') and canonical_url(address) not in candidate_urls
                and page.get('health') not in ('dynamic_content', 'unreachable', 'retirement_notice')):
            candidates.append({'name': page['label'] or '官网栏目待核实', 'list_url': address,
                               'list_selector': '', '_entry_only': True})
    from backend.services.student_sources import display_priority
    candidates.sort(key=lambda item: display_priority(item['name']))
    result = {'proposal_ids': [], 'activated_ids': [], 'remaining_candidates': 0}
    known = SourceProposal.query.filter_by(school_id=school_id).all()
    known_configs = {_hash(json.loads(item.candidate_json)): item for item in known}
    known_identities = {item.identity_key: item for item in known}
    existing = Department.query.filter_by(school_id=school_id).all()
    handled = 0
    for raw in candidates:
        from backend.services.discovery_control import pause_if_requested
        pause_if_requested()
        config = {field: raw.get(field, '') for field in FIELDS}
        from backend.services.source_review_recovery import page_problem
        cached = inventory.snapshot(key, config['list_url'])
        if cached and page_problem(cached) in ('article_instead_of_column', 'search_instead_of_column'):
            continue
        digest = _hash(config)
        initial_identity = _hash([school_id, None, canonical_url(config['list_url']), config['list_selector']])
        known_proposal = known_configs.get(digest) or known_identities.get(initial_identity)
        old_validation = json.loads(known_proposal.validation_json) if known_proposal else {}
        retry_snapshot = bool(known_proposal and known_proposal.state == 'needs_review'
            and old_validation.get('validator_version') == 'source-governance-1'
            and '网页证据已变化，请重新检查' in old_validation.get('errors', []))
        if (known_proposal and known_proposal.state not in ('proposed', 'validated') and not retry_snapshot) or any(source_config(dept) == config for dept in existing):
            continue
        if handled >= limit:
            result['remaining_candidates'] += 1
            continue
        handled += 1
        if known_proposal:
            # Resume a model-revised candidate using its persisted config; the
            # original detector result must not overwrite the revision on yield.
            config = json.loads(known_proposal.candidate_json)
        # Same URL with a new widget is a separate source; an existing unit
        # homepage cannot be repurposed as its discovered first notice list.
        page = next((p for p in report['pages'] if canonical_url(p.get('final_url') or p['url']) == canonical_url(config['list_url'])), None)
        seed = inventory.snapshot(key, page['url']) if page else None
        proposal = known_proposal or propose_source(school_id, config,
            origin='submitted_entry' if not config['list_selector'] else 'discovery')
        result['proposal_ids'].append(proposal.id)
        try:
            if proposal.state == 'validated':
                department = activate_proposal(proposal.id)
                result['activated_ids'].append(department.id)
                continue
            if not config['list_selector']:
                reviewed = process_source_review({'proposal_id': proposal.id})
                if reviewed.get('state') == 'activated':
                    result['activated_ids'].append(reviewed['department_id'])
                continue
            evidence = capture_source_evidence(school_id, config, seed_html=seed, inventory=inventory)
            validation = validate_proposal(proposal.id, independent_evidence=evidence)
            if not validation['passed']:
                skill = run_source_skill_for_proposal(proposal.id, inventory=inventory)
                if skill.get('changed'):
                    validation = json.loads(proposal.validation_json)
            if validation['passed']:
                department = activate_proposal(proposal.id)
                result['activated_ids'].append(department.id)
        except Exception as exc:
            db.session.rollback()
            proposal = db.session.get(SourceProposal, proposal.id)
            proposal.state = 'needs_review'
            proposal.validation_json = _json({'passed': False, 'errors': [str(exc)[:300]], 'validator_version': VERSION})
            db.session.commit()
    return result


def verify_source_proposal(proposal_id, *, fetcher=None, inventory=None):
    """Worker entrypoint for admin recheck, import and repaired list candidates."""
    proposal = db.session.get(SourceProposal, proposal_id)
    if not proposal or proposal.state in ('activated', 'rejected', 'superseded'):
        return serialize_proposal(proposal) if proposal else None
    config = json.loads(proposal.candidate_json)
    evidence = capture_source_evidence(proposal.school_id, config, department_id=proposal.department_id,
                                      inventory=inventory, fetcher=fetcher)
    validation = validate_proposal(proposal.id, independent_evidence=evidence)
    if not validation['passed']:
        skill = run_source_skill_for_proposal(proposal.id, inventory=inventory)
        if skill.get('changed'):
            validation = json.loads(proposal.validation_json)
    if validation['passed']:
        activate_proposal(proposal.id)
    return serialize_proposal(proposal)


def run_source_skill_for_proposal(proposal_id, *, inventory=None):
    """At most a draft and one revision; model results still face the same gate."""
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    proposal = db.session.get(SourceProposal, proposal_id)
    if not proposal or proposal.state in ('activated', 'rejected', 'superseded'):
        return {'status': 'skipped', 'changed': False}
    bundle = json.loads(proposal.evidence_json)
    if not bundle.get('list'):
        return {'status': 'missing_evidence', 'changed': False}
    from backend.services.source_review_recovery import page_problem
    if page_problem(read_snapshot(bundle['list'])):
        return {'status': 'not_a_public_column', 'changed': False}
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        return {'status': 'not_configured', 'changed': False}
    # Reserve the proposal briefly even outside the normal directory-worker
    # lease. Two invocations cannot each spend a fresh draft/revision allowance.
    db.session.execute(db.update(SourceProposal).where(SourceProposal.id == proposal.id)
                       .values(updated_at=datetime.utcnow()))
    db.session.refresh(proposal)
    bundle = json.loads(proposal.evidence_json)
    attempt_events = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='skill_attempt').all()
    resume_event = next((event for event in attempt_events if json.loads(event.detail_json).get('status') is None), None)
    if any(json.loads(event.detail_json).get('status') in ('pending', 'uncertain') for event in attempt_events):
        db.session.commit()
        return {'status': 'result_pending_review', 'changed': False}
    attempts = len(attempt_events)
    if attempts >= 2 and resume_event is None:
        db.session.commit()
        return {'status': 'review_required', 'changed': False}
    config = json.loads(proposal.candidate_json)
    html = read_snapshot(bundle['list'])
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup.select('script,style,iframe'):
        tag.decompose()
    observed = {bundle['list']['url'], config['list_url']}
    for anchor in soup.select('a[href]'):
        target = canonical_url(urljoin(bundle['list']['url'], anchor.get('href', '')))
        if target:
            observed.add(target)
    candidate_id = 'source-' + str(proposal.id)
    references = [{'evidence_id': 'list', 'url': bundle['list']['url'], 'html': str(soup)[:160000],
                   'scope': 'available DOM excerpt; all proposed selectors require full-page verification'}]
    entities = []
    for path in bundle.get('identity_paths', []):
        entities.append({'id': path['unit_key'], 'name': path['unit_name']})
        references.append({'evidence_id': 'identity-' + str(len(references)), 'paths': path})
    evidence = {'school_id': proposal.school_id, 'candidates': [{'candidate_id': candidate_id, 'config': config,
                'validation': json.loads(proposal.validation_json)}], 'entities': entities,
                'evidence': references, 'observed_urls': sorted(observed)}
    execution_id = (json.loads(resume_event.detail_json)['execution_id'] if resume_event else
                    'source:' + str(proposal.id) + ':' + str(attempts) + ':' + proposal.evidence_hash[:20])
    expected_hash = proposal.expected_config_hash
    expected_revision = proposal.revision
    event = resume_event or SourceReviewEvent(proposal_id=proposal.id, action='skill_attempt',
                   detail_json=_json({'execution_id': execution_id, 'attempt': attempts + 1,
                                      'revision': proposal.revision, 'evidence_hash': proposal.evidence_hash}))
    if resume_event:
        detail = json.loads(resume_event.detail_json)
        if detail.get('revision') != proposal.revision or detail.get('evidence_hash') != proposal.evidence_hash:
            db.session.commit()
            return {'status': 'stale', 'changed': False}
        attempts = detail['attempt'] - 1
    db.session.add(event)
    db.session.commit()
    event_id = event.id
    result = run_skill('university-source-onboarding', 'extraction', evidence, 'directory', execution_id,
                       expected_version=binding['version'], binding=binding)
    event = db.session.get(SourceReviewEvent, event_id)
    detail = json.loads(event.detail_json)
    detail['status'] = result.get('status')
    event.detail_json = _json(detail)
    db.session.commit()
    if result.get('status') != 'succeeded':
        return {'status': result.get('status', 'failed'), 'changed': False}
    from backend.services import tasks
    tasks.assert_owned()
    db.session.refresh(proposal)
    if proposal.expected_config_hash != expected_hash or proposal.revision != expected_revision:
        return {'status': 'stale', 'changed': False}
    suggestion = (result.get('output') or {}).get('proposals', [{}])[0]
    if suggestion.get('decision') != 'propose':
        return {'status': 'review_required', 'changed': False}
    new_config = _candidate(suggestion['config'])
    # The runtime also checks observed URLs; this extra source check binds the
    # output to the one proposal being repaired, not an arbitrary other widget.
    if new_config['list_url'] not in observed or suggestion.get('candidate_id') != candidate_id:
        return {'status': 'invalid_reference', 'changed': False}
    proposal.candidate_json = _json(new_config)
    proposal.revision += 1
    proposal.validated_hash = None
    proposal.state = 'proposed'
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='skill_suggestion',
        detail_json=_json({'provenance': result.get('provenance', {}), 'previous': config})))
    db.session.commit()
    captured = capture_source_evidence(proposal.school_id, new_config, department_id=proposal.department_id, inventory=inventory)
    validation = validate_proposal(proposal.id, independent_evidence=captured)
    # One bounded revision may use the failed check as input. No retry occurs
    # after a supplier result-unknown status or a proposed config being rejected.
    if not validation['passed'] and attempts == 0:
        return run_source_skill_for_proposal(proposal.id, inventory=inventory)
    return {'status': 'validated' if validation['passed'] else 'review_required', 'changed': True}


def queue_source_review(school_id, candidate, *, department_id=None, requested_by=None, subscribe=False):
    from backend.services.tasks import enqueue
    proposal = propose_source(school_id, candidate, department_id=department_id,
                              origin='configuration' if candidate.get('list_selector') else 'submitted_entry')
    if subscribe and requested_by:
        prior = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, actor_id=requested_by, action='subscribe_on_activation').first()
        if prior is None:
            db.session.add(SourceReviewEvent(proposal_id=proposal.id, actor_id=requested_by, action='subscribe_on_activation'))
            db.session.commit()
        if proposal.state == 'activated' and proposal.department_id:
            from backend.database.models import Subscription
            department = db.session.get(Department, proposal.department_id)
            subscription = Subscription.query.filter_by(school_id=school_id, user_id=requested_by).first()
            if department and department.school_id == school_id and subscription:
                if subscription.department_ids is not None:
                    subscription.department_ids = sorted(set(subscription.department_ids + [department.id]))
                db.session.commit()
                return {'proposal_id': proposal.id, 'task_id': None, 'state': 'activated',
                        'message': '栏目已通过核实，已加入你的订阅'}
    task = enqueue('source_review', proposal.id, {'proposal_id': proposal.id})
    return {'proposal_id': proposal.id, 'task_id': task.id, 'state': proposal.state,
            'message': '来源已加入核实队列，通过网页与归属检查后生效'}


def process_source_review(payload):
    """Shared queue handler, including selector-free public submissions."""
    from backend.scraper.acquisition import FetchFailure
    try:
        return _process_source_review(payload)
    except FetchFailure as exc:
        db.session.rollback()
        proposal = db.session.get(SourceProposal, payload['proposal_id'])
        if not proposal:
            return {'state': 'missing'}
        proposal.state = 'needs_review'
        proposal.validation_json = _json({'passed': False, 'errors': [str(exc)[:300]], 'validator_version': VERSION})
        proposal.validator_version = VERSION
        db.session.commit()
        return serialize_proposal(proposal)


def recover_source_reviews():
    from backend.services.source_review_recovery import schedule_recovery
    return schedule_recovery()


def _process_source_review(payload):
    proposal = db.session.get(SourceProposal, payload['proposal_id'])
    if not proposal or proposal.state in ('rejected', 'activated', 'superseded'):
        return serialize_proposal(proposal) if proposal else {'state': 'missing'}
    config = json.loads(proposal.candidate_json)
    seed = None
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services.source_review_recovery import cached_page, resolve_non_column, page_problem
    catalog = RuntimeCatalog(current_app.config.get('SOURCE_CATALOG_PATH'))
    cached = cached_page(proposal, catalog)
    if cached and page_problem(cached) in ('article_instead_of_column', 'search_instead_of_column') and resolve_non_column(proposal, cached):
        return serialize_proposal(proposal)
    if not config.get('list_selector'):
        url = config['list_url']
        db.session.commit()
        seed = _fetch(url, 'list')
        if resolve_non_column(proposal, seed):
            return serialize_proposal(proposal)
        from backend.scraper.discovery.publication_lists import publication_lists
        feeds = [f for f in publication_lists(seed, getattr(seed, 'final_url', url))
                 if f.get('name') and not f.get('heading_ambiguous')]
        exact = [feed for feed in feeds if _normal(feed['name']) == _normal(config['name'])]
        selected = exact if exact else feeds
        if len(selected) != 1:
            proposal.state = 'needs_review'
            school = db.session.get(School, proposal.school_id)
            proposal.evidence_json = _json({'schema': 1, 'school_id': school.id, 'school_name': school.name,
                'root_url': school.url, 'config_hash': _hash(config),
                'expected_config': source_config(db.session.get(Department, proposal.department_id)) if proposal.department_id else {},
                'list': _snapshot(seed, getattr(seed, 'final_url', url)), 'articles': [],
                'identity_paths': [], 'identity_snapshots': []})
            proposal.evidence_hash = _hash(json.loads(proposal.evidence_json))
            proposal.validation_json = _json({'passed': False, 'errors': ['column_region_requires_review'],
                                             'available_columns': [f['name'] for f in feeds]})
            db.session.commit()
            from backend.services.runtime_catalog import RuntimeCatalog
            catalog = RuntimeCatalog(current_app.config.get('SOURCE_CATALOG_PATH'))
            skill = run_source_skill_for_proposal(proposal.id, inventory=catalog)
            if skill.get('changed') and json.loads(proposal.validation_json).get('passed'):
                activate_proposal(proposal.id)
            return serialize_proposal(proposal)
        feed = selected[0]
        config.update({field: feed.get(field, config.get(field, '')) for field in FIELDS
                       if field not in ('list_url', 'group_name')})
        config = _candidate(config)
        proposal.candidate_json = _json(config)
        proposal.revision += 1
        proposal.validated_hash = None
        db.session.commit()
    if not config.get('group_name'):
        from backend.services.source_relationships import SourceRelationships
        key = site_key(db.session.get(School, proposal.school_id).url)
        report = catalog.report(key)
        if report:
            relations = SourceRelationships(report, catalog.structure(key))
            owners = relations.publication_owners(config['list_url'], relations.paths_for(config['list_url']))
            if len(owners) == 1:
                config['group_name'] = next(iter(owners))
                proposal.candidate_json = _json(config)
                proposal.revision += 1
                proposal.validated_hash = None
                db.session.commit()
    evidence = capture_source_evidence(proposal.school_id, config, department_id=proposal.department_id,
                                      seed_html=seed, inventory=catalog)
    validation = validate_proposal(proposal.id, independent_evidence=evidence)
    if not validation['passed']:
        skill = run_source_skill_for_proposal(proposal.id, inventory=catalog)
        if skill.get('changed'):
            validation = json.loads(proposal.validation_json)
    if validation['passed']:
        activate_proposal(proposal.id)
    return serialize_proposal(proposal)
