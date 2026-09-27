"""Project published official paths into the inbox without changing source identities.

Only the compact runtime catalogue is read. URL/name checks below reject unrelated
cross-links in already observed paths; they never create a new relationship.
"""
import hashlib
import re
from urllib.parse import urlsplit

from flask import current_app, g, has_app_context, has_request_context, request

from backend.database.models import Department, School
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_inventory import canonical_url, site_key


def _address(url):
    try:
        return canonical_url(url or '')
    except (TypeError, ValueError, UnicodeError):
        return ''


def _same_page(a, b):
    """Recognize root HTTP/HTTPS forms without treating sibling paths as aliases."""
    a, b = urlsplit(_address(a)), urlsplit(_address(b))
    return bool(a.hostname and b.hostname and
                (a.hostname, a.port, a.path.rstrip('/'), a.query, a.fragment) ==
                (b.hostname, b.port, b.path.rstrip('/'), b.query, b.fragment))


def _within_unit(source_url, unit_url, school_url):
    if not _address(unit_url) or _same_page(unit_url, school_url):
        return False
    source, unit = urlsplit(_address(source_url)), urlsplit(_address(unit_url))
    if (source.hostname, source.port) != (unit.hostname, unit.port):
        return False
    # Query- and fragment-routed identities cannot supply an inferred URL scope.
    if unit.query or unit.fragment:
        return _same_page(source_url, unit_url)
    path = unit.path.rstrip('/')
    return source.path == path or source.path.startswith(path + '/')


def _named_owner(source, units):
    name = (source.name or '').strip()
    group = (source.group_name or '').strip()
    names = [n['name'].strip() for n in units]
    prefixes = [names[-1], '-'.join(names)]
    return group == names[-1] or any(name == p or any(name.startswith(p + sep)
        for sep in ('-', '－', '—')) for p in prefixes)


def _column_label(source, units):
    name = (source.name or '').strip()
    names = [n['name'].strip() for n in units]
    if name in (names[-1], '-'.join(names)):
        return '本部门通知'
    for owner in sorted(names + ['-'.join(names)], key=len, reverse=True):
        for separator in ('-', '－', '—'):
            if name.startswith(owner + separator):
                return name[len(owner) + 1:].strip() or '本部门通知'
    return name


def _scoped_column_label(source, units, path):
    label = _column_label(source, units)
    if label not in {'通知公告', '通知', '公告', '新闻动态', '最新动态', '新闻公告', '招生工作', '招生信息'}:
        return label
    if _same_page(source.list_url, units[-1].get('url')):
        return label
    owners = {n['name'].strip() for n in units}
    trail = [n.get('name', '').strip() for n in path.get('entry_nodes', [])
             if n.get('kind') == 'group' and n.get('name', '').strip() not in owners | {'首页', label}
             and not re.search(r'登录|登出|退出|\blogin\b|sign in', n.get('name', ''), re.I)]
    return ' / '.join(list(dict.fromkeys(trail))[-2:] + [label]) if trail else label


def _unit_key(school_url, node):
    # The same unit observed in two directory documents retains one identity.
    identity = '\0'.join((site_key(school_url), _address(node.get('url')), node['name'].strip()))
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _path_units(path):
    return [node for node in path.get('nodes', [])
            if node.get('kind') == 'unit' and node.get('name', '').strip()]


def placements_from_paths(source, school_url, paths, known_groups):
    """Pure evidence filter used by the request adapter and regression fixtures."""
    if not _address(source.list_url) or _same_page(source.list_url, school_url):
        return []
    # A school-level entrance may be linked from many unit homepages. Keep its
    # existing school-level placement instead of mistaking those links for owners.
    if any(p.get('basis') == 'school_website_entry' and not p.get('nodes') for p in paths):
        # A unit homepage also appears in school-wide navigation. Its exact
        # observed name AND homepage address establish its own identity despite
        # that extra navigation link; generic news/column names cannot do this.
        paths = [path for path in paths if (units := _path_units(path)) and
                 _named_owner(source, units) and
                 _same_page(units[-1].get('url'), source.list_url)]
        if not paths:
            return []
    candidates = []
    current_group = (source.group_name or '').strip()
    for path in paths:
        if path.get('basis') != 'official_website_entry':
            continue
        nodes = path.get('nodes') or []
        if any(n.get('kind') == 'major' for n in nodes):
            continue
        units = _path_units(path)
        if not units:
            continue
        first_unit = nodes.index(units[0])
        group = next((n.get('name', '').strip() for n in nodes[:first_unit]
                      if n.get('kind') == 'group' and n.get('relation') in
                      ('page_identity', 'menu_group', 'directory_group', 'academic_group', 'campus_group')
                      and n.get('name', '').strip()), '')
        if not group:
            continue
        # Existing navigation groups suppress side-site/admissions directories.
        # An accidentally promoted unit name can be repaired from its own path.
        if group not in known_groups and current_group not in {n['name'].strip() for n in units}:
            continue
        if not (_named_owner(source, units) or
                _within_unit(source.list_url, units[-1].get('url'), school_url)):
            continue
        candidates.append({'group': group,
                           'nodes': [{'key': _unit_key(school_url, n), 'name': n['name'].strip()} for n in units],
                           'label': _scoped_column_label(source, units, path)})
    known = [candidate for candidate in candidates if candidate['group'] in known_groups]
    if known:
        candidates = known
    elif len({candidate['group'] for candidate in candidates}) > 1:
        # With no established top-level group, conflicting directory labels need
        # verification before one accidentally promoted unit can create roots.
        return []
    # Alternate titles for the same directory must not multiply a known group.
    if current_group and any(c['group'] == current_group for c in candidates):
        candidates = [c for c in candidates if c['group'] == current_group]
    result, seen = [], set()
    for candidate in candidates:
        identity = (candidate['group'], tuple(n['key'] for n in candidate['nodes']))
        if identity not in seen:
            seen.add(identity)
            result.append(candidate)
    return result


