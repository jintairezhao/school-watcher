"""Execute corrections to column scope against observed, unchanged page markup."""

from copy import deepcopy
import re
from bs4 import Comment, Tag


def observe_joint_selection(config, reference, html):
    """Describe a complete DOM partition; headings confer no ownership or receipt."""
    from backend.services import source_governance as g
    from backend.services.source_inventory import canonical_url
    from backend.scraper.discovery.publication_lists import publication_lists
    from soupsieve.util import SelectorSyntaxError
    url = getattr(html, 'final_url', '') or reference.get('url', '')
    if canonical_url(url) != canonical_url(config['list_url']):
        return None
    try:
        soup, items, records, errors = g._extract(config, reference, html=html)
        if len(items) < 2:
            return None
        selected = {id(item) for item in items}
        candidates = {}
        for feed in publication_lists(html, url):
            nodes = soup.select(feed['list_selector'])
            members = frozenset(id(node) for node in nodes) & selected
            if not members:
                continue
            candidates.setdefault(members, dict(name=feed.get('name') or '',
                name_status='observed' if feed.get('name') and not feed.get('heading_ambiguous') else 'unknown',
                list_selector=feed['list_selector'], container=feed.get('container_locator') or '',
                heading=feed.get('heading_locator') or '', selected_items=len(members),
                region_items=len(nodes), complete_region=members == {id(node) for node in nodes},
                column_url=feed.get('column_url') or '',
                column_link=feed.get('column_link_locator') or '',
                module_preview=bool(feed.get('module_preview')),
                **({'tab_binding_evidence': feed['tab_binding_evidence']}
                   if feed.get('tab_binding_evidence') else {})))
        covered, regions = set(), []
        for members, region in sorted(candidates.items(), key=lambda item: (len(item[0]), item[1]['list_selector'])):
            if members <= covered:
                continue
            if members & covered:
                return None  # Overlapping alternatives do not establish a partition.
            covered.update(members)
            regions.append(region)
        if covered != selected:
            return None
        preview = (len(regions)==1 and regions[0]['module_preview'] and regions[0]['complete_region']
                   and regions[0]['column_url'] and canonical_url(regions[0]['column_url'])!=canonical_url(url))
        if len(regions)<2 and not preview:
            return None
        return dict(kind='configured_module_preview' if preview else 'configured_multiple_regions', config_hash=g._hash(config),
            selected_items=len(items), extracted_records=len(records), extraction_errors=sorted(set(errors)),
            regions=regions, material={key: reference.get(key) for key in
                ('url', 'hash', 'captured_at', 'observed_at', 'outcome', 'script_materials')
                if key in reference},
            identity_basis='existing_source_configuration', publisher_assertion=False)
    except (KeyError, ValueError, TypeError, SelectorSyntaxError):
        return None


def configured_joint_scope(proposal, bundle=None):
    """Re-execute the current source on retained original bytes, never a model claim."""
    from backend.database.db import db
    from backend.database.models import Department
    from backend.services import source_governance as g
    from backend.services.source_inventory import canonical_url
    from datetime import datetime, timezone
    import json
    source = db.session.get(Department, proposal.department_id) if proposal.department_id else None
    if source is None or source.school_id != proposal.school_id:
        return None
    previous = g.source_config(source)
    if g._hash(previous) != proposal.expected_config_hash:
        return None
    bundle = json.loads(proposal.evidence_json) if bundle is None else bundle
    if g._hash(bundle) != proposal.evidence_hash:
        return None
    refs = [bundle.get('list'), bundle.get('independent'), *bundle.get('identity_snapshots', []),
            *bundle.get('exploration_pages', []), *(bundle.get('publisher_material') or {}).values()]
    # A new candidate can use another page. The previously recorded scope of
    # this same source still applies; rebasing the candidate cannot erase it.
    from backend.database.source_governance_models import SourceProposal, SourceReviewEvent
    recorded = SourceReviewEvent.query.join(SourceProposal,
        SourceProposal.id == SourceReviewEvent.proposal_id).filter(
            SourceProposal.school_id == proposal.school_id,
            SourceProposal.department_id == source.id,
            SourceReviewEvent.action == 'configured_scope_preserved').order_by(SourceReviewEvent.id.desc())
    known = []
    for event in recorded:
        scope = json.loads(event.detail_json).get('configured_scope') or {}
        if scope.get('source_id') == source.id and scope.get('config_hash') == g._hash(previous):
            known.append(scope)
            refs.append(scope.get('material'))
    def timestamp(ref, field):
        try:
            value = datetime.fromisoformat(ref.get(field) or '')
            # Server snapshots used UTC-naive captured_at before observed_at
            # was supplied by the transport; retain that existing convention.
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    # The same HTML can be paired with independently observed external scripts.
    # Replaying an older draft must not hide a newer complete observation. A
    # newer script-less read still wins and stays unknown, because identical
    # page bytes say nothing about whether its handler has since changed.
    refs = [ref for ref in refs if isinstance(ref, dict)]
    # captured_at is local retention time, including copies of historical HTML.
    # It cannot outrank a known website observation merely by being stored later.
    refs.sort(key=lambda ref: (timestamp(ref, 'observed_at'), timestamp(ref, 'captured_at')), reverse=True)
    seen = set()
    for ref in refs:
        context = g._hash([ref.get('hash'), ref.get('script_materials') or []])
        if (not ref.get('hash') or context in seen
                or canonical_url(ref.get('url', '')) != canonical_url(previous['list_url'])
                or ref.get('outcome', 'usable') not in ('usable', 'needs_adapter')):
            continue
        seen.add(context)
        try:
            observation = observe_joint_selection(previous, ref, g.read_snapshot(ref))
        except (ValueError, OSError):
            continue
        if observation:
            return dict(observation, source_id=source.id)
    if known:
        # Evicted material is a gap, not permission to narrow a known union.
        return dict(known[0], material_available=False, current_reexecution=False)
    return None


