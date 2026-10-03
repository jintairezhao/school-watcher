"""Readable publication channels from saved evidence, without an organisation audit.

This projection never fetches websites or calls AI. A publisher is the observed
website or unit carrying a column; it is not a claim about an article's author.
"""
from collections import defaultdict
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from flask import current_app, g, has_app_context, has_request_context, request

from backend.database.models import Department, DepartmentDirectoryEntry, School
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_inventory import canonical_url, site_key, website_scope_path
from backend.services.source_ownership import BRANDING_PREFIX
from backend.services.announcement_sources import _publication_url


def _address(value):
    try:
        return canonical_url(_publication_url(value))
    except (TypeError, ValueError, UnicodeError):
        return ''


def _key(school_id, kind, *parts):
    material = '\0'.join(str(part) for part in (school_id, kind, *parts))
    return kind + ':' + hashlib.sha256(material.encode()).hexdigest()[:24]


def _scope_contains(address, entrance):
    target, root = urlsplit(_address(address)), urlsplit(_address(entrance))
    if not root.hostname or (target.hostname, target.port) != (root.hostname, root.port):
        return False
    if root.query or root.fragment:
        return (target.path, target.query, target.fragment) == (root.path, root.query, root.fragment)
    path = website_scope_path(root.path)
    return target.path == path or target.path.startswith(path + '/')


def _column_label(source, units=()):
    label = (source.name or '').strip() or '官网信息'
    for name in sorted({row['name'] for row in units}, key=len, reverse=True):
        for separator in ('-', '－', '—', ' / '):
            if label.startswith(name + separator):
                label = label[len(name + separator):].strip() or label
                break
    return label


def _unit_chain(source_id, data):
    """Use actual memberships; menu groups cannot become publishers."""
    def visit(ident, visited):
        if ident in visited:
            return []
        result = []
        for parent_id in data['parents'].get(ident, []):
            parent = data['departments'].get(parent_id)
            if not parent:
                continue
            ancestors = visit(parent_id, visited | {ident}) or [[]]
            unit = [{'id': parent.id, 'name': parent.name, 'url': parent.list_url or ''}] if parent.kind == 'unit' else []
            result.extend(chain + unit for chain in ancestors)
        return result
    chains = [chain for chain in visit(source_id, set()) if chain]
    owners = {chain[-1]['id'] for chain in chains}
    return max(chains, key=len) if len(owners) == 1 else []


def _catalog_paths(data, catalog_path):
    if 'catalog_paths' in data:
        return
    data['catalog'] = RuntimeCatalog(catalog_path)
    data['catalog_paths'] = data['catalog'].paths(site_key(data['school'].url))
    data['catalog_pages'] = {}
    data['pages_by_address'] = {}
    data['metadata_checked'] = set()


def _metadata_addresses(source, data):
    """Bounded local lookups: constructed parent URLs are never fetched or evidence."""
    address = _address(source.list_url)
    if not address:
        return []
    parts = urlsplit(address)
    result = [address]
    # Prefer explicitly saved website entrances, including default documents.
    for path in data['catalog_paths'].get(address, []):
        if path.get('identity_pending'):
            continue
        for node in reversed(path.get('nodes', [])):
            entrance = _address(node.get('url'))
            if entrance and _scope_contains(address, entrance):
                result.append(entrance)
    for ident in data['parents'].get(source.id, []):
        parent = data['departments'].get(ident)
        entrance = _address(parent.list_url) if parent else ''
        if entrance and _scope_contains(address, entrance):
            result.append(entrance)
    root = _address(data['school'].url)
    if root and _scope_contains(address, root):
        result.append(root)
    # Query/fragment shells do not establish ownership of another routed page.
    parent = parts.path.rstrip('/').rsplit('/', 1)[0]
    for _ in range(3):
        if not parent:
            break
        result.append(urlunsplit((parts.scheme, parts.netloc, parent + '/', '', '')))
        parent = parent.rsplit('/', 1)[0]
    result.append(urlunsplit((parts.scheme, parts.netloc, '/', '', '')))
    return list(dict.fromkeys(result))[:12]


def _load_page_metadata(sources, data):
    wanted = {url for source in sources for url in _metadata_addresses(source, data)}
    missing = wanted - data['metadata_checked']
    if not missing:
        return
    pages = data['catalog'].page_metadata(site_key(data['school'].url), sorted(missing))
    data['metadata_checked'].update(missing)
    for page in pages.values():
        if page.get('state') != 'fetched':
            continue
        address = _address(page.get('url'))
        if not address:
            continue
        data['catalog_pages'][address] = page
        data['pages_by_address'][address] = page
        final = _address(page.get('final_url'))
        if final and page.get('health') != 'dynamic_content':
            data['pages_by_address'][final] = page


