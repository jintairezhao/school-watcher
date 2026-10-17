"""Partition observed DOM subtrees without losing entrances or changing positions."""
import re
from collections import Counter

from bs4 import BeautifulSoup, Comment, Tag
from backend.ai.skill_loader import canonical
from .structure import locator


def _compact_locations(soup):
    """Use exact DOM uniqueness and sibling positions for evidence-only CSS."""
    import soupsieve
    nodes = soup.find_all(True)
    ids = Counter(str(node.get('id')) for node in nodes if node.get('id'))
    classes = Counter((node.name, name) for node in nodes for name in node.get('class', []))
    cached = {}

    def locate(target):
        lineage, node = [], target
        while isinstance(node, Tag) and node is not soup and id(node) not in cached:
            lineage.append(node)
            node = node.parent
        for current in reversed(lineage):
            original = locator(current)
            candidates = [original]
            if current.get('id') and ids[str(current['id'])] == 1:
                candidates.append('#' + soupsieve.escape(str(current['id'])))
            for name in current.get('class', []):
                if classes[(current.name, name)] == 1:
                    candidates.append(current.name + '.' + soupsieve.escape(name))
            parent = current.parent
            if isinstance(parent, Tag):
                siblings = parent.find_all(current.name, recursive=False)
                step = current.name
                if len(siblings) > 1:
                    index = next(i for i, sibling in enumerate(siblings, 1) if sibling is current)
                    step += ':nth-of-type(' + str(index) + ')'
                prefix = cached.get(id(parent))
                candidates.append(prefix + ' > ' + step if prefix else step)
            chosen = min(candidates, key=lambda value: (len(value), value))
            cached[id(current)] = chosen
        return cached[id(target)] if id(target) in cached else locator(target)
    return locate


def repair_scope_selectors(html, observations):
    """Locate every failed region and its heading, or retain the full page.

    Selectors are executed on the original DOM. A missing or ambiguous region
    must not silently disappear merely to reduce the cost of a repair request.
    """
    soup = BeautifulSoup(html, 'lxml')
    roots = []
    for observation in observations:
        nodes = []
        for field in ('container_selector', 'heading_selector', 'list_selector'):
            if not observation.get(field):
                continue
            try:
                nodes.extend(soup.select(observation[field]))
            except (ValueError, TypeError):
                continue
        nodes = list({id(node): node for node in nodes}.values())
        if not nodes:
            return ()
        shared = {id(node) for node in [nodes[0], *nodes[0].parents]}
        for node in nodes[1:]:
            shared.intersection_update(id(parent) for parent in [node, *node.parents])
        root = next((node for node in [nodes[0], *nodes[0].parents] if id(node) in shared), None)
        if root is None or root.name in ('html', 'body', '[document]'):
            return ()
        # A list alone needs its immediate real surroundings to expose a
        # sibling heading. Never invent a heading from the failed rule's name.
        if not observation.get('heading_selector') and root.parent.name not in ('html', 'body', '[document]'):
            root = root.parent
        roots.append(root)
    unique = {id(node): node for node in roots}
    return tuple(locator(node) for node in unique.values()
                 if not any(id(parent) in unique for parent in node.parents))


