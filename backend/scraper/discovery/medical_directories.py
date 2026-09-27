"""Reviewed CMM directory layouts: explicit groups, text lists and separate views."""
import json
import re
from urllib.parse import urlsplit

from bs4 import Tag
from backend.services.source_inventory import resolve_page_link
from backend.scraper.http_client import same_school_url


def medical_directory(url):
    page = urlsplit(url)
    return page.hostname == 'www.cmm.zju.edu.cn' and page.path in ('/55143/list.htm', '/55144/list.htm')


def separated_labels(text):
    """Official wide/nonbreaking spaces delimit names, except inside parentheses."""
    labels, parts, depth, i = [], [], 0, 0
    while i < len(text):
        char = text[i]
        if char in '（(':
            depth += 1
        elif char in '）)':
            depth = max(0, depth - 1)
        if not depth and (char == '\u3000' or char == '\xa0'):
            if ''.join(parts).strip():
                labels.append(''.join(parts).strip())
            parts = []
        else:
            parts.append(char)
        i += 1
    if ''.join(parts).strip():
        labels.append(''.join(parts).strip())
    return labels


def text_entries(element, page_url):
    """Preserve unlinked labels between anchors, joining inline name fragments."""
    from .structure import clean, locator
    entries, fragments = [], []

    def flush():
        for text in separated_labels(''.join(fragments)):
            name = clean(text)
            if name:
                entries.append((name, '', locator(element) + f'::text({len(entries)})', text))
        fragments.clear()

    def visit(tag):
        if not isinstance(tag, Tag):
            fragments.append(str(tag))
        elif tag.name in ('script', 'style'):
            return
        elif tag.name == 'a':
            flush()
            name = clean(tag.get_text(' ', strip=True))
            if name:
                entries.append((name, resolve_page_link(page_url, tag.get('href')), locator(tag), name))
        else:
            block = tag.name in ('p', 'li', 'br')
            if block:
                flush()
            for child in tag.children:
                visit(child)
            if block:
                flush()
    visit(element)
    flush()
    return entries


def described_children(original):
    """Only explicit inclusion/subunit wording or a parenthetical list of offices.

    A shared-office statement is never a parent-child relation. A bare list in
    parentheses remains a directory association, not administrative evidence.
    """
    from .structure import clean, UNIT
    match = re.fullmatch(r'([^（(]+)[（(](.*)[）)]', original.strip())
    if not match:
        return None
    name, detail = clean(match[1]), match[2]
    if '合署' in detail:
        return None
    if detail.startswith('含'):
        labels, relation = re.split('[、，]', detail[1:]), 'nested_directory_entry'
    elif '下设：' in detail:
        labels, relation = re.split('[、，]', detail.split('下设：', 1)[1]), 'sub_unit'
    else:
        labels, relation = separated_labels(detail), 'directory_companion_entry'
    labels = [clean(label) for label in labels if clean(label)]
    if len(labels) < 2 or not UNIT.search(name) or not all(UNIT.search(label) for label in labels):
        return None
    return name, labels, relation