def configured_scope_reason(scope):
    if scope.get('kind')=='configured_module_preview':
        return '现有信源采集主页预览，已保留原配置和订阅范围。'
    return '现有信源覆盖多个列表，已保留原配置和订阅范围。'


def hold_configured_joint_scope(proposal, *, commit=True):
    """Keep a legacy union intact instead of paying to turn it into one child."""
    import json
    from backend.database.db import db
    from backend.database.source_governance_models import SourceReviewEvent
    from backend.services import source_governance as g
    observation = configured_joint_scope(proposal)
    if not observation:
        return False
    from backend.services import tasks
    tasks.assert_owned()
    detail = {'configured_scope': observation,
              'reason': configured_scope_reason(observation)}
    previous = SourceReviewEvent.query.filter_by(proposal_id=proposal.id,
        action='configured_scope_preserved').order_by(SourceReviewEvent.id.desc()).first()
    if previous is None or json.loads(previous.detail_json) != detail:
        db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='configured_scope_preserved',
                                        detail_json=g._json(detail)))
    result = json.loads(proposal.validation_json)
    result.update(passed=False, errors=sorted(set(result.get('errors', [])) |
        {'configured_joint_scope_requires_review'}), configured_scope=observation,
        validator_version=g.VERSION,
        workflow={'status': 'configured_scope_preserved', 'ai_status': 'not_needed',
            'reason': detail['reason']})
    tasks.assert_owned()
    proposal.validation_json = g._json(result)
    proposal.state, proposal.validated_hash = 'needs_review', None
    proposal.validator_version = g.VERSION
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return True


def publish_configured_scope(proposal_id):
    """Publish captured scope material and its review in one existing versioned transaction.

    This does not validate or activate a column, change its rules, or fetch pages.
    A failed publication cannot leave a successful scope review behind.
    """
    import json
    from pathlib import Path
    from flask import current_app
    from sqlalchemy import select
    from backend.database.db import db
    from backend.database.models import School,Department
    from backend.database.source_governance_models import SourceProposal
    from backend.services.source_inventory import site_key
    from backend.services.runtime_catalog import RuntimeCatalog,catalog_publication_lock
    from backend.services.source_material_publication import proposal_structure_writer
    root=Path(current_app.config['SOURCE_CATALOG_PATH']).resolve()
    try:
        with catalog_publication_lock(root):
            proposal=db.session.execute(select(SourceProposal).where(SourceProposal.id==proposal_id)
                .with_for_update()).scalar_one_or_none()
            if proposal is None or proposal.state=='activated':
                raise ValueError('仅可登记尚未接入的既有信源范围')
            db.session.execute(select(Department).where(Department.id==proposal.department_id)
                .with_for_update()).scalar_one_or_none()
            if not configured_joint_scope(proposal):
                raise ValueError('既有信源范围材料或当前配置不匹配')
            school=db.session.get(School,proposal.school_id)
            if school is None:
                raise ValueError('范围所属学校不存在')
            catalog=RuntimeCatalog(root)
            reader=catalog.pin(site_key(school.url))or RuntimeCatalog(root,versioned=False)
            bundle=json.loads(proposal.evidence_json)
            from backend.services.source_inventory import canonical_url
            if (bundle.get('school_name')!=school.name or
                    canonical_url(bundle.get('root_url')or'')!=canonical_url(school.url)):
                raise ValueError('范围材料与当前学校身份不匹配')
            # A scope-only capture can lack identity snapshots. Include its
            # hash-checked list as material, retaining role=list and no owner claim.
            references=list(bundle.get('identity_snapshots')or[])
            if not any(r.get('url')==bundle['list']['url']for r in references):
                references.append(dict(bundle['list']))
            material_bundle=dict(bundle,identity_snapshots=references)
            with proposal_structure_writer(school,material_bundle,
                    json.loads(proposal.candidate_json),reader):
                if not hold_configured_joint_scope(proposal,commit=False):
                    raise ValueError('范围登记前材料已变化')
        return True
    except BaseException:
        db.session.rollback()
        raise


