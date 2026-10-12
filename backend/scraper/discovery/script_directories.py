"""Explicit ZJU template data, preserving its indexed directory positions."""
import json
import re
from urllib.parse import urlsplit

from backend.scraper.http_client import same_school_url, validate_public_url
from backend.services.source_inventory import resolve_page_link
from .literal_data import literal_assignment


def literal_navigation_material(element, link, page_url, cache=None):
    """Observe one indexed menu entry without evaluating website JavaScript.

    The DOM selector identifies its actual script. The suffix identifies one
    parsed literal entry; neither the candidate name nor URL supplies truth.
    """
    if element is None or element.name != 'script' or element.get('src'):
        return None
    _, separator, suffix = (link.get('locator') or '').partition('::text(')
    if not separator or not suffix.endswith(')'):
        return None
    position = suffix[:-1]
    path = re.fullmatch(r'([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)'
                        r'(\[(?:0|[1-9][0-9]{0,5})\](?:\.children\[(?:0|[1-9][0-9]{0,5})\])*)', position)
    if not path:
        return None
    binding = path[1]
    cache = {} if cache is None else cache
    key = ('literal_navigation', id(element), binding)
    if key not in cache:
        try:
            cache[key] = literal_assignment(element.get_text(), binding)
        except ValueError:
            cache[key] = None
    entries = cache[key]
    ancestors = []
    indexes = [int(value) for value in re.findall(r'\[([0-9]+)\]', path[2])]

    def summary(entry):
        if not isinstance(entry, dict):
            return None
        fields = ('name', 'href') if isinstance(entry.get('name'), str) else ('title', 'link')
        if any(not isinstance(entry.get(field), str) for field in fields):
            return None
        return {fields[0]: entry[fields[0]], fields[1]: entry[fields[1]]}

    for depth, index in enumerate(indexes):
        if not isinstance(entries, list) or index >= len(entries) or not summary(entries[index]):
            return None
        entry = entries[index]
        if depth < len(indexes) - 1:
            ancestors.append(summary(entry))
            entries = entry.get('children', [])
    observed = summary(entry)
    name = entry.get('name', entry.get('title', '')).strip()
    introduction = link.get('label') == name + '（官网介绍）'
    if link.get('label') != name and not introduction:
        return None
    href = entry.get('url') if introduction else entry.get('href', entry.get('link', ''))
    if not isinstance(href, str):
        return None
    try:
        target = resolve_page_link(page_url, href)
    except ValueError:
        return None
    if target != link.get('url'):
        return None
    if introduction:
        observed = dict(observed, url=href)
    neighbors = [value for candidate in entries[max(0, index - 1):index + 2]
                 if (value := summary(candidate))]
    children = [value for candidate in entry.get('children', []) if (value := summary(candidate))]
    text = json.dumps({'position': position, 'entry': observed, 'ancestors': ancestors,
                       'nearby_entries': neighbors, 'children': children}, ensure_ascii=False)
    from backend.ai.skill_loader import digest
    hierarchy = [value.get('name', value.get('title', '')).strip() for value in ancestors]
    return {'key': digest(['literal-navigation-1', urlsplit(page_url).netloc,
                           link['locator'], element.get_text(), text]),
            'text': text, 'hierarchy': hierarchy}


def zju_relationships(soup, page_url, root_url, owner_key):
    from .structure import classify, locator, node_key, SERVICE, FILES, SKIP
    nodes, links, notes = [], [], []
    page = urlsplit(page_url)
    if page.hostname != 'www.zju.edu.cn':
        return nodes, links, notes
    bindings = ['main._menu', 'list._menu']
    if page.path == '/xywxw/list.htm':
        bindings.append('list._college')

    def decision(name, target):
        if not target:
            return 'missing_link'
        try:
            validate_public_url(target, resolve=False)
        except ValueError:
            return 'restricted_or_service'
        if name in SKIP or SERVICE.search(name) or re.search(
                r'https?://(?:vpn|webvpn|auth|sso|portal|mail|mails|id)\.', target, re.I):
            return 'restricted_or_service'
        if FILES.search(target):
            return 'document_reference'
        return 'follow' if same_school_url(target, root_url) else 'official_external_link'

    for binding in bindings:
        # Multiple definitions or additional code invalidate this binding. String
        # contents and comments are never interpreted as executable assignments.
        candidates = [s for s in soup.find_all('script') if not s.get('src') and
                      re.search(r'\b' + re.escape(binding).replace(r'\.', r'\s*\.\s*') + r'\s*=', s.get_text())]
        if not candidates:
            continue
        try:
            if len(candidates) != 1:
                raise ValueError('Multiple binding definitions')
            script = candidates[0]
            data = literal_assignment(script.get_text(), binding)
            college = binding.endswith('_college')
            name_field, url_field = ('title', 'link') if college else ('name', 'href')

            def validate(entries):
                if not isinstance(entries, list):
                    raise ValueError('Expected directory array')
                for entry in entries:
                    if not isinstance(entry, dict) or any(
                            not isinstance(entry.get(field), str) for field in (name_field, url_field)):
                        raise ValueError('Missing directory label or address')
                    if not entry[name_field].strip() or len(entry[name_field]) > 120:
                        raise ValueError('Invalid directory label')
                    if college and not isinstance(entry.get('url'), str):
                        raise ValueError('Missing separate introduction address')
                    validate(entry.get('children', []))

            validate(data)
            local_nodes, local_links, introductions = [], [], []

            def visit(entries, parent, path, trail, depth):
                for index, entry in enumerate(entries):
                    position = f'{path}[{index}]'
                    loc = locator(script) + '::text(' + position + ')'
                    name = entry[name_field].strip()
                    target = resolve_page_link(page_url, entry[url_field])
                    children = entry.get('children', [])
                    if college:
                        kind = 'group' if depth == 0 else 'unit'
                        relation = 'academic_group' if depth == 0 else 'directory_entry' if depth == 1 else 'nested_directory_entry'
                        link_kind = 'directory' if depth == 0 else 'unit'
                    else:
                        detected = classify(name)
                        kind = 'channel' if detected == 'channel' else 'group'
                        relation = 'navigation_entry'
                        link_kind = 'directory' if children or name in ('学校机构', '学院（系）') else detected or 'navigation'
                        if link_kind == 'unit':
                            link_kind = 'navigation'
                    key = node_key(kind, name, page_url + '#data-position:' + position)
                    local_nodes.append({'key': key, 'name': name, 'url': target, 'kind': kind,
                                        'parent': parent, 'locator': loc, 'relation': relation})
                    local_links.append({'label': name, 'url': target, 'kind': link_kind, 'path': trail,
                                        'locator': loc, 'decision': decision(name, target)})
                    if college and entry['url']:
                        introduction = resolve_page_link(page_url, entry['url'])
                        introductions.append({'name': name, 'website_url': target,
                                              'introduction_url': introduction, 'locator': loc})
                        local_links.append({'label': name + '（官网介绍）', 'url': introduction,
                                            'kind': 'navigation', 'path': trail, 'locator': loc,
                                            'decision': decision(name, introduction)})
                    visit(children, key, position + '.children', trail + [name], depth + 1)

            visit(data, owner_key, binding, [], 0)
            nodes.extend(local_nodes)
            links.extend(local_links)
            notes.append('literal_directory_evidence:' + json.dumps(
                {'binding': binding, 'locator': locator(script), 'positions': len(local_nodes),
                 'introductions': introductions}, ensure_ascii=False))
        except ValueError as exc:
            notes.append('literal_directory_requires_review:' + binding + ':' + str(exc))
    return nodes, links, notes
