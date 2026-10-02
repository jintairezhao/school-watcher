"""Publish official units and attach columns to their observed website owners."""
from collections import defaultdict
import hashlib

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School
from backend.services.source_inventory import canonical_url, site_key

STRUCTURAL_KINDS = ('unit', 'group')


def sync_official_structure(school, catalog, *, commit=True):
    from backend.services.source_relationships import SourceRelationships, ROSTER_RELATIONS
    from backend.services.source_placements import _within_unit, _same_page
    key = site_key(school.url)
    report = catalog.report(key)
    if not report:
        return {'units': 0, 'linked_columns': 0}
    records = catalog.structure(key)
    relationships = SourceRelationships(report, records, directory_navigation=True)
    db.session.execute(db.update(School).where(School.id == school.id).values(name=School.name))
    existing = school.departments.order_by(Department.id).all()
    indexed = {d.structure_key: d for d in existing if d.structure_key}
    links = {(e.parent_id, e.department_id) for e in DepartmentDirectoryEntry.query.join(
        Department, Department.id == DepartmentDirectoryEntry.parent_id).filter(Department.school_id == school.id)}
    nodes_by_reference = defaultdict(list)
    for node in records:
        page = relationships.pages.get(node['reference_url'])
        if (page and page['state'] == 'fetched' and page.get('content_hash') == node['content_hash']
                and relationships._school_page(page.get('final_url') or page['url'])):
            nodes_by_reference[node['reference_url']].append(node)
    units_by_address = defaultdict(list)

    def link(parent, child, position):
        if parent.id != child.id and (parent.id, child.id) not in links:
            db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id, position=position))
            links.add((parent.id, child.id))

    def structural_node(name, kind, address, group, identity):
        stable = hashlib.sha256(identity.encode()).hexdigest()
        item = indexed.get(stable)
        if item is None:
            imported = [d for d in existing if d.kind == kind and not d.structure_key
                        and d.name == name and canonical_url(d.list_url or '') == address]
            item = imported[0] if len(imported) == 1 else Department(school_id=school.id, kind=kind,
                name=name, list_url=address, group_name=group)
            item.structure_key = stable
            db.session.add(item); db.session.flush()
            indexed[stable] = item
        return item

    for reference, nodes in nodes_by_reference.items():
        by_key = defaultdict(list)
        for node in nodes:
            by_key[node['node_key']].append(node)
        for position, node in enumerate(nodes):
            if node['kind'] != 'unit' or node['relation'] not in ROSTER_RELATIONS:
                continue
            for chain in relationships._roster_paths(node, by_key):
                group_nodes = [n for n in chain if n['kind'] == 'group' and n['relation'] in
                               ('page_identity', 'menu_group', 'directory_group', 'academic_group', 'campus_group')]
                group = group_nodes[0]['name'] if group_nodes else report['site']['name']
                parent = structural_node(group, 'group', reference, group, f'{school.id}:group:{group}')
                for item in chain:
                    # Table captions and parallel labels are not departments.
                    is_unit = item['kind'] == 'unit' and item['relation'] in ROSTER_RELATIONS
                    is_group = item['kind'] == 'group' and item in group_nodes[1:]
                    if not is_unit and not is_group:
                        continue
                    address = canonical_url(item.get('url') or '')
                    identity = '\0'.join((str(school.id), item['name'], address.split('://', 1)[-1] if address else
                        (parent.structure_key if parent else group) + ':' + item['node_key']))
                    unit = structural_node(item['name'], 'unit' if is_unit else 'group', address, group, identity)
                    if parent:
                        link(parent, unit, position)
                    parent = unit
                    if address and is_unit:
                        for alias in relationships._addresses(address):
                            if unit not in units_by_address[alias]:
                                units_by_address[alias].append(unit)

    linked_columns = 0
    for source in existing:
        if source.kind in STRUCTURAL_KINDS or not source.list_selector:
            continue
        paths = relationships.paths_for(source.list_url)
        owners = {}
        school_entry = any(p.get('basis') == 'school_website_entry' and not p.get('nodes') for p in paths)
        if _same_page(source.list_url, school.url) or school_entry:
            if not source.group_name:
                source.group_name = '学校栏目'
            continue
        for path in paths:
            if path.get('basis') not in ('official_website_entry', 'official_directory_navigation'):
                continue
            owner = next((n for n in reversed(path.get('nodes', [])) if n['kind'] == 'unit'), None)
            if not owner:
                continue
            # A link from one college to another's site is navigation, not ownership.
            if not any(_within_unit(target, address, school.url)
                       for address in relationships._addresses(owner['url'])
                       for target in relationships._addresses(source.list_url)):
                continue
            for unit in units_by_address.get(canonical_url(owner['url']), []):
                if unit.name == owner['name']:
                    owners[unit.id] = unit
        if len(owners) == 1:
            owner = next(iter(owners.values()))
            link(owner, source, 0)
            linked_columns += 1
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return {'units': sum(d.kind == 'unit' for d in indexed.values()), 'linked_columns': linked_columns}
