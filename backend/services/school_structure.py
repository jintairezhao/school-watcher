"""Publish official units and attach columns to their observed website owners."""
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
import re

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School
from backend.services.source_inventory import canonical_url, site_key

STRUCTURAL_KINDS = ('unit', 'group')
FACT_WRITER = 'official_structure'


@contextmanager
def published_structure_writer(school_id):
    """Pin published material before source DML, with the publisher's lock order."""
    from flask import current_app
    from sqlalchemy import select
    from backend.services.runtime_catalog import RuntimeCatalog, catalog_publication_lock
    try:
        root = current_app.config['SOURCE_CATALOG_PATH']
        with catalog_publication_lock(root):
            school = db.session.execute(select(School).where(School.id == school_id)
                .execution_options(populate_existing=True)).scalar_one_or_none()
            if school is None:
                raise ValueError('学校不存在')
            catalog = RuntimeCatalog(root)
            if callable(getattr(catalog, 'pin', None)):
                pinned = catalog.pin(site_key(school.url))
                catalog = pinned if pinned is not None else RuntimeCatalog(root, versioned=False)
            yield catalog
    except BaseException:
        db.session.rollback()
        raise


def _organization_assertion(html, digest, parent, child):
    """Recheck the actual row and header; a parser enum alone proves no owner."""
    if not html or hashlib.sha256(html.encode()).hexdigest() != digest:
        return None
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import locator, locator_element, table_cell_entries
    soup = BeautifulSoup(html, 'lxml')
    element = locator_element(soup, re.sub(r'::text\(\d+\)$', '', child['locator']))
    cell = (element if element is not None and element.name in ('td', 'th') else
            element.find_parent(['td', 'th']) if element is not None else None)
    table = cell.find_parent('table') if cell is not None else None
    if table is None:
        return None
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    if not rows:
        return None
    headers = rows[0].find_all(['td', 'th'], recursive=False)
    labels = [re.sub(r'\s+', '', head.get_text()) for head in headers]
    parent_index = next((i for i, label in enumerate(labels) if label in
                         ('学院', '单位名称', '部门名称', '机构名称')), None)
    if parent_index is None:
        return None
    spans = {}
    for row_index, row in enumerate(rows):
        occupied = {col: prior for col, (end, prior) in spans.items() if end > row_index}
        col = 0
        for item in row.find_all(['td', 'th'], recursive=False):
            while col in occupied:
                col += 1
            try:
                width, height = int(item.get('colspan', '1')), int(item.get('rowspan', '1'))
            except ValueError:
                return None
            if not 1 <= width <= 100 or not 1 <= height <= 1000:
                return None
            for slot in range(col, col + width):
                occupied[slot] = item
                if height > 1:
                    spans[slot] = (row_index + height, item)
            col += width
        columns = [column for column, item in occupied.items() if item is cell]
        if row_index == 0 or len(columns) != 1:
            continue
        child_index = columns[0]
        if child_index >= len(labels):
            return None
        label = labels[child_index]
        if label not in ('下属单位', '下设单位', '系', '系部', '挂靠单位'):
            return None
        parent_cell = occupied.get(parent_index)
        if parent_cell is None or parent_cell is cell:
            return None
        names = lambda node: {re.sub(r'\s+', '', item[0]) for item in table_cell_entries(node, child['reference_url'])}
        if (re.sub(r'\s+', '', parent.name) not in names(parent_cell) or
                re.sub(r'\s+', '', child['name']) not in names(cell)):
            return None
        return dict(kind='explicit_table_header', predicate='attached_unit' if label == '挂靠单位' else 'subordinate_unit',
                    direction='parent_to_child', text=label, locator=locator(headers[child_index]),
                    parent_locator=locator(parent_cell), child_locator=child['locator'])
    return None


