"""Execute corrections to column scope against observed, unchanged page markup."""

from copy import deepcopy
import re
from bs4 import Comment, Tag


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
                id(child) in selected_ids or child.name in ('script', 'style')):
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
    def residual_text(branch, excluded):
        clean = deepcopy(branch)
        pairs = list(zip([branch, *branch.find_all(True)], [clean, *clean.find_all(True)]))
        for original, copied in pairs:
            if copied.attrs is not None and (original is excluded or original.name in ('script', 'style')):
                if copied is clean:
                    return ''
                copied.decompose()
        return _normal(clean.get_text(' ', strip=True))

    name = _normal(heading.get_text(' ', strip=True))
    for anchor in common.select('a[href]'):
        if parent is anchor or parent in anchor.parents:
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
