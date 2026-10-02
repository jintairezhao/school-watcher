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

VERSION = 'source-governance-7'
FIELDS = ('name', 'list_url', 'list_selector', 'title_selector', 'link_selector',
          'date_selector', 'content_selector', 'group_name')
ACTION_LABEL = re.compile(r'^(?:read(?:\s+more)?|more|learn\s+more|了解|更多|查看|详情)$', re.I)
# The address is not a publication column, so sampling its entries as articles
# would prove nothing. Every other preflight finding is a reason to gather more
# evidence, never a reason to stop gathering it.
PAGE_PROBLEMS = ('article_instead_of_column', 'search_instead_of_column', 'source_login_required')
# The heading detector could not name this column, so discovery carries it under
# a sentinel. A sentinel is a question, never an identity: comparing it against a
# page heading for equality can only ever fail, which is what turned 63 real
# columns into "scope unconfirmed" without a single sample ever being read.
UNVERIFIED_COLUMN_NAME = '栏目名称待核实'
# Official sites mix public notices with entries only their own members may read.
# A gated entry is a fact about the site, not a defect in the column and not a
# parser task, so it is recorded as a limitation instead of a validation error.
# Bodies are read a few at a time; when the site gates the first entries the
# sample extends within this ceiling rather than abandoning a real public column.
ARTICLE_SAMPLE_TARGET = 3
ARTICLE_SAMPLE_LIMIT = 5
GATED_OUTCOMES = ('denied', 'needs_manual')
GATED_CODES = ('source_login_required', 'access_denied', 'access_denied_page',
               'human_verification', 'access_challenge', 'http_401', 'http_403')


def gated_sample(exc):
    """True when the site refused this entry rather than the program misreading it."""
    from backend.scraper.acquisition import FetchFailure
    return isinstance(exc, FetchFailure) and (exc.outcome in GATED_OUTCOMES
                                              or exc.error_code in GATED_CODES)


def unverified_column_name(name):
    return not (name or '').strip() or (name or '').strip() == UNVERIFIED_COLUMN_NAME


def adoptable_column_name(name):
    """A heading worth installing as a column name: real text, not a call to action."""
    text = (name or '').strip()
    return len(_normal(text)) >= 2 and not ACTION_LABEL.match(text)


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


def _fetch(url, purpose, *, config=None):
    from backend.scraper.engine import _fetch_html
    policy = {'exploration': config is None or purpose == 'directory'}
    config = config or {}
    selector = (config.get('content_selector', '') if purpose in ('body', 'article') else
                config.get('list_selector', '') if purpose in ('list', 'independent_list') else '')
    if purpose == 'independent_list':
        return _fetch_html(url, purpose='list', raise_fetch_errors=True,
                           readiness_selector=selector, policy={**policy, 'verification_pass': 'independent'})
    # Evidence roles use "body"; the shared transport calls that purpose "article".
    return _fetch_html(url, purpose='article' if purpose == 'body' else purpose,
                       readiness_selector=selector, policy=policy, raise_fetch_errors=True)


def _fetch_snapshot(url, purpose, fetcher):
    from backend.scraper.acquisition import FetchFailure
    try:
        html = fetcher(url, purpose)
    except FetchFailure as exc:
        if purpose not in ('list', 'independent_list') or exc.outcome != 'needs_adapter' or not exc.result.html:
            raise
        # The transport's generic list recognizer is not the authority for an
        # AI-declared CSS plan. Retain readable material and let _column_scope,
        # article identity and the independent sample validate the actual plan.
        return _snapshot(exc.result.html, exc.result.final_url or url,
                         outcome='needs_adapter', role=purpose)
    result = getattr(html, 'result', None)
    if result is not None and not result.ok:
        raise ValueError('官网返回了无效页面')
    outcome = getattr(result, 'outcome', 'usable')
    if outcome not in ('usable', 'empty'):
        raise ValueError('网页内容尚未就绪，不能审核来源')
    return _snapshot(html, getattr(html, 'final_url', url), outcome=outcome, role=purpose)