def placements_from_unit_identity(source, school_url, unit_paths, known_groups):
    """Bridge an older source to one evidenced unit, with three identity checks.

    This only applies to a stored unit source (exact unit name and existing group),
    whose URL lies inside that evidenced unit's website. A bare name, a changed
    CMS URL, or two plausible unit identities leaves the legacy source separate.
    """
    group = (source.group_name or '').strip()
    name = (source.name or '').strip()
    if not group or not name:
        return []
    candidates = []
    for path in unit_paths:
        units = _path_units(path)
        if (not units or units[-1]['name'].strip() != name or
                not _within_unit(source.list_url, units[-1].get('url'), school_url)):
            continue
        candidates.extend(p for p in placements_from_paths(source, school_url, [path], known_groups)
                          if p['group'] == group)
    if len({p['nodes'][-1]['key'] for p in candidates}) != 1:
        return []
    unique = {}
    for candidate in candidates:
        unique.setdefault((candidate['group'], tuple(n['key'] for n in candidate['nodes'])), candidate)
    return list(unique.values())


def _unit_path_index(paths):
    """Index each observed roster path once, independent of its many columns."""
    result, seen = {}, set()
    for entries in paths.values():
        for path in entries:
            if path.get('basis') != 'official_website_entry':
                continue
            units = _path_units(path)
            if not units:
                continue
            identity = tuple((n.get('kind'), n.get('relation'), n.get('name'), _address(n.get('url')))
                             for n in path.get('nodes', []))
            if identity in seen:
                continue
            seen.add(identity)
            result.setdefault(units[-1]['name'].strip(), []).append(path)
    return result


def official_source_placements(departments):
    """Return placements only for requested IDs; cache catalogue reads per request."""
    if not has_app_context():
        return {}
    departments = list(departments)
    if not departments:
        return {}
    catalog_path = current_app.config.get('SOURCE_CATALOG_PATH')
    if not catalog_path:
        return {}
    request_object = request._get_current_object() if has_request_context() else None
    cache = (getattr(g, '_official_source_placement_cache', None)
             if request_object is not None and
             getattr(g, '_official_source_placement_request', None) is request_object else None)
    if cache is None:
        cache = {}
        if has_request_context():
            g._official_source_placement_cache = cache
            g._official_source_placement_request = request_object
    school_ids = {d.school_id for d in departments}
    missing = {i for i in school_ids if (catalog_path, i) not in cache}
    if missing:
        schools = {s.id: s for s in School.query.filter(School.id.in_(missing)).all()}
        groups = {i: set() for i in missing}
        for ident, name in (Department.query.with_entities(Department.school_id, Department.group_name)
                            .filter(Department.school_id.in_(missing)).distinct().all()):
            if name and name.strip():
                groups[ident].add(name.strip())
        catalog = RuntimeCatalog(catalog_path)
        for ident in missing:
            school = schools.get(ident)
            cache[(catalog_path, ident)] = ({'school_url': school.url, 'groups': groups[ident],
                'paths': catalog.paths(site_key(school.url)), 'resolved': {}} if school else None)
    result = {}
    for source in departments:
        school = cache.get((catalog_path, source.school_id))
        if not school:
            continue
        identity = (source.id, source.name, source.group_name, source.list_url)
        if identity not in school['resolved']:
            paths = school['paths'].get(_address(source.list_url), [])
            placements = placements_from_paths(source, school['school_url'], paths, school['groups'])
            if not placements and not any(path.get('basis') == 'school_website_entry' and
                                          not path.get('nodes') for path in paths):
                if 'unit_paths' not in school:
                    school['unit_paths'] = _unit_path_index(school['paths'])
                placements = placements_from_unit_identity(source, school['school_url'],
                    school['unit_paths'].get((source.name or '').strip(), []), school['groups'])
            school['resolved'][identity] = placements
            if not placements and current_app.config.get('DESKTOP_MODE'):
                from backend.services.starter_catalog import placements as starter_placements
                school['resolved'][identity] = starter_placements(school['school_url'], source)
        if school['resolved'][identity]:
            result[source.id] = school['resolved'][identity]
    return result