def _heading_breadcrumbs(heading, name, page_url, article_urls, *, allow_plain_label=False):
    """Recognize only local, terminal self-link breadcrumbs beside a heading."""
    from urllib.parse import urlsplit
    from backend.services.source_inventory import canonical_url, resolve_page_link
    if heading.name not in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and not (
            allow_plain_label and heading.name in ('p', 'div', 'span') and not heading.find(True)):
        return set()
    result = set()
    for node in heading.parent.find_all(True):
        if node is heading or heading in node.descendants:
            continue
        marker = ' '.join(node.get('class', [])) + ' ' + node.get('aria-label', '')
        if not (re.search(r'breadcrumb|crumb|position|(?:^|[\s_-])cur(?:$|[\s_-])', marker, re.I)
                or node.get_text(' ', strip=True).startswith('当前位置')):
            continue
        anchors = node.select('a[href]')
        if not 2 <= len(anchors) <= 8:
            continue
        try:
            targets = [resolve_page_link(page_url, anchor['href']) for anchor in anchors]
        except ValueError:
            continue
        if (targets[-1] != canonical_url(page_url)
                or anchors[-1].get_text(' ', strip=True) != name
                or any(not target or target in article_urls
                       or urlsplit(target).netloc != urlsplit(page_url).netloc for target in targets)
                or any(not 1 <= len(anchor.get_text(' ', strip=True)) <= 40 for anchor in anchors)):
            continue
        result.add(id(node))
    return result


def _scope_pagers(region):
    """Account for a control-only widget, without proving pagination works."""
    label = re.compile(r'^(?:\d+|首页|尾页|第一页|最后一页|末页|最末页|上一页|下一页|上页|下页|'
                       r'跳转|first|last|previous|prev|next|[<>‹›«»…]+)$', re.I)
    directions = re.compile(r'首页|尾页|第一页|最后一页|最末页|末页|上一页|下一页|上页|下页|'
                            r'\b(?:first|last|previous|prev|next)\b', re.I)
    result = set()
    for node in region.find_all(True):
        # Navigation controls may include a page-number input and a scripted
        # jump. Only their DOM role is recognized here; no script is executed,
        # next page inferred, or exhausted-column claim made.
        text = node.get_text(' ', strip=True)
        tokens = {m.group().lower() for m in directions.finditer(text)}
        remainder = directions.sub('', text)
        remainder = re.sub(r'到第|跳转|共|页|条|\b(?:page|of)\b', '', remainder, flags=re.I)
        inputs = node.select('input')
        if (node.name in ('div', 'span') and len(tokens) >= 2 and re.search(r'\d', text)
                and not re.sub(r'[\s\d:：/|\[\]()（）,，.·<>«»‹›…-]+', '', remainder)
                and not node.select('img,iframe,object,button,select,textarea,h1,h2,h3,h4,h5,h6,ul,ol,table')
                and all((control.get('type') or 'text').lower() in ('text', 'number') for control in inputs)
                and all(label.fullmatch(anchor.get_text(' ', strip=True)) for anchor in node.select('a[href]'))
                and all(child.name in ('div', 'span', 'b', 'em', 'strong', 'a', 'input', 'script', 'link')
                        for child in node.find_all(True))):
            result.add(id(node))
            continue
        marker = ' '.join(node.get('class', [])) + ' ' + node.get('id', '')
        if not re.search(r'(?:^|[\s_-])(?:pages?|paging|pagination|pager|pagebar)(?:$|[\s_-])', marker, re.I):
            continue
        if node.select('a[href],button,input,select,textarea,img,script'):
            continue
        leaves = [child for child in node.find_all(True) if not child.find(True)]
        texts = [child.get_text(' ', strip=True) for child in leaves]
        if (not leaves or any(child.name not in ('span', 'b', 'em', 'strong') for child in leaves)
                or not all(label.fullmatch(text) for text in texts)
                or ''.join(node.stripped_strings) != ''.join(texts)):
            continue
        result.add(id(node))
    return result