def _exploration_html(url):
    """Unknown readable markup is AI material, never a successful body sample."""
    from backend.scraper.acquisition import FetchFailure, FetchedHTML
    try:
        return _fetch(url, 'directory')
    except FetchFailure as exc:
        if exc.outcome == 'needs_adapter' and exc.result.html:
            return FetchedHTML(exc.result)
        raise


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


def _column_scope(config, reference, scope_evidence=None):
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
    # An unnamed candidate has no claim to confirm; the page heading is itself the
    # evidence, so scope is established structurally and the heading is returned.
    unnamed = unverified_column_name(config['name'])
    regions = []
    # A model can identify a region whose markup is unknown to the optional
    # template detector. Execute its declarative evidence against both reads;
    # never require a new CMS rule merely because a heading uses a div or span.
    if scope_evidence:
        from soupsieve.util import SelectorSyntaxError
        try:
            containers = soup.select(scope_evidence.get('container_selector', ''))
            headings = soup.select(scope_evidence.get('heading_selector', ''))
            if len(containers) == len(headings) == 1:
                container, heading = containers[0], headings[0]
                inside = lambda node: node is container or container in node.parents
                if (container.name not in ('html', 'body') and inside(heading)
                        and all(inside(item) for item in items)
                        and _normal(heading.get_text(' ', strip=True)) == expected):
                    regions.append({'name': heading.get_text(' ', strip=True),
                                    'container': scope_evidence['container_selector'],
                                    'heading': scope_evidence['heading_selector'], 'basis': 'executed_ai_scope'})
        except (ValueError, TypeError, SelectorSyntaxError):
            pass
    for feed in ([] if regions else publication_lists(html, reference['url'])):
        if not feed.get('name') or feed.get('heading_ambiguous'):
            continue
        if unnamed:
            if not adoptable_column_name(feed['name']):
                continue
        elif _normal(feed['name']) != expected:
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
            if len(headings) != 1:
                continue
            observed = headings[0].get_text(' ', strip=True)
            if unnamed:
                # Still one named region covering exactly these entries; it only
                # lacks a pre-existing claim to match. Never adopt a call to
                # action ("更多") or a degenerate heading as a column name.
                if not adoptable_column_name(observed):
                    continue
            elif _normal(observed) != expected:
                continue
            regions.append({'name': observed, 'container': parent.get('id', ''), 'heading': headings[0].name})
            break
    if not regions:
        errors.append('column_identity_or_scope_unconfirmed')
    return records, sorted(set(errors)), regions[0] if regions else {}


def _page_problem_errors(errors):
    """Preflight findings that mean the address is not a column at all."""
    return [error for error in errors if error in PAGE_PROBLEMS]


