"""Reviewed academic rosters whose headings or labels mislead generic navigation parsing."""
import json
import re
from urllib.parse import urlsplit

from backend.scraper.http_client import same_school_url
from backend.services.source_inventory import resolve_page_link, canonical_url


def academic_relationships(soup, page_url, root_url, owner_key):
    from .structure import clean, locator, node_key
    page = urlsplit(page_url)
    signature = (page.hostname, page.path)
    nodes, links, replaced, notes = [], [], set(), []
    if signature not in {('www.hubu.edu.cn', '/zzjg/xbxy.htm'),
                         ('www.muc.edu.cn', '/zzjg/jxhkydw1.htm'),
                         ('www.sjtu.edu.cn', '/yxsz/'), ('www.sjtu.edu.cn', '/yxsz/index.html'),
                         ('www.sjtu.edu.cn', '/jgsz/'), ('www.sjtu.edu.cn', '/jgsz/index.html')}:
        return nodes, links, replaced, notes

    def add(element, name, kind, parent, relation, anchor=None):
        address = anchor.get('href', '') if anchor else ''
        target = resolve_page_link(page_url, address)
        loc = locator(element)
        key = node_key(kind, name, (target or page_url) + '#directory-position:' + loc)
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': target,
                      'parent': parent, 'locator': loc, 'relation': relation})
        replaced.add(loc)
        if anchor is not None:
            decision = ('missing_link' if not target else 'follow' if same_school_url(target, root_url)
                        else 'official_external_link')
            links.append({'label': name, 'raw_label': clean(anchor.get_text(' ', strip=True)),
                          'url': target, 'kind': 'unit', 'path': [], 'locator': loc, 'decision': decision})
            # Preserve what a browser actually opens. Do not silently repair a
            # bare hostname or invent a unit website from a directory self-link.
            problem = ('self_link' if target == canonical_url(page_url) else
                       'hostname_without_scheme' if re.fullmatch(r'[\w.-]+\.edu\.cn/?', address) else '')
            if problem:
                notes.append('official_directory_link_requires_review:' + json.dumps(
                    {'name': name, 'raw_href': address, 'resolved_url': target,
                     'locator': loc, 'reason': problem}, ensure_ascii=False))
        return key

    if signature[0] == 'www.sjtu.edu.cn':
        # The headerpage class wraps the entire document; it is not a header.
        # These named lists are the actual roster, including no-link institutes.
        for listing in soup.select('.org-content > ul.org-list'):
            heading = listing.find_previous_sibling()
            if heading is None or 'org-title' not in heading.get('class', []):
                continue
            group = add(heading, clean(heading.get_text(' ', strip=True)), 'group', owner_key,
                        'academic_group' if page.path.startswith('/yxsz/') else 'directory_group')
            for item in listing.find_all('li', recursive=False):
                anchors = item.find_all('a', recursive=False)
                elements = anchors or [item]
                for element in elements:
                    name = clean(element.get_text(' ', strip=True))
                    if not name:
                        continue
                    anchor = element if element.name == 'a' else None
                    address = anchor.get('href', '').strip() if anchor is not None else ''
                    valid = bool(address and address != '#' and not address.lower().startswith('javascript:'))
                    target = resolve_page_link(page_url, address) if valid else ''
                    self_link = bool(target and urlsplit(target).hostname == page.hostname and
                                     urlsplit(target).path.removesuffix('index.html').rstrip('/') ==
                                     page.path.removesuffix('index.html').rstrip('/'))
                    if self_link:
                        notes.append('official_directory_link_requires_review:' + json.dumps(
                            {'name': name, 'raw_href': address, 'resolved_url': target,
                             'locator': locator(element), 'reason': 'self_link'}, ensure_ascii=False))
                    add(element, name, 'unit', group,
                        'directory_label_pending' if self_link else
                        'directory_entry' if valid else 'directory_entry_no_link', anchor if valid else None)
                    if self_link:
                        links[-1]['decision'] = 'self_reference'
    elif signature[0] == 'www.hubu.edu.cn':
        for section in soup.select('.system > .sy-item'):
            headings = section.find_all('h4', recursive=False)
            listings = section.find_all('ul', recursive=False)
            if len(headings) != 1 or len(listings) != 1:
                continue
            name = clean(headings[0].get_text(' ', strip=True))
            if name not in ('学部设置', '学院设置', '跨学科学院'):
                continue
            group = add(headings[0], name, 'group', owner_key, 'directory_group')
            for anchor in listings[0].select('li > a'):
                # These are sibling entrances even when two share a list item.
                label = re.sub(r'\s+([（(])', r'\1', clean(anchor.get_text(' ', strip=True))).rstrip('、')
                if label:
                    add(anchor, label, 'unit', group, 'directory_entry', anchor)
    else:
        for anchor in soup.select('.n_znbm > ul.list_box9 > li > a'):
            name = clean(anchor.get_text(' ', strip=True))
            if not name:
                continue
            # Flat sibling cards do not prove that the following colleges belong
            # to the preceding 学部. The non-independent qualifier remains visible.
            relation = 'non_entity_directory_entry' if name.startswith('（非独立科研平台）') else 'directory_entry'
            add(anchor, name, 'unit', owner_key, relation, anchor)
    return nodes, links, replaced, notes
