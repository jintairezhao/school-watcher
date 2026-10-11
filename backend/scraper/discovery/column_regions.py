"""Partition observed DOM subtrees without losing entrances or changing positions."""
import re

from bs4 import BeautifulSoup, Comment, Tag
from backend.ai.skill_loader import canonical
from .structure import locator


def materials(school_id, url, html, *, max_bytes=None, preferred_selectors=()):
    from backend.services.discovery_policy import get_policy
    policy = get_policy()
    max_bytes = max_bytes if max_bytes is not None else policy['evidence_max_bytes']
    soup = BeautifulSoup(html, 'lxml')
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

    def publication_signal(node):
        if date.search(node.get_text(' ', strip=True)):
            return True
        return any(ARTICLE.search(a.get('href', '')) and len(a.get_text(' ', strip=True)) >= 16
                   for a in node.select('a[href]'))

    # Navigation was already observed by the separate navigation task. Do not
    # buy another column classification for pure site chrome. Clear rather
    # than detach, preserving every remaining node's original nth-of-type.
    if soup.head:
        soup.head.clear()
    for node in soup.find_all(True):
        semantic = node.name in ('header', 'nav') or node.get('role') in ('banner', 'navigation')
        chrome = navigation.search(' '.join(node.get('class', [])) + ' ' + str(node.get('id', '')))
        if (semantic or chrome) and not publication_signal(node):
            node.clear()
    preferred_roots = set()
    for selector in preferred_selectors:
        try:
            preferred_roots.update(locator(node) for node in soup.select(selector))
        except (TypeError, ValueError):
            continue

    envelope = {'school_id': school_id, 'candidates': [{'candidate_id': 'page', 'url': url}]}
    region_selector = 'ul,ol,table,h2,h3,h4,h5,h6,[role=tab],.title,.tit,.hd'
    region_limit = policy['navigation_batch_size']
    contexts = {}

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
                        rows.append({'selector': locator(heading), 'heading': text[:200]})
                if rows:
                    break
            contexts[key] = rows
        return contexts[key]

    def proof(node, *, html=None, **extra):
        root = '' if node is soup else locator(node)
        result = {'evidence_id': 'page', 'url': url, 'html': str(node) if html is None else html,
                  'page_title': title, 'page_headings': headings, 'original_root_selector': root}
        if root:
            result['region_context'] = context(node)
            result['ancestor_context'] = [{'selector': locator(parent), 'tag': parent.name,
                'id': parent.get('id', ''), 'classes': parent.get('class', [])}
                for parent in list(node.parents)[:3] if isinstance(parent, Tag)
                and parent.name not in ('[document]', 'html', 'body')]
        result.update(extra)
        return result

    def regions(node):
        return len(node.select(region_selector)) if isinstance(node, Tag) else 0

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

    result, batch, count = [], [], 0
    for index, (item, item_count) in enumerate(sorted(split(soup), key=priority)):
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