def capture_source_evidence(school_id, candidate, *, department_id=None, seed_html=None,
                            inventory=None, fetcher=None, scope_evidence=None, publisher_material=None):
    """Acquire bounded fresh checks. Called by trusted workers, never model output."""
    from backend.scraper.acquisition import FetchDeferred, FetchFailure
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
    fetcher = fetcher or (lambda url, purpose: _fetch(url, purpose, config=config))
    first = (_snapshot(seed_html, getattr(seed_html, 'final_url', config['list_url']),
                       outcome=getattr(getattr(seed_html, 'result', None), 'outcome', 'usable'))
             if seed_html is not None else _fetch_snapshot(config['list_url'], 'list', fetcher))
    records, errors, region = _column_scope(config, first, scope_evidence)
    # The official page's own region heading names an unnamed column. Adopt it
    # here, before the evidence hash binds this capture to a config, so the
    # proposal carries a name read from the site rather than a sentinel.
    if unverified_column_name(config['name']) and region.get('name'):
        config = _candidate({**config, 'name': region['name']})
        records, errors, region = _column_scope(config, first, scope_evidence)
    bundle = {'schema': 1, 'school_id': school_id, 'school_name': school_name, 'root_url': root_url,
              'config_hash': _hash(config), 'expected_config': previous, 'config': config,
              'list': first, 'articles': [], 'identity_paths': [], 'identity_snapshots': []}
    bundle['publisher_material'] = dict(publisher_material or {})
    if scope_evidence:
        bundle['scope_evidence'] = scope_evidence
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
                if not html:
                    stored = next((r for p in bundle['identity_paths'] for r in p.get('references', []) if r.get('url') == ref_url), {})
                    try:
                        html = read_snapshot({'hash': stored.get('content_hash', '')})
                    except ValueError:
                        pass
                if html:
                    bundle['identity_snapshots'].append(_snapshot(html, ref_url, role='identity'))
            # A published catalogue may retain a roster path after its small HTML
            # cache has expired. Recapture missing public evidence instead of
            # asking the model to rediscover an already observed department.
            captured = {r['url']: r for r in bundle['identity_snapshots']}
            for ref_url in references - set(captured):
                try:
                    captured[ref_url] = _fetch_snapshot(ref_url, 'directory', fetcher)
                    bundle['identity_snapshots'].append(captured[ref_url])
                except FetchDeferred:
                    raise
                except (FetchFailure, ValueError, OSError):
                    continue
            for path in bundle['identity_paths']:
                if path.get('unit_name') != config.get('group_name'):
                    continue
                unit = next((n for n in reversed(path.get('nodes', [])) if n.get('kind') == 'unit'), {})
                home = captured.get(unit.get('url'))
                directory = next((captured.get(r['url']) for r in path.get('references', [])
                                  if r['url'] != unit.get('url') and captured.get(r['url'])), None)
                if not home or not directory:
                    continue
                root_ref = next((r for r in bundle['publisher_material'].values() if r['url'] == canonical_url(root_url)), None)
                if not root_ref:
                    root_html = inventory.snapshot(key, root_url)
                    if not root_html:
                        page = inventory.get_page(key, root_url) or {}
                        try:
                            root_html = read_snapshot({'hash': page.get('content_hash', '')})
                        except ValueError:
                            pass
                    try:
                        root_ref = _snapshot(root_html, root_url, role='identity') if root_html else _fetch_snapshot(root_url, 'directory', fetcher)
                    except FetchDeferred:
                        raise
                    except (FetchFailure, ValueError, OSError):
                        continue
                bundle['publisher_material'].update(identity_root=root_ref, identity_directory=directory, identity_home=home)
                proof = {'directory_evidence_id': 'identity_directory', 'homepage_evidence_id': 'identity_home', 'name': path['unit_name']}
                from backend.services.source_workflow import publisher_proven
                if publisher_proven(dict(bundle, publisher_evidence=proof), config):
                    bundle['publisher_evidence'] = proof
                    break
    db.session.commit()
    records, errors, region = _column_scope(config, first, scope_evidence)
    bundle['region'] = region
    bundle['preflight_errors'] = errors
    # Acquire each kind of evidence on its own and record how each one went.
    # The list's own quality findings are findings, not a gate: an article body
    # and the independent page are exactly the evidence needed to settle an
    # unconfirmed column, so requiring a clean list first deadlocks every hard
    # case (it produced 74 body-sample and 74 independent-sample gaps with zero
    # article or independent evidence ever captured).
    bundle['samples'] = {}
    bundle['access_limited'] = []

    def sample(key, url, purpose, **extra):
        if not url:
            bundle['samples'][key] = 'no_target'
            return None
        try:
            snapshot = _fetch_snapshot(url, purpose, fetcher)
        except FetchDeferred:
            raise
        except FetchFailure as exc:
            # Kept distinguishable from a plain failure: "the site would not let
            # us read it" is not "the program could not read it".
            bundle['samples'][key] = (('access_limited:' + (exc.error_code or exc.outcome))
                                      if gated_sample(exc) else 'failed: ' + str(exc)[:160])
            if purpose == 'body' and exc.outcome == 'needs_adapter' and exc.result.html:
                bundle.setdefault('exploration_pages', []).append(_snapshot(exc.result.html,
                    exc.result.final_url or url, role='article', outcome=exc.outcome))
            return None
        except (ValueError, OSError) as exc:
            bundle['samples'][key] = 'failed: ' + str(exc)[:160]
            return None
        bundle['samples'][key] = 'obtained'
        snapshot.update(extra)
        return snapshot

    # A single unreachable sample must not cancel the others, so the page-level
    # problems are the only preconditions: they mean the address is not a
    # publication column at all, where sampling entries would be meaningless.
    if not _page_problem_errors(errors):
        # Fewer than three bodies is enough to confirm an article list, but a
        # gated entry proves nothing either way, so the sample extends to the
        # ceiling while the site keeps refusing — and stops at the first body it
        # can read. A column whose entries are all gated still ends up with no
        # article evidence at all, which validation refuses.
        for index, record in enumerate(records[:ARTICLE_SAMPLE_LIMIT]):
            article = sample('article:' + record['url'], record['url'], 'body',
                             listed_title=record['title'], listed_url=record['url'])
            if article:
                bundle['articles'].append(article)
            if bundle['articles'] and index + 1 >= ARTICLE_SAMPLE_TARGET:
                break
        bundle['access_limited'] = [key.split(':', 1)[1] for key, value in bundle['samples'].items()
                                    if key.startswith('article:') and str(value).startswith('access_limited:')]
        from backend.scraper.engine import _next_page_url
        next_url = _next_page_url(read_snapshot(first), first['url'], 1)
        if next_url:
            bundle['pagination'] = sample('pagination', next_url, 'list', observed_url=next_url)
        # The independent read is required precisely when the first pass is
        # unclear, so it is never conditional on the list being clean.
        bundle['independent'] = sample('independent', config['list_url'], 'independent_list')
        if (config['group_name'] in ('', school_name) and canonical_url(config['list_url']) != canonical_url(root_url)
                and not any(canonical_url(ref['url']) == canonical_url(root_url) for ref in bundle['publisher_material'].values())):
            home = sample('school_home', root_url, 'directory')
            if home:
                bundle['publisher_material']['school_home'] = home
    return CapturedEvidence(bundle)