def _path_units(source, data):
    candidates = {}
    for path in data['catalog_paths'].get(_address(source.list_url), []):
        if path.get('basis') not in ('official_website_entry', 'official_directory_navigation') or path.get('identity_pending'):
            continue
        nodes = path.get('nodes', [])
        if any(node.get('kind') == 'major' for node in nodes):
            continue
        units = [dict(name=node['name'], url=node.get('url') or '') for node in nodes
                 if node.get('kind') == 'unit' and node.get('name')]
        if not units or not _scope_contains(source.list_url, units[-1]['url']):
            continue
        owner = units[-1]
        if _address(owner['url']).rstrip('/') == _address(data['school'].url).rstrip('/'):
            continue
        candidates[(_address(owner['url']), owner['name'])] = units
    return next(iter(candidates.values())) if len(candidates) == 1 else []


def _title_identity(title, column_label, school_name=''):
    """Recognise explicit site-title slots, never infer a unit from its hostname."""
    title = (title or '').strip()
    parts = [part.strip() for part in re.split(r'\s*[|｜_—–-]\s*', title) if part.strip()]
    generic = {column_label, '首页', '主页', '通知公告', '通知', '公告', '新闻动态', '新闻', '信息公开', 'Home'}
    choices = [part for part in parts if part not in generic]
    if len(choices) > 1:
        choices = [part for part in choices if part != school_name]
    if len(choices) != 1:
        return ''
    label = choices[0]
    # A title like "关于成立某学院的通知" is a document title, not site branding.
    if (not 2 <= len(label) <= 100 or re.search(r'关于|关于开展|通知|公告|来访|访问|调研|召开|举行', label)
            or not re.search(r'大学|学院|学部|书院|教务|研究生院|学生工作|学生事务|招生|就业|图书馆|中心|办公室|委员会|[处部院馆]$', label)):
        return ''
    return label


def _page_identity(page, school, column_label):
    if not page or not page.get('content_hash'):
        return ''
    candidates = []
    try:
        notes = json.loads(page.get('notes_json') or '[]')
    except (TypeError, ValueError):
        notes = []
    for note in notes:
        if not isinstance(note, str) or not note.startswith(BRANDING_PREFIX):
            continue
        try:
            evidence = json.loads(note[len(BRANDING_PREFIX):])
            if (evidence.get('site_key') != site_key(school.url)
                    or evidence.get('reference_url') != page.get('url')
                    or evidence.get('final_url') != (page.get('final_url') or page.get('url'))
                    or evidence.get('content_hash') != page['content_hash']):
                continue
            candidates.extend(evidence.get('candidates', []))
        except (TypeError, ValueError):
            continue
    for source_kind in ('og:site_name', 'header_logo_alt'):
        labels = {(row.get('identity') or '').strip() for row in candidates if isinstance(row, dict)
                  and row.get('source') == source_kind and row.get('locator')}
        labels = {label for label in labels if 2 <= len(label) <= 100 and label not in {column_label, '首页', '官网', '网站', 'logo', 'Logo', 'LOGO'}}
        if len(labels) == 1:
            return labels.pop()
    return _title_identity(page.get('title'), column_label, school.name)


def _identity_entrance(source, label, data):
    """Unify site identity only with an observed matching website entrance."""
    choices = []
    for page in data['catalog_pages'].values():
        address = page.get('final_url') or page.get('url') or ''
        if (page.get('kind') in ('root', 'unit', 'gateway') and _scope_contains(source.list_url, address)
                and _page_identity(page, data['school'], '') == label):
            choices.append(address)
    return max(choices, key=lambda value: len(urlsplit(value).path)) if choices else _address(source.list_url)


