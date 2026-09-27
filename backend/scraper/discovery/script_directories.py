"""Explicit ZJU template data, preserving its indexed directory positions."""
import json
import re
from urllib.parse import urlsplit

from backend.scraper.http_client import same_school_url, validate_public_url
from backend.services.source_inventory import resolve_page_link
from .literal_data import literal_assignment


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