def propose_source(school_id, candidate, evidence=None, *, department_id=None, origin='discovery', expected_config=None, commit=True):
    # Evidence carries the config it was captured against, including a column name
    # read from the page. Preferring it keeps the evidence hash and the proposal
    # in agreement instead of demanding the caller re-derive the same correction.
    if evidence is not None and isinstance(evidence, CapturedEvidence) and evidence.bundle.get('config'):
        candidate = evidence.bundle['config']
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
    from backend.services.source_workflow import publisher_proven, school_publisher_proven
    if publisher_proven(bundle, config) or school_publisher_proven(bundle, config):
        return []
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
        # Fresh evidence may carry a column name read from the page for a
        # candidate that never had one. Adopt it before the agreement check, so
        # the proposal and its evidence describe the same config. A real name is
        # never silently replaced: that stays a mismatch and is refused below.
        captured = bundle.get('config')
        if (captured and _hash(captured) == bundle.get('config_hash')
                and unverified_column_name(config['name'])):
            previous_name = config['name']
            config = _candidate(captured)
            proposal.candidate_json = _json(config)
            db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='column_named_from_page',
                detail_json=_json({'previous_name': previous_name, 'observed_name': config['name'],
                                   'region': bundle.get('region', {}), 'list_url': config['list_url']})))
        if bundle.get('config_hash') != _hash(config) or bundle.get('school_id') != proposal.school_id:
            raise ValueError('复测证据不属于此配置')
        proposal.evidence_json, proposal.evidence_hash = _json(bundle), _hash(bundle)
    errors, records, limitations = [], [], []
    if not bundle or not bundle.get('list'):
        errors.append('evidence_missing')
    else:
        try:
            if _hash(bundle) != proposal.evidence_hash or bundle.get('config_hash') != _hash(config):
                raise ValueError('evidence_changed')
            records, errors, region = _column_scope(config, bundle['list'], bundle.get('scope_evidence'))
            errors += _identity_errors(proposal, config, bundle)
            independent = bundle.get('independent')
            if not independent:
                errors.append('independent_sample_missing')
            else:
                _, independent_errors, _ = _column_scope(config, independent, bundle.get('scope_evidence'))
                errors += ['independent_' + e for e in independent_errors]
            # Entries the capture actually tried to read, so a gated entry is
            # judged as gated rather than reported as a missing sample.
            sampled = {key[len('article:'):]: value
                       for key, value in (bundle.get('samples') or {}).items() if key.startswith('article:')}
            articles = {ref.get('listed_url'): ref for ref in bundle.get('articles', [])}
            if not sampled and records:
                # Capture never sampled (the address is not a column at all);
                # the entries stay required and unconfirmed, exactly as before.
                sampled = {record['url']: 'unsampled' for record in records[:ARTICLE_SAMPLE_TARGET]}
            conclusive = 0
            for url, outcome in sorted(sampled.items()):
                ref = articles.get(url)
                if not ref:
                    if str(outcome).startswith('access_limited:'):
                        limitations.append(outcome + ':' + url)
                    else:
                        errors.append('article_sample_missing')
                    continue
                conclusive += 1
                record = next((item for item in records if item['url'] == url), {'title': ''})
                soup = BeautifulSoup(read_snapshot(ref), 'lxml')
                visible = _normal(soup.get_text(' ', strip=True))
                title = _normal(record['title'])
                if ref.get('outcome') != 'usable' or title[:min(len(title), 20)] not in visible:
                    errors.append('article_identity_mismatch')
                body = soup.select(config['content_selector']) if config.get('content_selector') else [soup]
                body_text = _normal(' '.join(node.get_text(' ', strip=True) for node in body))
                if len(body_text) < 15 or len(visible) < len(title) + 15:
                    errors.append('article_body_missing')
            if sampled and not conclusive:
                # Nothing here could be confirmed as an article of this column.
                # That is what the missing-sample check exists for, so a list
                # whose every entry is unreadable is still refused. The refusal
                # stands either way; only the reason changes when the site's own
                # sign-in requirement is what made every entry unreadable.
                # "缺少正文样本，请重新检查" would send the reader to re-check a
                # column that is simply not public, and no re-check can help.
                gated = [str(value) for value in sampled.values()
                         if str(value).startswith('access_limited:')]
                errors.append('source_login_required'
                              if gated and len(gated) == len(sampled) else 'article_sample_missing')
            from backend.scraper.engine import _next_page_url
            next_url = _next_page_url(read_snapshot(bundle['list']), bundle['list']['url'], 1)
            if next_url:
                pagination = bundle.get('pagination')
                if not pagination or canonical_url(pagination.get('observed_url', '')) != canonical_url(next_url):
                    errors.append('pagination_sample_missing')
                else:
                    next_records, next_errors, _ = _column_scope(config, pagination, bundle.get('scope_evidence'))
                    errors += ['pagination_' + e for e in next_errors]
                    if next_records and {r['url'] for r in next_records} == {r['url'] for r in records}:
                        errors.append('pagination_repeats_first_page')
        except (ValueError, OSError) as exc:
            errors.append(str(exc)[:160])
    errors = sorted(set(errors))
    limitations = sorted(set(limitations))
    # A column whose entries are partly gated still installs; the gate is reported
    # alongside the result so the review shows what the site would not hand over.
    result = {'passed': not errors, 'errors': errors, 'item_count': len(records),
              'samples': records[:3], 'limitations': limitations, 'validator_version': VERSION}
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
    from backend.services.directory_work import resolve_column_material
    resolve_column_material(proposal)
    # Submission is a pending intent, never an early subscription to a guessed
    # source. Only still-subscribed users receive the now-validated column.
    from backend.database.models import Subscription
    for event in SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='subscribe_on_activation'):
        sub = Subscription.query.filter_by(school_id=proposal.school_id, user_id=event.actor_id).first()
        if sub and sub.department_ids is not None:
            sub.department_ids = sorted(set(sub.department_ids + [department.id]))
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='activate', detail_json=_json({'version': latest + 1})))
    if Subscription.query.filter_by(school_id=proposal.school_id).first():
        collection = tasks.enqueue('collect', department.id, {'school_id': proposal.school_id, 'department_id': department.id},
                                   replace_finished=False, commit=False)
        if collection.state == 'pending' and collection.claim_count == 0:
            collection.phase = 'onboarding_collection'
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
    from backend.services.source_workflow import workflow
    return {'id': proposal.id, 'school_id': proposal.school_id, 'department_id': proposal.department_id,
            'state': proposal.state, 'origin': proposal.origin, 'candidate': json.loads(proposal.candidate_json),
            'validation': json.loads(proposal.validation_json), 'revision': proposal.revision,
            'updated_at': proposal.updated_at.isoformat(), 'workflow': workflow(proposal)}


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
        elif (key and scopes[key].get('reference_hash') and
              item.get('reference_url') == scopes[key].get('reference_url') and
              item.get('content_hash') != scopes[key]['reference_hash']):
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
    from backend.services.directory_work import sync_inventory
    sync_inventory(school_id, report)
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
    from backend.database.models import BackgroundTask
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.services.directory_work import run_material
    binding = get_model_binding('directory')
    if expected_version is not None and str(binding['version']) != str(expected_version):
        raise AIConfigError('模型配置版本已变更', 'configuration_changed')
    parent = BackgroundTask.query.filter_by(identity=f'discover:{school_id}').first()
    result = run_material(school_id, parent.generation if parent else 1, execution_id,
        'extraction', evidence_bundle, binding)
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
        from backend.services import tasks
        if tasks.current_execution():
            # Discovered columns are independent queue work, not blocked by the
            # rest of a long university crawl or another column's AI exploration.
            tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id},
                          capability='directory', replace_finished=False)
            continue
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
                                      inventory=inventory, fetcher=fetcher,
                                      scope_evidence=json.loads(proposal.evidence_json).get('scope_evidence'))
    validation = validate_proposal(proposal.id, independent_evidence=evidence)
    if not validation['passed']:
        skill = run_source_skill_for_proposal(proposal.id, inventory=inventory)
        if skill.get('changed'):
            validation = json.loads(proposal.validation_json)
    if validation['passed']:
        activate_proposal(proposal.id)
    return serialize_proposal(proposal)