def materials(school_id, url, html, *, max_bytes=None, preferred_selectors=(),
              compact_locations=None, scope_selectors=()):
    from backend.services.discovery_policy import get_policy
    policy = get_policy()
    max_bytes = max_bytes if max_bytes is not None else policy['evidence_max_bytes']
    soup = BeautifulSoup(html, 'lxml')
    if compact_locations is None:
        from backend.services.tasks import current_execution
        compact_locations = ((current_execution() or {}).get('checkpoint') or {}).get(
            'column_evidence_layout') in ('compact-locations-1', 'focused-repair-2')
    # Uniqueness belongs to the original DOM. Later clearing of chrome, SVG or
    # head contents must not make an originally duplicated ID/class look unique.
    short_location = _compact_locations(soup) if compact_locations else None
    title = soup.title.get_text(' ', strip=True)[:300] if soup.title else ''
    headings = [node.get_text(' ', strip=True)[:200] for node in soup.select('h1')[:3]]
    for node in soup.select('script,style,svg'):
        node.clear()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.startswith('on') or attr in ('style', 'srcset') or (attr == 'src' and str(node[attr]).startswith('data:')):
                del node.attrs[attr]
    from backend.scraper.article_urls import ARTICLE
    date = re.compile(r'(?<!\d)(?:19|20)\d{2}\s*[-./年]\s*\d{1,2}\s*[-./月]\s*\d{1,2}(?!\d)')
    navigation = re.compile(r'(?:^|[-_\s])(?:header|nav|menu)(?:[-_\s]|$)', re.I)
    banner = re.compile(r'(?:^|[-_\s])header(?:[-_\s]|$)', re.I)

    def publication_signal(node):
        if date.search(node.get_text(' ', strip=True)):
            return True
        return any(ARTICLE.search(a.get('href', '')) and len(a.get_text(' ', strip=True)) >= 16
                   for a in node.select('a[href]'))

    # Navigation was already observed by the separate navigation task. Do not
    # buy another column classification for pure site chrome. Clear rather
    # than detach, preserving every remaining node's original nth-of-type.
    local_navigation = []
    if soup.head:
        soup.head.clear()
    for node in soup.find_all(True):
        semantic = node.name in ('header', 'nav') or node.get('role') in ('banner', 'navigation')
        chrome = navigation.search(' '.join(node.get('class', [])) + ' ' + str(node.get('id', '')))
        if (semantic or chrome) and not publication_signal(node):
            # A breadcrumb or section menu beside the list is part of the
            # column evidence. A header/banner remains site chrome. Inspect
            # actual enclosing content rather than treating every "nav" class
            # as a global menu and erasing the only visible column name.
            lineage = [node, *node.parents]
            global_banner = any(isinstance(parent, Tag) and (parent.name in ('header', 'footer')
                or parent.get('role') == 'banner' or banner.search(
                    ' '.join(parent.get('class', [])) + ' ' + str(parent.get('id', '')))) for parent in lineage)
            region = None
            if not global_banner:
                for parent in node.parents:
                    if not isinstance(parent, Tag) or parent.name in ('html', 'body', '[document]'):
                        break
                    if publication_signal(parent):
                        region = parent
                        break
            if region is None:
                node.clear()
            else:
                local_navigation.append((node, region))
    preferred_roots = set()
    for selector in preferred_selectors:
        try:
            preferred_roots.update(locator(node) for node in soup.select(selector))
        except (TypeError, ValueError):
            continue

    envelope = {'school_id': school_id, 'candidates': [{'candidate_id': 'page', 'url': url}]}
    metadata_nodes = {}
    def location(node):
        original = locator(node)
        if compact_locations:
            metadata_nodes[original] = node
        return original

    def compact_proof(item):
        # Partition the same complete subtrees first. Compacting before the
        # split can change which large chrome subtrees enter the material and
        # paradoxically increase both calls and bytes. Only its representation
        # and bounded packing change here; every old HTML piece stays present.
        result = dict(item)
        for key in ('heading_locations', 'navigation_locations', 'region_context', 'ancestor_context'):
            if key not in item:
                continue
            result[key] = []
            for row in item[key]:
                compact = dict(row)
                for field in ('selector', 'containing_region_selector'):
                    node = metadata_nodes.get(row.get(field))
                    if node is not None:
                        compact[field] = short_location(node)
                result[key].append(compact)
        return result
    heading_selector = 'h1,h2,h3,h4,h5,h6,[role=tab],.title,.tit,.hd'
    region_limit = policy['navigation_batch_size']
    contexts = {}
    from backend.services.source_inventory import canonical_url, resolve_page_link
    navigation_locations, seen_navigation = [], set()
    for navigation_node, region in local_navigation:
        for anchor in navigation_node.select('a[href]'):
            try:
                target = resolve_page_link(url, anchor['href'])
            except ValueError:
                continue
            text = anchor.get_text(' ', strip=True)
            if id(anchor) in seen_navigation or not target or ARTICLE.search(target) or not 1 <= len(text) <= 200:
                continue
            seen_navigation.add(id(anchor))
            navigation_locations.append((id(region), {'selector': location(anchor), 'text': text,
                'url': target, 'containing_region_selector': location(region)}))

    def tab_list(node):
        return node.get('role') == 'tablist' or any(
            re.search(r'(?:^|[-_])tabs?(?:$|[-_])', name, re.I)
            for name in node.get('class', []))

    def in_article_list(node):
        from .publication_lists import article_heading
        return article_heading(node, url) or any(parent.name in ('ul', 'ol', 'table') and not tab_list(parent)
                   for parent in node.parents if isinstance(parent, Tag))

    # An article's h4/.title is a list item title, not a new publication
    # region. Keep its whole list with the actual column heading whenever the
    # byte limit permits it. Tab labels use the actual label node, excluding
    # their adjacent "more" link.
    heading_nodes = [node for node in soup.select(heading_selector) if not in_article_list(node)]
    # The current column can be named by an ordinary linked breadcrumb or
    # side navigation, with no h1/title class at all. This is observed page
    # identity, not an assertion that every nearby list belongs to the link.
    # Keep the real selector so native scope validation still decides that.
    current_links = set()
    for node in soup.select('a[href]'):
        if node.find_parent(['footer', 'header']):
            continue
        try:
            target = resolve_page_link(url, node['href'])
        except ValueError:
            continue
        text = node.get_text(' ', strip=True)
        if target and canonical_url(target) == canonical_url(url) and 1 <= len(text) <= 200:
            current_links.add(id(node))
            heading_nodes.append(node)
    for tabs in soup.find_all(['ul', 'ol']):
        if not tab_list(tabs):
            continue
        for item in tabs.find_all('li', recursive=False):
            labels = [node for node in item.select('span')
                      if not node.find_parent('a') and node.get_text(' ', strip=True)
                      and not node.find('span')]
            heading_nodes.extend(labels)
    heading_nodes = list({id(node): node for node in heading_nodes}.values())
    heading_rows = {}

    def heading_location(node):
        key = id(node)
        if key not in heading_rows:
            row = {'selector': location(node), 'heading': node.get_text(' ', strip=True)[:200]}
            if key in current_links:
                row['basis'] = 'observed_current_page_link'
            for parent in node.parents:
                if not isinstance(parent, Tag) or parent.name in ('[document]', 'html', 'body'):
                    break
                if publication_signal(parent):
                    # This is an observed common DOM ancestor, not an assertion
                    # that every publication in it belongs to this heading.
                    row['containing_region_selector'] = location(parent)
                    break
            heading_rows[key] = row
        return heading_rows[key]

    def material(items):
        return dict(envelope, evidence=items)

    def size(items):
        return len(canonical(material(items)).encode())

    def context(node):
        parent = node.parent
        key = id(parent)
        if key not in contexts:
            rows = []
            for ancestor in node.parents:
                if not isinstance(ancestor, Tag) or ancestor.name in ('[document]', 'html', 'body'):
                    break
                # Only real headings in the containing region, never another
                # sibling region's heading inferred as this region's owner.
                direct = [child for child in ancestor.children if isinstance(child, Tag)]
                for heading in direct:
                    if not (heading.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
                            or heading.get('role') == 'tab'
                            or set(heading.get('class', [])).intersection(('title', 'tit', 'hd'))):
                        continue
                    text = heading.get_text(' ', strip=True)
                    if text:
                        rows.append({'selector': location(heading), 'heading': text[:200]})
                if rows:
                    break
            contexts[key] = rows
        return contexts[key]

    def proof(node, *, html=None, **extra):
        root = '' if node is soup else locator(node)
        result = {'evidence_id': 'page', 'url': url, 'html': str(node) if html is None else html,
                  'page_title': title, 'page_headings': headings, 'original_root_selector': root}
        if html is None:
            contained = {id(child) for child in node.descendants if isinstance(child, Tag)} | {id(node)}
            result['heading_locations'] = [heading_location(heading) for heading in heading_nodes
                                           if id(heading) in contained]
            nearby = contained | {id(parent) for parent in node.parents}
            observed_navigation = [row for region, row in navigation_locations if region in nearby]
            if observed_navigation:
                result['navigation_locations'] = observed_navigation
        if root:
            result['region_context'] = context(node)
            result['ancestor_context'] = [{'selector': location(parent), 'tag': parent.name,
                'id': parent.get('id', ''), 'classes': parent.get('class', [])}
                for parent in list(node.parents)[:3] if isinstance(parent, Tag)
                and parent.name not in ('[document]', 'html', 'body')]
        result.update(extra)
        return result

    def regions(node):
        if not isinstance(node, Tag):
            return 0
        lists = ([node] if node.name in ('ul', 'ol', 'table') else []) + node.select('ul,ol,table')
        roots = {id(candidate) for candidate in lists if candidate.select('a[href]')}
        count = sum(not any(id(parent) in roots for parent in candidate.parents)
                    for candidate in lists if id(candidate) in roots)
        # Non-list layouts still retain their observed region headings. Never
        # count article headings nested in an already counted publication list.
        if not count:
            contained = {id(child) for child in node.descendants if isinstance(child, Tag)} | {id(node)}
            count = sum(id(heading) in contained for heading in heading_nodes)
        return count

    def text_parts(node, text):
        # A single huge text node must not hide a later, lone notice link. Text
        # excerpts retain their original parent but aren't pretend full DOMs.
        start = 0
        while start < len(text):
            low, high, chosen = 1, len(text) - start, None
            while low <= high:
                count = (low + high) // 2
                wrapper = soup.new_tag(node.name, attrs=dict(node.attrs))
                wrapper.append(text[start:start + count])
                item = proof(node, html=str(wrapper),
                             fragment_partial=True, fragment_kind='text_excerpt',
                             text_span=[start, start + count])
                if size([item]) <= max_bytes:
                    chosen = item
                    low = count + 1
                else:
                    high = count - 1
            if chosen is None:
                raise ValueError('Column evidence envelope exceeds the configured input limit')
            yield chosen, 0
            start = chosen['text_span'][1]

    def split(node):
        if not node.get_text(strip=True) and not node.select('a[href]') and node.name != 'a':
            return
        item = proof(node)
        count = regions(node)
        if size([item]) <= max_bytes and count <= region_limit:
            yield item, count
            return
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, Tag):
                yield from split(child)
            elif str(child).strip():
                yield from text_parts(node, str(child))

    # Complete child subtrees are separate evidence records, packed into a
    # bounded call. We never rewrap siblings in a partial parent, which would
    # silently shift nth-of-type and could select the wrong original column.
    def priority(piece):
        item, _ = piece
        root = item['original_root_selector']
        preferred = any(root == selected or root.startswith(selected + ' > ')
                        or selected.startswith(root + ' > ') for selected in preferred_roots)
        observed = BeautifulSoup(item['html'], 'lxml')
        return not preferred, not publication_signal(observed)

    # Scoping is explicit repair work. Ordinary discovery still visits every
    # retained subtree. Clearing chrome must not turn a requested scope into
    # an empty successful repair: fall back when it cannot be represented.
    roots = []
    for selector in scope_selectors:
        try:
            matches = soup.select(selector)
        except (ValueError, TypeError):
            matches = []
        if len(matches) != 1 or not (matches[0].get_text(strip=True) or matches[0].select('a[href]')):
            roots = []
            break
        roots.extend(matches)
    pieces = (piece for root in roots or [soup] for piece in split(root))
    result, batch, count = [], [], 0
    for index, (item, item_count) in enumerate(sorted(pieces, key=priority)):
        if compact_locations:
            item = compact_proof(item)
        item['evidence_id'] = 'page' if not item['original_root_selector'] else f'region-{index}'
        if size([item]) > max_bytes:
            # Reserve the longest eventual ID before text partitioning.
            item['evidence_id'] = 'page'
        if batch and (size(batch + [item]) > max_bytes or count + item_count > region_limit):
            result.append(material(batch))
            batch, count = [], 0
        batch.append(item)
        count += item_count
    if batch:
        result.append(material(batch))
    return result