def _directory_website_assertion(binding, unit_name, material, relationships):
    """Recheck a roster name, its row's website link and exact site branding."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    from backend.scraper.discovery.structure import locator_element, table_cell_entries
    from backend.services.source_inventory import resolve_page_link
    from backend.services.source_ownership import normalized_name
    if (binding.get('kind') != 'explicit_directory_website' or
            binding.get('direction') != 'organization_to_page' or binding.get('unit_name') != unit_name):
        return None
    reference, address = binding.get('directory_reference_url'), binding.get('website_url')
    directory, website = relationships.pages.get(reference), relationships.pages.get(address)
    if (not directory or directory.get('kind') != 'directory' or not website or
            any(page.get('state') != 'fetched' or not page.get('content_hash') for page in (directory, website))):
        return None
    bodies = [material(url) for url in (reference, address)]
    if any(not body or hashlib.sha256(body.encode()).hexdigest() != page['content_hash']
           for body, page in zip(bodies, (directory, website))):
        return None
    soup = BeautifulSoup(bodies[0], 'lxml')
    unit = locator_element(soup, re.sub(r'::text\(\d+\)$', '', binding.get('unit_locator', '')))
    entry = locator_element(soup, binding.get('entry_locator', ''))
    if unit is None or entry is None or entry.name != 'a' or not entry.get('href'):
        return None
    unit_cell = unit if unit.name in ('td', 'th') else unit.find_parent(['td', 'th'])
    entry_cell = entry.find_parent(['td', 'th'])
    row = unit_cell.find_parent('tr') if unit_cell is not None else None
    if row is None or entry_cell is None or entry_cell.find_parent('tr') is not row or entry_cell is unit_cell:
        return None
    table = row.find_parent('table')
    if table is None:
        return None
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
    cells = row.find_all(['td', 'th'], recursive=False)
    headers = rows[0].find_all(['td', 'th'], recursive=False) if rows else []
    if unit_cell not in cells or entry_cell not in cells or len(cells) != len(headers):
        return None
    labels = [re.sub(r'\s+', '', h.get_text()) for h in headers]
    if (labels[cells.index(unit_cell)] not in ('单位名称', '部门名称', '机构名称', '学院', '系', '系部', '挂靠单位', '下属单位', '下设单位') or
            labels[cells.index(entry_cell)] not in ('备注', '网站', '相关网站', '工作链接') or
            normalized_name(unit_name) not in {normalized_name(n[0]) for n in table_cell_entries(unit_cell, reference)}):
        return None
    base = soup.find('base', href=True)
    try:
        target = resolve_page_link(urljoin(reference, base['href']) if base else reference, entry['href'])
    except ValueError:
        return None
    if target not in relationships._addresses(address):
        return None
    identity = relationships._directory_website_identity(
        website, {'unit_name':unit_name}, entry.get_text(' ', strip=True))
    if (identity and (identity.get('identity_form') != binding.get('identity_form') or
            identity.get('identity_form') and entry.get_text(' ', strip=True) != binding.get('entry_label'))):
        return None
    if not identity or (identity['locator'], identity['identity']) != (binding.get('identity_locator'), binding.get('identity_text')):
        return None
    site = BeautifulSoup(bodies[1], 'lxml')
    element = locator_element(site, identity['locator'])
    text = ((element.get('content') if element.name == 'meta' else element.get('alt') if element.name == 'img'
             else element.get_text(' ', strip=True)) or '') if element is not None else ''
    if re.sub(r'\s+', '', text) != re.sub(r'\s+', '', identity['identity']):
        return None
    return dict(binding)


def _publisher_assertion(path, source, owner, receipt, material, relationships):
    """A saved owner fact additionally requires the real current source receipt."""
    if not receipt:
        return None
    return _publisher_material_assertion(path, source, material, relationships, publication=receipt)


def _profile_identity_assertion(identity, path, material, relationships):
    """Execute both profile routes and the opening statement on original DOMs."""
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import locator_element
    from backend.services.source_inventory import resolve_page_link
    from backend.services.source_ownership import profile_subject_matches, profile_identity_candidates
    from backend.services.publisher_identity import verify_profile_route, home_anchor_kind
    home, profile = identity.get('website_url'), identity.get('reference_url')
    if (identity.get('unit_name') != path.get('unit_name') or
            not profile_subject_matches(identity.get('entity_name'), path.get('unit_name'), relationships.site['name']) or
            home not in relationships.website_addresses(path)):
        return None
    documents = {}
    for url in (home, profile):
        page = relationships.pages.get(url)
        html = material(url)
        if (not page or page.get('state') != 'fetched' or not html
                or hashlib.sha256(html.encode()).hexdigest() != page.get('content_hash')):
            return None
        documents[url] = BeautifulSoup(html, 'lxml')
    if not verify_profile_route(home, profile, identity.get('profile_entry_locator', ''),
            identity.get('profile_route'), material, relationships):
        return None
    from .profile_home_reference import verify_home_return
    witness=identity.get('profile_home_witness')
    if witness and not verify_home_return(home,profile,identity.get('profile_home_locator'),witness,
            path['unit_name'],relationships,material=material):
        return None
    back = locator_element(documents[profile], identity.get('profile_home_locator', ''))
    for node, origin, target in ((back, profile, witness['homepage_url']if witness else home),):
        if not home_anchor_kind(node):
            return None
        try:
            from urllib.parse import urljoin
            base = documents[origin].find('base', href=True)
            address = urljoin(origin, base['href']) if base else origin
            if resolve_page_link(address, node['href']) != target:
                return None
        except ValueError:
            return None
    candidates = profile_identity_candidates(documents[profile])
    if not candidates or not all(profile_subject_matches(c.get('entity_name'),
            path['unit_name'], relationships.site['name']) for c in candidates):
        return None
    if not any(all(candidate.get(field) == identity.get(field)
                   for field in ('source', 'entity_name', 'identity', 'locator', 'container')) for candidate in candidates):
        return None
    title = documents[home].title
    return dict(kind='explicit_unit_self_description', unit_name=path['unit_name'],
        reference_url=profile, locator=identity['locator'], content_hash=relationships.pages[profile]['content_hash'],
        observed_at=relationships.pages[profile]['checked_at'], website_url=home,
        profile_entry_locator=identity['profile_entry_locator'], profile_home_locator=identity['profile_home_locator'],
        **({'profile_route': identity['profile_route']} if 'profile_route' in identity else {}),
        **({'profile_home_witness': witness} if witness else {}),
        website_title=title.get_text(' ', strip=True) if title else '', identity_alias_asserted=False)


def _publisher_material_assertion(path, source, material, relationships, *, publication=None):
    """Shared original-DOM check; material proof alone never saves ownership."""
    identity = path.get('website_identity') or {}
    if path.get('basis') != 'official_website_entry' or not identity.get('locator'):
        return None
    # Old parser records can label a header shortcut as a directory member.
    # A real publisher logo does not rehabilitate that false roster premise.
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import auxiliary_navigation, locator_element
    from backend.services.source_relationships import ROSTER_RELATIONS
    leaf = next((node for node in reversed(path.get('nodes', [])) if node.get('kind') == 'unit'), {})
    if leaf.get('relation') in ROSTER_RELATIONS and path.get('references'):
        roster = path['references'][0]
        html = material(roster['url'])
        if html and hashlib.sha256(html.encode()).hexdigest() == roster.get('content_hash'):
            soup = BeautifulSoup(html, 'lxml')
            normalized = lambda text: re.sub(r'\s+', '', text)
            candidates = [element for position in roster.get('locators', [])
                if (element := locator_element(soup, re.sub(r'::text\(\d+\)$', '', position))) is not None
                and normalized(element.get_text(' ', strip=True)) == normalized(path.get('unit_name', ''))]
            if candidates and all(auxiliary_navigation(element) for element in candidates):
                return None
    publisher_identity = None
    if path.get('publisher_declaration'):
        from backend.services.publisher_identity import verify_declaration
        publisher_identity = verify_declaration(path, material, relationships)
        if not publisher_identity:
            return None
    profile_witness = None
    if identity.get('source') == 'unit_profile_self_description':
        profile_witness = _profile_identity_assertion(identity, path, material, relationships)
        if not profile_witness:
            return None
    binding = path.get('website_binding')
    if binding:
        unit = next((node for node in reversed(path.get('nodes', [])) if node['kind'] == 'unit'), {})
        if (binding.get('unit_key') != path.get('unit_key') or binding.get('unit_key') != unit.get('node_key')
                or binding.get('unit_name') != unit.get('name')
                or not _directory_website_assertion(binding, path['unit_name'], material, relationships)):
            return None
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    from backend.scraper.discovery.structure import locator_element
    from backend.services.source_inventory import resolve_page_link
    from backend.services.source_placements import _within_unit
    identity_proof = entrance_proof = None
    for reference in path.get('references', []):
        url = reference['url']
        if not any(_within_unit(url, address, relationships.site['root_url'])
                   for address in relationships.website_addresses(path)):
            continue
        html = material(url)
        if not html or hashlib.sha256(html.encode()).hexdigest() != reference.get('content_hash'):
            continue
        soup = BeautifulSoup(html, 'lxml')
        identities = [identity] if identity['locator'] in reference.get('locators', []) else []
        targets = relationships._addresses(source.list_url)
        page = relationships.pages.get(url) or {}
        # A column can carry the same publisher's exact branding itself. Reuse
        # that observed identity only on the actual source page, with current
        # metadata and matching raw bytes; a missing homepage body is not a
        # reason to discard this independent witness. The recorded unit path,
        # receipt and publication entrance are still required.
        if (url in targets and path.get('unit_name') and page.get('state') == 'fetched'
                and page.get('content_hash') == reference.get('content_hash')):
            local_identity = relationships._unit_website_identity(page, path)
            if local_identity:
                identities.append(local_identity)
        for candidate in identities:
            element = locator_element(soup, candidate['locator'])
            if element is None:
                continue
            text = (element.get('content') if element.name == 'meta' else
                    element.get('alt') if element.name == 'img' else element.get_text(' ', strip=True)) or ''
            if re.sub(r'\s+', '', text) == re.sub(r'\s+', '', candidate.get('identity', '')):
                identity_proof = (url, candidate['locator'], text)
        response_url = url
        # DOM links were observed at the successful response address. Keep the
        # requested reference identity, while resolving its relative entrances
        # with the recorded response rather than guessing a protocol alias.
        if (page.get('state') == 'fetched' and page.get('health') != 'dynamic_content'
                and page.get('content_hash') == reference.get('content_hash')
                and page.get('final_url') and relationships._school_page(page['final_url'])):
            response_url = page['final_url']
        base = soup.find('base', href=True)
        base_url = urljoin(response_url, base['href']) if base else response_url
        for loc in reference.get('locators', []):
            element = locator_element(soup, loc)
            if element is None or element.name != 'a' or not element.get('href'):
                continue
            try:
                target = resolve_page_link(base_url, element['href'])
            except ValueError:
                continue
            if target in targets:
                entrance_proof = (url, loc, 'column_link')
        if (publication and url in targets and reference.get('content_hash') == publication.get('content_hash') and
                all(locator_element(soup, publication['region'].get(field, '')) is not None
                    for field in ('container', 'heading'))):
            entrance_proof = (url, publication['region']['container'], 'publication_region')
    if not identity_proof or not entrance_proof:
        return None
    return dict(kind='explicit_publisher_identity', predicate='publication_owner', direction='publisher_to_source',
        identity_reference_url=identity_proof[0], identity_locator=identity_proof[1], identity_text=identity_proof[2],
        entry_reference_url=entrance_proof[0], entry_locator=entrance_proof[1], entry_kind=entrance_proof[2],
        entry_url=source.list_url, **({'website_binding':binding} if binding else {}),
        **({'profile_identity_witness':profile_witness} if profile_witness else {}),
        **({'publisher_declaration_key':publisher_identity['node_key']} if publisher_identity else {}))


def sync_official_structure(school, catalog, *, commit=True, only_source_ids=None, materialize_source_units=False):
    """Reconcile official facts, optionally refreshing only named source placements.

    A bounded material publication can reuse registered organization identities
    without rederiving unrelated organizations or source relations. New unit
    identities are only created for verified source-owner paths when explicitly
    requested by the source installation writer. School-wide reconciliation
    remains a separate full structure publication.
    """
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
    selected_ids = None if only_source_ids is None else set(only_source_ids)
    if type(materialize_source_units) is not bool or materialize_source_units and selected_ids is None:
        raise ValueError('请选择本校需要刷新归属依据的信源')
    if selected_ids is not None:
        eligible = {row.id for row in existing if row.kind == 'column'}
        if (not selected_ids or any(type(value) is not int or value <= 0 for value in selected_ids)
                or not selected_ids <= eligible):
            raise ValueError('请选择本校需要刷新归属依据的信源')
    indexed = {d.structure_key: d for d in existing if d.structure_key}
    links = {(e.parent_id, e.department_id): e for e in DepartmentDirectoryEntry.query.join(
        Department, Department.id == DepartmentDirectoryEntry.parent_id).filter(Department.school_id == school.id)}
    bound_observations = defaultdict(set)
    for row in links.values():
        for fact in row.facts_json or []:
            if fact.get('node_key'):
                for proof in fact.get('evidence', []):
                    bound_observations[(proof.get('reference_url'), fact['node_key'])].add(row.department_id)
    existing_by_id = {row.id: row for row in existing}
    desired = defaultdict(list)
    nodes_by_reference = defaultdict(list)
    for node in records:
        page = relationships.pages.get(node['reference_url'])
        if (page and page['state'] == 'fetched' and page.get('content_hash') == node['content_hash']
                and relationships._school_page(page.get('final_url') or page['url'])):
            nodes_by_reference[node['reference_url']].append(node)
    units_by_observation = {}
    materials = {}
    created_structure_ids = set()
    related_pairs = set()

    if selected_ids is not None:
        for node in records:
            if node.get('kind') != 'unit':
                continue
            reference, node_key = node['reference_url'], node['node_key']
            parts = ((str(school.id), 'publisher_declaration', reference, node_key)
                     if node.get('relation') == 'website_publisher_declaration'
                     else (str(school.id), reference, node_key))
            unit = indexed.get(hashlib.sha256('\0'.join(parts).encode()).hexdigest())
            if unit is None:
                bound = bound_observations.get((reference, node_key), set())
                if len(bound) == 1:
                    unit = existing_by_id.get(next(iter(bound)))
            if unit is not None and unit.kind == 'unit':
                units_by_observation[(reference, node_key)] = unit

    def material(reference):
        if reference not in materials:
            materials[reference] = catalog.snapshot(key, reference)
        return materials[reference]

    def evidence(reference, locators, material, observed_at):
        page = relationships.pages.get(reference)
        if not page or page.get('state') != 'fetched' or page.get('content_hash') != material:
            return []
        at = observed_at or page.get('checked_at')
        if not at:
            return []
        return [dict(reference_url=reference, locator=locator, content_hash=material,
                     observed_at=at, parser_revision=page.get('parser_revision'))
                for locator in sorted(set(locators)) if isinstance(locator, str) and locator.strip()]

    def link(parent, child, position, relation_type, observed_relation, proofs, **context):
        if parent.id == child.id or not proofs:
            return
        pair = (parent.id, child.id)
        if materialize_source_units and child.kind in STRUCTURAL_KINDS:
            related_pairs.add(pair)
        if pair not in links:
            links[pair] = DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id,
                position=position, facts_json=[])
            db.session.add(links[pair])
        fact = dict(writer=FACT_WRITER, type=relation_type, observed_relation=observed_relation,
                    evidence=proofs, **context)
        if fact not in desired[pair]:
            desired[pair].append(fact)

    def structural_node(name, kind, address, group, identity, observation=None):
        stable = hashlib.sha256(identity.encode()).hexdigest()
        item = indexed.get(stable)
        if item is None and observation:
            bound = bound_observations.get(observation, set())
            if len(bound) == 1:
                candidate = existing_by_id.get(next(iter(bound)))
                if candidate is not None and candidate.kind == kind:
                    item = candidate
                    indexed[stable] = item
        if item is None:
            # An old unkeyed row may be a different entity at a shared address.
            # Only the stored structure key can reuse an existing identity.
            # One exception is the already configured *directory page* itself:
            # its current material establishes its type, not a unit alias.
            page = relationships.pages.get(address)
            declared_directory = [d for d in existing if kind == 'group' and page
                and page.get('kind') == 'directory' and d.kind in ('column', 'group')
                and not d.structure_key and not d.list_selector and d.name == name
                and canonical_url(d.list_url or '') == address and not d.announcements.count()]
            item = declared_directory[0] if len(declared_directory) == 1 else Department(
                school_id=school.id, kind=kind, name=name, list_url=address, group_name=group)
            item.kind = kind
            item.structure_key = stable
            db.session.add(item); db.session.flush()
            created_structure_ids.add(item.id)
            indexed[stable] = item
        return item

    from backend.services.source_path_receipts import load_source_receipts
    receipts = {r['source_id']: r for r in load_source_receipts({'name': school.name, 'root_url': school.url})}
    related_observations = set()
    if materialize_source_units:
        for source in existing:
            if source.id not in selected_ids or source.kind != 'column' or not source.list_selector:
                continue
            for path in relationships.paths_for(source.list_url):
                owner = next((node for node in reversed(path.get('nodes', [])) if node['kind'] == 'unit'), None)
                if owner is None or not _publisher_assertion(path, source, owner, receipts.get(source.id), material, relationships):
                    continue
                references = {ref['url'] for ref in path.get('references', [])}
                keys = {node['node_key'] for node in path.get('nodes', []) if node['kind'] in STRUCTURAL_KINDS}
                related_observations.update((node['reference_url'], node['node_key']) for node in records
                    if node.get('kind') == 'unit' and node['reference_url'] in references and node['node_key'] in keys)
                related_observations.update((d['reference_url'], d['node_key']) for d in relationships.publisher_declarations
                    if d['reference_url'] in references and d['node_key'] in keys)

    from backend.services.publisher_identity import verify_declaration
    for declaration in (relationships.publisher_declarations if selected_ids is None or materialize_source_units else []):
        if materialize_source_units and (declaration['reference_url'], declaration['node_key']) not in related_observations:
            continue
        checked = verify_declaration(declaration, material, relationships)
        if not checked:
            continue
        reference = checked['reference_url']
        identity = '\0'.join((str(school.id), 'publisher_declaration', reference, checked['node_key']))
        unit = structural_node(checked['name'], 'unit', checked['website_url'], '', identity)
        if (selected_ids is None or unit.id in created_structure_ids) and unit.identity_json != checked:
            unit.identity_json = checked
        units_by_observation[(reference, checked['node_key'])] = unit

    for reference, nodes in (nodes_by_reference.items() if selected_ids is None or materialize_source_units else []):
        by_key = defaultdict(list)
        for node in nodes:
            by_key[node['node_key']].append(node)
        for position, node in enumerate(nodes):
            if node['kind'] != 'unit' or node['relation'] not in ROSTER_RELATIONS:
                continue
            if materialize_source_units and (reference, node['node_key']) not in related_observations:
                continue
            for chain in relationships._roster_paths(node, by_key):
                group_nodes = [n for n in chain if n['kind'] == 'group' and n['relation'] in
                               ('page_identity', 'menu_group', 'directory_group', 'academic_group', 'campus_group')]
                group = group_nodes[0]['name'] if group_nodes else report['site']['name']
                parent = structural_node(group, 'group', reference, group, f'{school.id}:directory:{reference}')
                for item in chain:
                    # Table captions and parallel labels are not departments.
                    is_unit = item['kind'] == 'unit' and item['relation'] in ROSTER_RELATIONS
                    is_group = item['kind'] == 'group' and item in group_nodes[1:]
                    if not is_unit and not is_group:
                        continue
                    address = canonical_url(item.get('url') or '')
                    # A roster occurrence is a stable evidence binding. Identical
                    # names/URLs in another document do not prove one identity.
                    identity = '\0'.join((str(school.id), reference, item['node_key']))
                    unit = structural_node(item['name'], 'unit' if is_unit else 'group', address, group, identity,
                        observation=(reference, item['node_key']))
                    if parent:
                        assertion = (_organization_assertion(material(reference), item['content_hash'], parent, item)
                            if parent.kind == unit.kind == 'unit' and item['relation'] in ('sub_unit', 'attached_unit') else None)
                        proofs = evidence(reference, [item['locator']], item['content_hash'], item.get('observed_at'))
                        if assertion:
                            proofs.extend(evidence(reference, [assertion['locator']], item['content_hash'], item.get('observed_at')))
                        link(parent, unit, position,
                             'organization_membership' if assertion else 'directory_listing',
                             item['relation'], proofs, node_key=item['node_key'],
                             **({'assertion': assertion} if assertion else {}))
                        if is_unit:
                            for binding in relationships.website_bindings:
                                if binding['unit_key'] != item['node_key'] or binding['directory_reference_url'] != reference:
                                    continue
                                checked = _directory_website_assertion(binding, item['name'], material, relationships)
                                if not checked:
                                    continue
                                website = relationships.pages[binding['website_url']]
                                proofs = evidence(reference, [binding['unit_locator'], binding['entry_locator']],
                                    item['content_hash'], item.get('observed_at'))
                                proofs.extend(evidence(website['url'], [binding['identity_locator']],
                                    website['content_hash'], website.get('checked_at')))
                                link(parent, unit, position, 'entity_page', 'verified_directory_website', proofs,
                                    node_key=item['node_key'], page_url=website['url'], assertion=checked)
                    parent = unit
                    if is_unit:
                        units_by_observation[(reference, item['node_key'])] = unit

    linked_columns = 0
    # Pending native validation belongs to the same installation transaction.
    # Make it readable without committing independently of the task fence.
    db.session.flush()
    for source in existing:
        if selected_ids is not None and source.id not in selected_ids:
            continue
        if source.kind in STRUCTURAL_KINDS or not source.list_selector:
            continue
        paths = relationships.paths_for(source.list_url)
        # A school-home shortcut is an access path. It must not suppress a
        # separately proven publisher at the department's own website.
        if _same_page(source.list_url, school.url):
            if selected_ids is None and not source.group_name:
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
                       for address in relationships.website_addresses(path)
                       for target in relationships._addresses(source.list_url)):
                continue
            if path.get('website_binding') and not _directory_website_assertion(
                    path['website_binding'], path['unit_name'], material, relationships):
                continue
            proofs = []
            for reference in path.get('references', []):
                proofs.extend(evidence(reference['url'], reference.get('locators', []),
                    reference.get('content_hash'), reference.get('checked_at')))
            receipt = receipts.get(source.id)
            # An observed entrance without verified publisher identity is a
            # directory placement only; URL scope alone cannot grant ownership.
            assertion = _publisher_assertion(path, source, owner, receipt, material, relationships)
            if assertion:
                for role in ('identity', 'entry'):
                    reference = assertion[role + '_reference_url']
                    page = relationships.pages.get(reference) or {}
                    proofs.extend(evidence(reference, [assertion[role + '_locator']],
                        page.get('content_hash'), page.get('checked_at')))
            context = dict(config_hash=receipt['config_hash'], receipt_id=receipt['receipt_id'], assertion=assertion) if assertion else {}
            bound_units = {unit.id: unit for reference in path.get('references', [])
                if (unit := units_by_observation.get((reference['url'], owner['node_key']))) is not None}
            for unit in bound_units.values():
                link(unit, source, 0, 'source_ownership' if assertion else 'directory_listing',
                     'verified_publication_entrance' if assertion else 'observed_website_entrance', proofs, **context)
                linked_columns += 1
    # Reconcile only facts owned by this publisher. Failed rereads retain the
    # last successful material in RuntimeCatalog; unrelated/manual or untyped
    # legacy memberships are never removed by this pass.
    from .runtime_catalog import direct_directory_members
    # directory_options wrote direct page memberships. The same child and HTML
    # hash cannot validate an old flat copy after its actual local parent is known.
    current_roster_observations = {(ref, node['node_key']) for ref, nodes in nodes_by_reference.items()
                                  for node in direct_directory_members(nodes)}
    def superseded_directory_copy(fact):
        if fact.get('writer') != 'directory_options':
            return False
        for proof in fact.get('evidence', []):
            reference = proof.get('reference_url')
            page = relationships.pages.get(reference)
            if page and page.get('state') == 'fetched' and page.get('content_hash') and (
                    proof.get('content_hash') != page['content_hash'] or
                    (reference, fact.get('node_key')) not in current_roster_observations):
                return True
        return False

    for pair, row in links.items():
        related_structure = materialize_source_units and pair in related_pairs
        if selected_ids is not None and row.department_id not in selected_ids and not related_structure:
            continue
        previous = row.facts_json or []
        if related_structure:
            # A source save may add its necessary proven ancestry, but cannot
            # retire organizational facts owned by another publication.
            current = previous + [fact for fact in desired.get(pair, []) if fact not in previous]
        else:
            retained = [fact for fact in previous if fact.get('writer') != FACT_WRITER and not superseded_directory_copy(fact)]
            current = retained + desired.get(pair, [])
        current.sort(key=lambda fact: json.dumps(fact, ensure_ascii=False, sort_keys=True))
        if current != previous:
            if not current and previous:
                db.session.delete(row)
            else:
                row.facts_json = current
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    result = {'units': sum(d.kind == 'unit' for d in indexed.values()), 'linked_columns': linked_columns}
    if selected_ids is not None:
        result['scoped_source_ids'] = sorted(selected_ids)
    return result


def sync_source_structure(source, catalog, *, commit=True):
    """Install one source's facts through the canonical structure writer."""
    return sync_official_structure(source.school, catalog, commit=commit,
        only_source_ids={source.id}, materialize_source_units=True)