def run_source_skill_for_proposal(proposal_id, *, inventory=None):
    from backend.services.source_exploration import run
    return run(proposal_id, inventory=inventory)


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
    """Distinguish supplier access limits from program faults without losing history."""
    from backend.scraper.acquisition import FetchFailure, FetchDeferred
    from backend.services.source_exploration import save_event
    try:
        return _process_source_review(payload)
    except FetchDeferred:
        raise
    except FetchFailure as exc:
        db.session.rollback()
        proposal = db.session.get(SourceProposal, payload['proposal_id'])
        if not proposal:
            return {'state': 'missing'}
        proposal.state = 'needs_review'
        failure = {'status': 'recognition_incomplete' if exc.outcome == 'needs_adapter' else 'site_unavailable', 'outcome': exc.outcome,
                   'error_code': exc.error_code, 'reason': str(exc)[:400]}
        proposal.validation_json = _json({'passed': False, 'errors': [str(exc)[:300]],
            'workflow': failure, 'validator_version': VERSION})
        proposal.validator_version = VERSION
        save_event(proposal.id, 'fetch_failed', failure)
        return serialize_proposal(proposal)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        db.session.rollback()
        proposal = db.session.get(SourceProposal, payload['proposal_id'])
        if proposal:
            validation = json.loads(proposal.validation_json)
            validation.update(passed=False, workflow={'status': 'program_error',
                'reason': str(exc)[:400], 'exception': type(exc).__name__})
            proposal.validation_json = _json(validation)
            save_event(proposal.id, 'program_failed', validation['workflow'])
        raise


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
    from backend.services.source_exploration import STEP_ACTION
    saved_material = json.loads(proposal.evidence_json)
    steps = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action=STEP_ACTION).all()
    pending_config = any((lambda step: step.get('config_applied') and not step.get('applied')
                         and step.get('revision') == proposal.revision)(json.loads(event.detail_json)) for event in steps)
    prior_validation = json.loads(proposal.validation_json)
    prior_plan = next((json.loads(event.detail_json) for event in reversed(steps)
        if (json.loads(event.detail_json).get('result') or {}).get('status') == 'succeeded'
        and ((json.loads(event.detail_json)['result'].get('output') or {}).get('proposals') or [{}])[0].get('decision') == 'propose'), None)
    if not pending_config and prior_plan and prior_validation.get('validator_version') not in (None, VERSION):
        suggestion = prior_plan['result']['output']['proposals'][0]
        history = SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='revalidate_after_upgrade').all()
        record = next((event for event in history if json.loads(event.detail_json).get('validator_version') == VERSION), None)
        if _candidate(suggestion['config']) == config and (not record or json.loads(record.detail_json).get('status') == 'pending'):
            from backend.services.source_exploration import save_event
            if not record:
                record = save_event(proposal.id, 'revalidate_after_upgrade', {'validator_version': VERSION,
                    'status': 'pending', 'previous_validation': prior_validation, 'execution_id': prior_plan.get('execution_id')})
            # Old versions could label a homepage snapshot as a newly selected
            # list during a browser handoff. Recapture this existing AI plan with
            # real URL provenance; do not repay the model or trust the bad sample.
            captured = capture_source_evidence(proposal.school_id, config, department_id=proposal.department_id,
                inventory=catalog, scope_evidence=suggestion.get('scope_evidence'),
                publisher_material=saved_material.get('publisher_material'))
            captured.bundle['exploration_pages'] = saved_material.get('exploration_pages', []) + captured.bundle.get('exploration_pages', [])
            if suggestion.get('publisher_evidence'):
                captured.bundle['publisher_evidence'] = suggestion['publisher_evidence']
            result = validate_proposal(proposal.id, independent_evidence=captured)
            detail = json.loads(record.detail_json); detail.update(status='complete', validation=result)
            record.detail_json = _json(detail); db.session.commit()
            if not result['passed']:
                run_source_skill_for_proposal(proposal.id, inventory=catalog)
            if json.loads(proposal.validation_json).get('passed'):
                activate_proposal(proposal.id)
            return serialize_proposal(proposal)
    if saved_material.get('list') and steps and (saved_material.get('config_hash') == _hash(config) or pending_config):
        run_source_skill_for_proposal(proposal.id, inventory=catalog)
        if json.loads(proposal.validation_json).get('passed'):
            activate_proposal(proposal.id)
        return serialize_proposal(proposal)
    cached = cached_page(proposal, catalog)
    if cached and page_problem(cached) in ('article_instead_of_column', 'search_instead_of_column') and resolve_non_column(proposal, cached):
        return serialize_proposal(proposal)
    if not config.get('list_selector'):
        url = config['list_url']
        db.session.commit()
        # An unconfigured entrance may be a directory or unfamiliar list. Let
        # AI see readable DOM even when the heuristic list detector has no rule.
        seed = _exploration_html(url)
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
    # With an API configured, let AI inspect the observed list before executing
    # a heuristic body selector. Otherwise one guessed CMS rule can spend minutes
    # rendering several articles before the model ever sees the first page.
    from backend.ai.configuration import get_model_binding, AIConfigError
    try:
        get_model_binding('directory')
        ai_available = True
    except AIConfigError:
        ai_available = False
    if ai_available:
        seed = seed or cached or _exploration_html(config['list_url'])
        school = db.session.get(School, proposal.school_id)
        bundle = {'schema': 1, 'school_id': school.id, 'school_name': school.name, 'root_url': school.url,
                  'config_hash': _hash(config), 'expected_config': source_config(
                      db.session.get(Department, proposal.department_id)) if proposal.department_id else {},
                  'list': _snapshot(seed, getattr(seed, 'final_url', config['list_url'])),
                  'articles': [], 'identity_paths': [], 'identity_snapshots': []}
        proposal.evidence_json, proposal.evidence_hash = _json(bundle), _hash(bundle)
        proposal.validation_json = _json({'passed': False, 'errors': [], 'validator_version': VERSION})
        db.session.commit()
        run_source_skill_for_proposal(proposal.id, inventory=catalog)
        if json.loads(proposal.validation_json).get('passed'):
            activate_proposal(proposal.id)
        return serialize_proposal(proposal)
    evidence = capture_source_evidence(proposal.school_id, config, department_id=proposal.department_id,
                                      seed_html=seed, inventory=catalog,
                                      scope_evidence=json.loads(proposal.evidence_json).get('scope_evidence'))
    validation = validate_proposal(proposal.id, independent_evidence=evidence)
    if not validation['passed']:
        skill = run_source_skill_for_proposal(proposal.id, inventory=catalog)
        if skill.get('changed'):
            validation = json.loads(proposal.validation_json)
    if validation['passed']:
        activate_proposal(proposal.id)
    return serialize_proposal(proposal)