def recover_observed_scope(config, reference, proposed_scope, *, html):
    """Correct only a scope typo for one unambiguous, fully selected list.

    The name, list, titles, addresses and dates remain the paid candidate's
    claims. New CSS comes from original DOM observations, then runs through the
    same scope validator. Ambiguous or incomplete lists go back to the agent.
    This does not install a source or substitute for body/pagination validation.
    """
    from backend.scraper.discovery.column_regions import materials
    from backend.scraper.discovery.publication_lists import article_heading
    from backend.scraper.discovery.structure import locator
    from backend.services.source_governance import (
        _column_matches, _column_scope, _extract, _normal, unverified_column_name)
    from backend.services.source_inventory import canonical_url, resolve_page_link

    records, errors, _ = _column_scope(config, reference, proposed_scope, html=html)
    if errors != ['column_identity_or_scope_unconfirmed'] or not records or unverified_column_name(config['name']):
        return None
    soup, items, _, extraction_errors = _extract(config, reference, html=html)
    if extraction_errors or not items:
        return None
    parent = items[0].parent
    if parent is None or any(item.parent is not parent for item in items):
        return None
    selected_ids = {id(item) for item in items}
    list_pagers = _scope_pagers(parent)
    # A container containing several article targets is not a single record,
    # even if select_one happened to return a plausible first title.
    for item, record in zip(items, records):
        targets = set()
        for anchor in ([item] if item.name == 'a' and item.get('href') else []) + item.select('a[href]'):
            if not (anchor.get_text(' ', strip=True) or anchor.get('title')):
                continue
            try:
                target = resolve_page_link(reference['url'], anchor['href'])
            except ValueError:
                return None
            if target:
                targets.add(target)
        if targets != {record['url']}:
            return None
    # A selector for the first few cards cannot establish the complete region.
    pager_label = re.compile(r'^(?:\d+|首页|尾页|第一页|最后一页|末页|最末页|上一页|下一页|'
                             r'first|last|previous|prev|next|[<>‹›«»…]+)$', re.I)
    for child in parent.children:
        if isinstance(child, Comment) or isinstance(child, Tag) and (
                id(child) in selected_ids | list_pagers or child.name in ('script', 'style')):
            continue
        text = child.get_text(' ', strip=True) if isinstance(child, Tag) else str(child).strip()
        if not text and (not isinstance(child, Tag) or not child.select('a[href],img[alt]')):
            continue
        controls = child.select('a[href]') if isinstance(child, Tag) else []
        markers = (' '.join(child.get('class', [])) + ' ' + child.get('id', '')) if isinstance(child, Tag) else ''
        genuine_pager = (re.search(r'pagination|pager|pagebar', markers, re.I) and controls
                         and all(pager_label.fullmatch(anchor.get_text(' ', strip=True)) for anchor in controls))
        if not genuine_pager:
            return None

    rows = {}
    try:
        observed_parts = materials(0, reference['url'], html)
    except ValueError:
        return None  # The agent retains the full page and the original failure.
    for part in observed_parts:
        for proof in part['evidence']:
            for row in proof.get('heading_locations', []):
                if _column_matches(config['name'], row['heading'], config.get('group_name', '')):
                    rows[row['selector']] = row
    # A declared plain label can be checked even when the optional fragment
    # detector does not classify its tag as a heading. Require an actual local
    # terminal self-link breadcrumb and uniqueness across all such labels;
    # the model's selector alone cannot decide between independent sections.
    from soupsieve.util import SelectorSyntaxError
    try:
        declared = soup.select((proposed_scope or {}).get('heading_selector', ''))
    except (ValueError, TypeError, SelectorSyntaxError):
        declared = []
    plain = []
    article_urls = {record['url'] for record in records}
    for node in soup.find_all(('p', 'div', 'span')) if len(declared) == 1 else []:
        if (node.find(True) or node.get('onclick') or node.get('role') in ('button', 'tab', 'link')
                or node.find_parent(('a', 'button', 'header', 'footer', 'nav', 'form'))
                or any(id(ancestor) in selected_ids for ancestor in [node, *node.parents])
                or not _column_matches(config['name'], node.get_text(' ', strip=True), config.get('group_name', ''))):
            continue
        if _heading_breadcrumbs(node, _normal(node.get_text(' ', strip=True)), config['list_url'],
                                article_urls, allow_plain_label=True):
            plain.append(node)
    if len(plain) == 1 and plain[0] is declared[0]:
        node = plain[0]
        rows[locator(node)] = {'selector': locator(node), 'heading': node.get_text(' ', strip=True),
                              'basis': 'executed_plain_label_with_breadcrumb'}
    # A unique actual section heading and repeated current-page navigation are
    # different observations. Repeated sidebars/breadcrumbs do not invalidate
    # that heading; multiple genuine headings still remain ambiguous.
    if len(rows) > 1:
        semantic = {selector: row for selector, row in rows.items()
                    if len(nodes := soup.select(selector)) == 1
                    and (nodes[0].name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
                         or row.get('basis') == 'executed_plain_label_with_breadcrumb')
                    and not any(id(parent) in selected_ids for parent in [nodes[0], *nodes[0].parents])}
        if len(semantic) == 1 and all(selector in semantic or row.get('basis') == 'observed_current_page_link'
                                      for selector, row in rows.items()):
            rows = semantic
    # Same-named independent modules need further evidence. Do not choose one
    # by DOM order, URL or the model's preferred name.
    if len(rows) != 1:
        return None
    row = next(iter(rows.values()))
    headings = soup.select(row['selector'])
    if len(headings) != 1:
        return None
    heading = headings[0]
    inside_item = lambda node: any(id(ancestor) in selected_ids for ancestor in [node, *node.parents])
    if inside_item(heading) or article_heading(heading, reference['url']):
        return None
    common = None
    for ancestor in heading.parents:
        if ancestor.name in ('html', 'body', '[document]'):
            break
        if all(ancestor in item.parents for item in items):
            common = ancestor
            break
    if common is None:
        return None
    # Everything between the list and its proposed heading must be accounted
    # for. A plain div/a label is just as capable of marking another column as
    # an h2. Do not infer missing boundaries from a CMS URL pattern.
    name = _normal(heading.get_text(' ', strip=True))
    breadcrumbs = _heading_breadcrumbs(heading, name, config['list_url'], article_urls,
        allow_plain_label=row.get('basis') == 'executed_plain_label_with_breadcrumb')
    scope_pagers = _scope_pagers(common)

    def residual_text(branch, excluded):
        clean = deepcopy(branch)
        pairs = list(zip([branch, *branch.find_all(True)], [clean, *clean.find_all(True)]))
        for original, copied in pairs:
            if copied.attrs is not None and (original is excluded or id(original) in breadcrumbs | scope_pagers
                                              or original.name in ('script', 'style')):
                if copied is clean:
                    return ''
                copied.decompose()
        return _normal(clean.get_text(' ', strip=True))

    for anchor in common.select('a[href]'):
        if parent is anchor or parent in anchor.parents:
            continue
        if any(id(node) in breadcrumbs for node in [anchor, *anchor.parents]):
            continue
        try:
            target = resolve_page_link(reference['url'], anchor['href'])
        except ValueError:
            return None
        if (_normal(anchor.get_text(' ', strip=True)) != name
                or target != canonical_url(config['list_url']) and anchor['href'] != '#'):
            return None
    for branch in common.children:
        if isinstance(branch, Comment):
            continue
        remaining = residual_text(branch, parent) if isinstance(branch, Tag) else _normal(str(branch))
        # A repeated literal column label is harmless; any other visible
        # content needs explicit evidence instead of automatic scope expansion.
        if remaining and (not name or remaining.replace(name, '')):
            return None
    # The nearest region may not silently absorb another publishing area.
    for node in common.select('h1,h2,h3,h4,h5,h6,[role=tab],.title,.tit,.hd'):
        if node is heading or heading in node.descendants or inside_item(node):
            continue
        if node.get_text(' ', strip=True):
            return None
    scope = {'container_selector': locator(common), 'heading_selector': locator(heading)}
    verified, errors, region = _column_scope(config, reference, scope, html=html)
    if errors or verified != records:
        return None
    return {'basis': 'executed_observed_heading', 'proposed_scope': dict(proposed_scope or {}),
            'scope': scope, 'region': region, 'record_count': len(records)}