def _descriptor(source, data, units):
    school = data['school']
    label = _column_label(source, units)
    if units:
        publisher = units[-1]
        publisher_key = (_key(school.id, 'unit', _address(publisher['url']), publisher['name'])
                         if _address(publisher['url']) else _key(school.id, 'unit', publisher['id']))
        publisher_label, basis = publisher['name'], ('stored_unit' if 'id' in publisher else 'official_path')
        publisher_address = _address(publisher['url'])
    else:
        page = data['pages_by_address'].get(_address(source.list_url))
        publisher_label = _page_identity(page, school, label)
        if publisher_label:
            entrance = _identity_entrance(source, publisher_label, data)
            publisher_key = _key(school.id, 'site', entrance, publisher_label)
            publisher_address = entrance
            basis = 'site_identity'
        else:
            address = _address(source.list_url)
            publisher_label = _display_address(address) or '官网信息来源'
            publisher_key = _key(school.id, 'page', address or source.id)
            publisher_address = address
            basis = 'official_page'
        # Legacy grouping is useful column context, not proof of a publisher.
        group = (source.group_name or '').strip()
        if group and group not in (label, publisher_label) and not label.startswith(group + ' / '):
            label = group + ' / ' + label
    names = [school.name, *[unit['name'] for unit in units]] if units else [school.name, publisher_label]
    names.append(label)
    breadcrumb = ' / '.join(dict.fromkeys(name for name in names if name))
    return {'source_id': source.id, 'school_id': school.id, 'school_name': school.name,
            'publisher_key': publisher_key, 'publisher_label': publisher_label, 'column_label': label,
            'breadcrumb': breadcrumb, 'official_url': _publication_url(source.list_url), 'basis': basis,
            '_publisher_address': publisher_address}


def _display_address(address):
    parts = urlsplit(address)
    label = parts.netloc + parts.path.rstrip('/')
    if parts.query:
        label += '?' + parts.query
    if parts.fragment:
        label += '#' + parts.fragment
    return label


def _disambiguate(result, cache):
    identities = defaultdict(set)
    for data in cache['schools'].values():
        if not data['school']:
            continue
        for unit in data['departments'].values():
            if unit.kind == 'unit' and unit.school_id == data['school'].id:
                address = _address(unit.list_url)
                key = _key(unit.school_id, 'unit', address, unit.name) if address else _key(unit.school_id, 'unit', unit.id)
                identities[(unit.school_id, unit.name)].add(key)
    for row in cache['descriptors'].values():
        identities[(row['school_id'], row['publisher_label'])].add(row['publisher_key'])
    for row in result.values():
        label = row['publisher_label']
        address = row.pop('_publisher_address', '')
        if len(identities[(row['school_id'], label)]) > 1 and address:
            row['publisher_label'] = label + '（' + _display_address(address) + '）'
            parts = row['breadcrumb'].split(' / ')
            row['breadcrumb'] = ' / '.join(row['publisher_label'] if part == label else part for part in parts)


def channels_for(departments):
    """Batch descriptors, reusing database/catalogue evidence within one request."""
    departments = list(departments)
    if not departments or not has_app_context():
        return {}
    catalog_path = current_app.config.get('SOURCE_CATALOG_PATH')
    request_object = request._get_current_object() if has_request_context() else None
    cache = getattr(g, '_source_channels_cache', None) if request_object is not None else None
    if cache is None or cache['request'] is not request_object or cache['catalog_path'] != catalog_path:
        cache = {'request': request_object, 'catalog_path': catalog_path, 'schools': {}, 'descriptors': {}}
        if request_object is not None:
            g._source_channels_cache = cache
    missing = {source.school_id for source in departments} - cache['schools'].keys()
    if missing:
        schools = {school.id: school for school in School.query.filter(School.id.in_(missing))}
        known = Department.query.filter(Department.school_id.in_(missing)).all()
        indexed = {row.id: row for row in known}
        parents = defaultdict(list)
        for entry in DepartmentDirectoryEntry.query.filter(DepartmentDirectoryEntry.parent_id.in_(indexed)):
            child = indexed.get(entry.department_id)
            parent = indexed[entry.parent_id]
            if child and child.school_id == parent.school_id:
                parents[entry.department_id].append(entry.parent_id)
        for ident in missing:
            cache['schools'][ident] = {'school': schools.get(ident), 'departments': indexed, 'parents': parents}
    pending = defaultdict(list)
    resolved_units = {}
    for source in departments:
        data = cache['schools'][source.school_id]
        if not data['school']:
            continue
        units = _unit_chain(source.id, data)
        if not units:
            _catalog_paths(data, catalog_path)
            units = _path_units(source, data)
        resolved_units[source.id] = units
        if not units:
            pending[source.school_id].append(source)
    for ident, sources in pending.items():
        _load_page_metadata(sources, cache['schools'][ident])
    result = {}
    for source in departments:
        data = cache['schools'][source.school_id]
        if not data['school']:
            continue
        identity = (source.id, source.school_id, source.name, source.list_url, source.group_name)
        if identity not in cache['descriptors']:
            cache['descriptors'][identity] = _descriptor(source, data, resolved_units[source.id])
        result[source.id] = dict(cache['descriptors'][identity])
    _disambiguate(result, cache)
    return result