def medical_relationships(soup, page_url, root_url, owner_key):
    from .structure import clean, locator, node_key
    nodes, links, replaced, notes = [], [], set(), []
    if not medical_directory(page_url):
        return nodes, links, replaced, notes

    def add(name, target, loc, parent, kind, relation, link_kind=None):
        key = node_key(kind, name, page_url + '#position:' + loc)
        nodes.append({'key': key, 'name': name, 'url': target, 'kind': kind,
                      'parent': parent, 'locator': loc, 'relation': relation})
        links.append({'label': name, 'url': target, 'kind': link_kind or ('unit' if kind == 'unit' else 'directory'),
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not target else
                      'follow' if same_school_url(target, root_url) else 'official_external_link'})
        return key

    def replace(block):
        replaced.update(locator(tag) for tag in [block, *block.find_all(True)])

    if urlsplit(page_url).path == '/55144/list.htm':
        # The page's inline script copies sublist_title into the items h2, then
        # removes sublist_title (including its anchors). Treat it as a grouping
        # title, not a second unit or a website entry for the whole group.
        for row in soup.select('.departments_setup .wp_subcolumn_list > li.wp_sublist'):
            headings = row.select(':scope > h3.sublist_title > a[childcolumnid]')
            if len(headings) != 1:
                continue
            heading = headings[0]
            name = clean(heading.get_text(' ', strip=True))
            if not name:
                continue
            group = add(name, '', locator(heading), owner_key, 'group', 'directory_group')
            notes.append('directory_heading_template:' + json.dumps(
                {'name': name, 'locator': locator(heading), 'template_url': resolve_page_link(page_url, heading.get('href')),
                 'rendered_role': 'section_heading'}, ensure_ascii=False))
            for anchor in row.select(':scope > .items > .college > .text > .departments > a'):
                label = clean(anchor.get_text(' ', strip=True))
                if label:
                    add(label, resolve_page_link(page_url, anchor.get('href')), locator(anchor), group,
                        'unit', 'nested_directory_entry')
            replace(row)
        return nodes, links, replaced, notes

    def add_entries(element, parent, header):
        for name, target, loc, original in text_entries(element, page_url):
            description = described_children(original) if not target else None
            label = description[0] if description else name
            primary = add(label, target, loc, parent, 'unit', 'navigation_entry' if header else 'directory_entry')
            if description:
                notes.append('directory_relationship_wording:' + json.dumps(
                    {'name': label, 'original': original, 'locator': loc, 'relation': description[2]}, ensure_ascii=False))
                for i, child in enumerate(description[1]):
                    child_loc = loc.split('::text(')[0] + f'::text({loc};child:{i})'
                    add(child, '', child_loc, primary, 'unit', description[2])

    for block in soup.select('.about-content-box .content #wp_news_w5'):
        parent = None
        for paragraph in block.find_all('p', recursive=False):
            text = clean(paragraph.get_text(' ', strip=True))
            following = paragraph.find_next_sibling()
            if paragraph.find('strong') and text and following is not None and following.name == 'hr':
                parent = add(text, '', locator(paragraph), owner_key, 'group', 'directory_group')
            elif text and parent:
                add_entries(paragraph, parent, False)
        replace(block)
    for block in soup.select('.dropdown-item'):
        headings = block.select(':scope > h3.dropdown-item-title')
        if len(headings) != 1:
            continue
        heading = headings[0]
        name = clean(heading.get_text(' ', strip=True))
        parent = add(name, '', locator(heading), owner_key, 'group', 'menu_group')
        for listing in block.select(':scope > ul.dropdown-item-list'):
            add_entries(listing, parent, True)
        replace(block)
    return nodes, links, replaced, notes


def medical_variants(soup, page_url):
    if not medical_directory(page_url) or urlsplit(page_url).path != '/55143/list.htm':
        return []
    nodes, _, _, _ = medical_relationships(soup, page_url, 'https://www.zju.edu.cn/', 'owner')
    by_key = {n['key']: n for n in nodes}
    views = {'正文组织机构': {}, '顶部机构目录': {}}
    for node in nodes:
        if node['kind'] != 'unit':
            continue
        parent = by_key[node['parent']]
        ancestor = parent
        while ancestor['parent'] != 'owner':
            ancestor = by_key[ancestor['parent']]
        view = '顶部机构目录' if ancestor['relation'] == 'menu_group' else '正文组织机构'
        wording = {'sub_unit': '官网明确写明下设', 'directory_companion_entry': '括号列示，隶属关系待核实'}.get(node['relation'], '')
        views[view].setdefault((node['name'], parent['name']), set()).add((node['url'], wording))
    if not all(views.values()):
        return []
    result = []
    for identity in sorted(set().union(*(set(entries) for entries in views.values()))):
        signatures = [frozenset(entries[identity]) if identity in entries else None for entries in views.values()]
        if len(set(signatures)) > 1:
            result.append({'name': identity[0], 'parent': identity[1], 'versions': [
                {'name': label, 'present': identity in entries,
                 'urls': sorted({url for url, _ in entries.get(identity, set())}),
                 'relationship': '；'.join(sorted({wording for _, wording in entries.get(identity, set()) if wording}))}
                for label, entries in views.items()]})
    return result
