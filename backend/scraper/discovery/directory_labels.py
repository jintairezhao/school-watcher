"""Recover displayed directory labels only from explicit, matching page evidence."""
import re
from urllib.parse import urlsplit

from backend.services.source_inventory import resolve_page_link

FUDAN_VIEWS = (
    ('正文院系目录', '.col_news_list .part_xy > li.column-1 > .sub-con > .sub-list > li > a'),
    ('顶部院系导航', 'li.sub-item[class*="i5-1-"] > a.sub-link'),
    ('页脚院系目录', '#fddx_yxdw > ul > li > span > a'),
)
ELLIPSIS = re.compile(r'(?:\.{3,}|…+)\s*$')


def fudan_directory(page_url):
    page = urlsplit(page_url)
    return (page.hostname, page.path) == ('www.fudan.edu.cn', '/489/list.htm')


def visible_directory_label(anchor):
    from .structure import clean
    marker = anchor.find('span', recursive=False) if 'sub-link' in anchor.get('class', []) else None
    if marker is not None and marker.get_text(strip=True) == '>':
        return clean(' '.join(str(s) for s in anchor.strings if not any(p is marker for p in s.parents)))
    return clean(anchor.get_text(' ', strip=True))


def directory_label(anchor, soup, page_url):
    """A same-address copy may complete a prefix; it may not rename a full label."""
    from .structure import clean, locator
    visible = visible_directory_label(anchor)
    if not ELLIPSIS.search(visible):
        return visible, None
    prefix = ELLIPSIS.sub('', visible).rstrip()
    title = clean(anchor.get('title'))
    evidence = {'displayed': visible, 'locator': locator(anchor)}
    if len(prefix) < 3:
        return visible, dict(evidence, status='unresolved')
    if title.startswith(prefix) and not ELLIPSIS.search(title) and len(title) > len(prefix):
        return title, dict(evidence, status='expanded', method='own_title_attribute',
                           full_label=title, reference_locators=[locator(anchor)])
    target = resolve_page_link(page_url, anchor.get('href'))
    candidates = {}
    if target:
        for other in soup.select('a.sub-link, #fddx_yxdw a, #fddx_yxdw2 a'):
            if other is anchor or resolve_page_link(page_url, other.get('href')) != target:
                continue
            text = visible_directory_label(other)
            if ELLIPSIS.search(text):
                full = clean(other.get('title'))
                if full.startswith(ELLIPSIS.sub('', text).rstrip()):
                    text = full
            if text.startswith(prefix) and not ELLIPSIS.search(text) and len(text) > len(prefix):
                candidates.setdefault(text, []).append(locator(other))
    if len(candidates) == 1:
        full = next(iter(candidates))
        return full, dict(evidence, status='expanded', method='same_address_directory_copy',
                          full_label=full, reference_locators=candidates[full])
    return visible, dict(evidence, status='unresolved', candidates=sorted(candidates))


def fudan_directory_variants(soup, page_url):
    views = []
    for label, selector in FUDAN_VIEWS:
        anchors = soup.select(selector)
        if not anchors:
            continue
        entries = {}
        for anchor in anchors:
            name, _ = directory_label(anchor, soup, page_url)
            entries.setdefault(name, set()).add(resolve_page_link(page_url, anchor.get('href')))
        views.append((label, entries))
    if len(views) < 2:
        return []
    result = []
    for name in sorted(set().union(*(set(entries) for _, entries in views))):
        signatures = [frozenset(entries[name]) if name in entries else None for _, entries in views]
        if len(set(signatures)) > 1:
            result.append({'name': name, 'parent': '', 'versions': [
                {'name': label, 'present': name in entries, 'urls': sorted(entries.get(name, set()))}
                for label, entries in views]})
    return result
