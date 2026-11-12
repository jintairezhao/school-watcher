"""Pure, typed projection of saved structure evidence and source configuration.

No database, network, model or mutation is performed here. Caller-supplied rows
must belong to one pinned snapshot. This module does not issue a transaction
revision, infer administrative parents, or reconstruct crawl history.
"""
from collections import defaultdict
import hashlib
import json
from urllib.parse import parse_qsl, urlsplit

from backend.services.source_inventory import canonical_url


CONFIG_FIELDS = ('name', 'list_url', 'list_selector', 'title_selector',
                 'link_selector', 'date_selector', 'content_selector', 'group_name')
DIRECTORY_RELATIONS = {
    'directory_entry', 'directory_entry_no_link', 'nested_directory_entry',
    'campus_directory_entry', 'non_entity_directory_entry',
    'directory_companion_entry', 'hidden_directory_entry',
}
ORGANIZATION_RELATIONS = {'sub_unit', 'attached_unit'}
GROUP_RELATIONS = {'page_identity', 'menu_group', 'directory_group',
                   'academic_group', 'campus_group', 'publication_group'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _url(value):
    try:
        url = canonical_url(value) if isinstance(value, str) else ''
        parsed = urlsplit(url)
        sensitive = {'token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'password'}
        # Reject credential-bearing identities instead of rewriting them into a
        # different public page and accidentally merging unrelated identities.
        queries = [parsed.query, urlsplit(parsed.fragment).query]
        if parsed.username or parsed.password or any(k.lower() in sensitive for q in queries for k, _ in parse_qsl(q)):
            return ''
        return url
    except (ValueError, UnicodeError):
        return ''


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stamp(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


def _rows(value):
    return list(value.values()) if isinstance(value, dict) else list(value)


def build_structure_facts(*, site, pages, structure_nodes, navigation_edges,
                          departments, directory_entries, validations=(), receipts=(), school_publishers=(), source_scopes=(),
                          source_page_aliases=(), entry_observations=()):
    """Return page/entity/source nodes, evidenced relations and explicit gaps.

    ``site`` supplies root_url, site_key and optionally id/school_id. All inputs
    are plain dictionaries, not ORM objects. An explicit department_id (or an
    exact stored structure_key) can bind an observed structure record to an
    entity. Name and URL similarity never establish that binding.

    Web evidence requires the same material hash as its successful source page,
    an actual locator and an observation time. A known missing original remains
    a recorded observation, not a confirmed relation. Legacy memberships without that
    evidence are gaps. Configuration-to-page edges describe saved application
    rules, with a database record locator and config hash, not official claims.
    A missing config timestamp remains null instead of being invented.
    """
    # URL validation and page identity are pure within this captured input.
    # Keep caches local so credentials or old material never persist globally.
    from functools import lru_cache
    checked_url = lru_cache(maxsize=16384)(_url)
    def checked_address(value):
        return checked_url(value) if isinstance(value, str) else ''

    root_url = checked_address(site.get('root_url') or site.get('url'))
    key = str(site.get('site_key') or _digest(root_url)[:20])
    school_id = site.get('school_id', site.get('id'))
    scope = str(school_id if school_id is not None else key)
    page_rows = {checked_address(row.get('url')): row for row in _rows(pages) if checked_address(row.get('url'))}
    nodes, relations, gaps = {}, {}, {}
    page_ids = {}

    def gap(code, **detail):
        item = dict(code=code, **detail)
        gaps[_digest(item)] = item

    def page_node(url):
        url = checked_address(url)
        if not url:
            return None
        if url in page_ids:
            return page_ids[url]
        ident = f'page:{key}:{_digest(url)[:24]}'
        page_ids[url] = ident
        if ident not in nodes:
            row = page_rows.get(url, {})
            nodes[ident] = dict(id=ident, entity_type='page', page_id=ident, url=url,
                label=row.get('label') or row.get('title') or url,
                state=row.get('state', 'observed'), content_hash=row.get('content_hash'),
                checked_at=_stamp(row.get('checked_at')))
        return ident

    def web_evidence(record, *, reference=None):
        reference = checked_address(reference or record.get('reference_url') or record.get('evidence_url'))
        row = page_rows.get(reference)
        material = record.get('content_hash')
        locator = record.get('locator')
        observed_at = _stamp(record.get('observed_at') or (row or {}).get('checked_at'))
        if not reference or not material or not isinstance(locator, str) or not locator.strip() or not observed_at:
            gap('incomplete_web_evidence', reference_url=reference,
                record_id=record.get('id') or record.get('node_key'), locator=locator or '')
            return None
        if not row or row.get('state') != 'fetched' or row.get('content_hash') != material:
            gap('stale_or_unavailable_material', reference_url=reference, content_hash=material)
            return None
        available = row.get('material_available', record.get('material_available'))
        if available is False:
            gap('original_material_not_retained', reference_url=reference, content_hash=material)
            return None
        return dict(kind='web_observation', reference_url=reference,
            locator=locator, content_hash=material, observed_at=observed_at,
            record_id=record.get('id') or record.get('node_key'),
            parser_version=record.get('parser_revision') or row.get('parser_revision'),
            material_available=available)

    def add_relation(source, target, relation_type, evidence, *, observed_relation=None, assertion=None):
        if not source or not target or source == target or not evidence:
            return
        identity = (source, target, relation_type, observed_relation)
        ident = 'relation:' + _digest(identity)[:32]
        relation = relations.setdefault(ident, dict(id=ident, source=source, target=target,
            type=relation_type, directed=True, observed_relation=observed_relation, evidence=[]))
        if evidence not in relation['evidence']:
            relation['evidence'].append(evidence)
        if assertion:
            assertions = relation.setdefault('assertions', [])
            if assertion not in assertions:
                assertions.append(dict(assertion))

    for address in page_rows:
        page_node(address)
    if root_url:
        page_node(root_url)

    department_nodes, structure_keys, source_configs = {}, defaultdict(list), {}
    access_candidates = defaultdict(list)
    department_rows = {}
    for row in _rows(departments):
        ident = row.get('id')
        if ident is None or (school_id is not None and row.get('school_id', school_id) != school_id):
            gap('foreign_or_missing_department_identity', department_id=ident)
            continue
        department_rows[ident] = row
        if row.get('kind', 'column') == 'column':
            node_id, entity_type = f'source:{ident}', 'source'
            config = {field: row.get(field) or '' for field in CONFIG_FIELDS}
            config_hash = _digest(config)
            source_configs[ident] = config_hash
            node = dict(id=node_id, entity_type=entity_type, source_id=ident,
                label=row.get('name') or '', url=checked_address(row.get('list_url')), config_hash=config_hash,
                display_group=row.get('group_name') or '',
                configured=bool(config['list_url'] and config['list_selector']),
                lifecycle=row.get('lifecycle') or 'unknown', verification='unknown',
                access='unknown')
            nodes[node_id] = node
            address = checked_address(config['list_url'])
            if address and node['configured']:
                timestamp = _stamp(row.get('config_updated_at') or row.get('created_at'))
                evidence = dict(kind='source_configuration', reference_url=address,
                    locator=f'departments:{ident}:list_url,list_selector',
                    content_hash=config_hash, observed_at=timestamp,
                    timestamp_status='known' if timestamp else 'unknown', record_id=f'departments:{ident}')
                add_relation(page_node(address), node_id, 'source_on_page', evidence)
        else:
            stable = row.get('structure_key') or str(ident)
            node_id = f'entity:{scope}:{stable}'
            entity_type = 'organization' if row.get('kind') == 'unit' else 'directory_group'
            nodes[node_id] = dict(id=node_id, entity_type=entity_type, department_id=ident,
                structure_key=row.get('structure_key'), label=row.get('name') or '',
                url=checked_address(row.get('list_url')))
            if row.get('structure_key'):
                structure_keys[row['structure_key']].append(node_id)
        department_nodes[ident] = node_id

    # Current configuration and execution evidence have separate semantics.
    latest = {}
    for row in _rows(validations):
        ident = row.get('department_id', row.get('source_id'))
        if ident not in source_configs or (school_id is not None and row.get('school_id', school_id) != school_id):
            continue
        ordering = (str(_stamp(row.get('created_at')) or ''), row.get('id') or 0)
        if ident not in latest or ordering > latest[ident][0]:
            latest[ident] = (ordering, row)
    for ident, (_, event) in latest.items():
        node = nodes[department_nodes[ident]]
        detail = _object(event.get('validation_json', event.get('validation')))
        if event.get('config_hash') != source_configs[ident]:
            gap('validation_for_other_config', source_id=ident, validation_id=event.get('id'))
            continue
        source = department_rows[ident]
        if (detail.get('department_id', ident) != ident
                or detail.get('school_id', source.get('school_id')) != source.get('school_id')
                or detail.get('config_hash', source_configs[ident]) != source_configs[ident]
                or checked_address(detail.get('list_url', source.get('list_url'))) != checked_address(source.get('list_url'))):
            gap('validation_identity_mismatch', source_id=ident, validation_id=event.get('id'))
            continue
        if event.get('id') is not None:
            access_candidates[ident].append(dict(reference=_object(detail.get('list_snapshot')),
                record_id='native:' + str(event['id']), kind='source_validation'))
        accepted = (detail.get('status') in ('full', 'partial', 'limited')
                    and _object(detail.get('articles')).get('passed') is True and not detail.get('errors'))
        region, material = _object(detail.get('region')), _object(detail.get('list_snapshot'))
        accepted = accepted and bool(region.get('container') and region.get('heading')
            and not region.get('empty') and material.get('hash')
            and checked_address(material.get('url')) == checked_address(department_rows[ident].get('list_url'))
            and material.get('outcome', 'usable') == 'usable')
        node['verification'] = 'verified' if accepted else 'not_verified'
        node['validation_id'] = event.get('id')
        node['validation_status'] = detail.get('status', 'unknown')

    # Operational observations cannot validate extraction or invent an owner.
    # Their identities and configuration must match this same frozen snapshot.
    for observation in entry_observations:
        ident = observation.get('source_id')
        if (ident in source_configs and observation.get('school_id') == school_id
                and observation.get('config_hash') == source_configs[ident]
                and observation.get('record_id') and isinstance(observation.get('reference'),dict)):
            access_candidates[ident].append(observation)

    receipt_ids = defaultdict(set)
    for ident, node_id in department_nodes.items():
        node = nodes[node_id]
        if node.get('verification') == 'verified':
            receipt_ids[ident].add('native:' + str(node['validation_id']))
    # These compact receipts come from the existing executed-evidence reader,
    # which validates native and activated-proposal evidence before returning it.
    for receipt in receipts:
        ident = receipt.get('source_id')
        if ident in source_configs and receipt.get('config_hash') == source_configs[ident] and receipt.get('receipt_id'):
            receipt_ids[ident].add(receipt['receipt_id'])
            node = nodes[department_nodes[ident]]
            node['verification'] = 'verified'
            observation = _object(receipt.get('list_observation'))
            if not observation:
                continue
            access_candidates[ident].append(dict(reference=observation, record_id=receipt['receipt_id'],
                kind='validated_list_observation', expected_hash=receipt.get('content_hash')))
            if checked_address(observation.get('url')) == node['url']:
                region = _object(receipt.get('region'))
                # The common, non-redirected case still has an executed page
                # observation. Keep its region proof on the stable config edge;
                # an observed widget establishes neither an owner nor a click.
                if (not node['configured'] or checked_address(receipt.get('url')) != node['url']
                        or observation.get('outcome', 'usable') != 'usable'
                        or observation.get('requested_url') and checked_address(observation['requested_url']) != node['url']
                        or 'status' in observation and (type(observation['status']) is not int or not 200 <= observation['status'] < 300)
                        or not observation.get('observed_at')
                        or observation.get('hash') != receipt.get('content_hash')
                        or region.get('empty') or any(not isinstance(region.get(k), str) or not region[k].strip()
                                                    for k in ('container', 'heading'))):
                    gap('source_publication_region_unverified', source_id=ident, receipt_id=receipt['receipt_id'])
                    continue
                for locator in (region['container'], region['heading']):
                    proof = web_evidence(dict(content_hash=observation['hash'], locator=locator,
                        observed_at=observation['observed_at'], id=receipt['receipt_id']), reference=node['url'])
                    add_relation(page_node(node['url']), department_nodes[ident], 'source_on_page', proof,
                        assertion=dict(kind='validated_publication_region', receipt_id=receipt['receipt_id'],
                                       config_hash=source_configs[ident]))
                continue
            requested = checked_address(observation.get('requested_url'))
            final = checked_address(observation.get('url'))
            region = _object(receipt.get('region'))
            if (not node['configured'] or not final or not requested or requested != node['url']
                    or checked_address(receipt.get('url')) != requested
                    or type(observation.get('status')) is not int or not 200 <= observation['status'] < 300
                    or observation.get('outcome', 'usable') != 'usable'
                    or not observation.get('observed_at')
                    or observation.get('hash') != receipt.get('content_hash')
                    or region.get('empty') or any(not isinstance(region.get(k), str) or not region[k].strip()
                                                for k in ('container', 'heading'))):
                gap('source_response_page_unverified', source_id=ident, receipt_id=receipt['receipt_id'])
                continue
            proof = web_evidence(dict(content_hash=observation['hash'], locator=region['container'],
                                      observed_at=observation['observed_at'], id=receipt['receipt_id']), reference=final)
            # The original configuration binding remains. This is a distinct,
            # current validated response-page binding, not a redirect click,
            # an identity merge, or a publisher assertion.
            add_relation(page_node(final), department_nodes[ident], 'source_on_page', proof,
                observed_relation='validated_response_page', assertion=dict(kind='validated_response_page',
                    requested_url=requested, response_url=final, receipt_id=receipt['receipt_id'],
                    config_hash=source_configs[ident], response_status=observation['status']))

    # A re-observed equivalent list entrance is another binding of this source
    # to its page, not navigation from one URL to the other or an identity merge.
    for alias in source_page_aliases:
        ident=alias.get('source_id');source_id=department_nodes.get(ident)
        address=checked_address(alias.get('configured_url'));target=checked_address(alias.get('alias_url'))
        references=alias.get('references')or[]
        if (ident not in source_configs or not source_id or not address or not target or address==target
                or nodes[source_id].get('url')!=address or alias.get('config_hash')!=source_configs[ident]
                or alias.get('receipt_id')not in receipt_ids[ident]
                or not {address,target}<={checked_address(r.get('url'))for r in references}):
            gap('source_page_alias_unverified',source_id=ident);continue
        proofs=[web_evidence(dict(content_hash=r.get('content_hash'),locator=locator,
                    observed_at=r.get('checked_at')),reference=r.get('url'))
                for r in references for locator in r.get('locators',[])]
        if (not proofs or any(proof is None for proof in proofs)
                or not {address,target}<={proof['reference_url']for proof in proofs if proof}):
            gap('source_page_alias_material_unavailable',source_id=ident);continue
        assertion=dict(kind='validated_listing_alias',configured_url=address,alias_url=target,
            receipt_id=alias['receipt_id'],config_hash=source_configs[ident])
        for proof in proofs:
            add_relation(page_node(target),source_id,'source_on_page',proof,
                observed_relation='validated_listing_alias',assertion=assertion)

    # School is the existing School row, not an invented Department parent.
    # These observations were executed on this pinned catalogue's original
    # material and still need the same current-config receipt at projection.
    for observation in school_publishers:
        ident = observation.get('source_id')
        school_name = site.get('name')
        assertion = observation.get('assertion') or {}
        source_id = department_nodes.get(ident)
        if (not school_id or not school_name or observation.get('school_id') != school_id
                or observation.get('school_name') != school_name
                or checked_address(observation.get('root_url')) != root_url
                or ident not in source_configs or not source_id
                or observation.get('config_hash') != source_configs[ident]
                or observation.get('receipt_id') not in receipt_ids[ident]
                or assertion.get('kind') != 'school_website_publication'
                or assertion.get('predicate') != 'publication_owner'
                or assertion.get('direction') != 'publisher_to_source'
                or assertion.get('publisher_name') != school_name
                or checked_address(assertion.get('entry_url')) != nodes[source_id]['url']):
            gap('school_publisher_assertion_unverified', source_id=ident)
            continue
        evidence = observation.get('evidence') or {}
        roles = ('homepage_identity', 'homepage_entry', 'column_identity')
        proofs = {role: web_evidence(evidence.get(role) or {}) for role in roles}
        if (not all(proofs.values()) or proofs['homepage_identity']['reference_url'] != root_url
                or proofs['homepage_entry']['reference_url'] != root_url
                or proofs['column_identity']['reference_url'] != nodes[source_id]['url']):
            gap('school_publisher_material_unavailable', source_id=ident)
            continue
        publisher = f'school:{school_id}'
        nodes[publisher] = dict(id=publisher, entity_type='organization', organization_kind='school',
            school_id=school_id, label=school_name, url=root_url,
            identity_basis='school_record_and_official_material', identity_record_id=f'schools:{school_id}')
        add_relation(publisher, page_node(root_url), 'entity_page', proofs['homepage_identity'],
            observed_relation='school_website_identity')
        for proof in proofs.values():
            add_relation(publisher, source_id, 'source_ownership', proof,
                observed_relation='school_website_publication', assertion=assertion)

    # Preview composition is an application scope plus an observed link, not
    # complete-column equivalence, publisher ownership or administrative ancestry.
    for observation in source_scopes:
        ident=observation.get('source_id');source=department_nodes.get(ident)
        if not source or ident not in source_configs or observation.get('config_hash')!=source_configs[ident]:continue
        scope_kind=observation.get('kind','configured_multiple_regions')
        if scope_kind not in ('configured_multiple_regions','configured_module_preview'):
            continue
        nodes[source]['scope_kind']=scope_kind
        nodes[source]['source_scope']=dict(kind=scope_kind,status=observation.get('status'),
            selected_items=observation.get('selected_items'),components=[{
                **{key:c[key] for key in ('name','preview_items','target_source_id','status','status_label','full_column_equivalent') if key in c},
                'column_url':checked_address(c.get('column_url'))} for c in observation.get('components',[])])
        for component in observation.get('components',[]):
            target_id=component.get('target_source_id');target=department_nodes.get(target_id)
            if component.get('status')!='resolved':continue
            if (not target or target_id not in source_configs or target==source
                    or component.get('target_config_hash')!=source_configs[target_id]
                    or component.get('receipt_id') not in receipt_ids[target_id]
                    or checked_address(component.get('column_url'))!=nodes[target]['url']
                    or component.get('full_column_equivalent') is not False
                    or component.get('matched_items')!=component.get('preview_items')
                    or not isinstance(component.get('matched_items'),int) or component['matched_items']<1):
                gap('preview_component_not_confirmed',source_id=ident,target_source_id=target_id);continue
            proofs=[(raw.get('role'),web_evidence(raw)) for raw in component.get('evidence',[])]
            required={'preview_heading','preview_entry','preview_container','column_heading','column_container','preview_item','column_item'}
            if (not required<={role for role,p in proofs if p} or not all(p for _,p in proofs)
                    or any(len({p['locator'] for r,p in proofs if r==role and p})!=component['matched_items'] for role in ('preview_item','column_item'))
                    or any(p['reference_url']!=(nodes[source]['url'] if role.startswith('preview_') else nodes[target]['url']) for role,p in proofs)):
                gap('preview_component_material_unavailable',source_id=ident,target_source_id=target_id);continue
            assertion=dict(kind='configured_column_preview',direction='configured_source_to_column',
                coverage='observed_preview_items',full_column_equivalent=False,
                preview_items=component['preview_items'],target_config_hash=component['target_config_hash'],
                receipt_id=component['receipt_id'])
            if component.get('tab_binding_evidence'):assertion['tab_binding_evidence']=component['tab_binding_evidence']
            for _,proof in proofs:add_relation(source,target,'source_preview',proof,
                observed_relation='configured_column_preview',assertion=assertion)

    # Navigation direction comes from the actual source document, never from a
    # structural node's DOM parent or a previously calculated ownership path.
    for edge in _rows(navigation_edges):
        source_url, target_url = checked_address(edge.get('parent_url')), checked_address(edge.get('target_url'))
        if not source_url or not target_url:
            gap('invalid_navigation_endpoint', reference_url=source_url, target_url=target_url)
            continue
        proof = web_evidence(edge, reference=source_url)
        if proof:
            add_relation(page_node(source_url), page_node(target_url), 'navigation_link', proof)

    expanded_entries = []
    for entry in _rows(directory_entries):
        facts = entry.get('facts_json') or []
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except (TypeError, ValueError):
                facts = []
        expanded_entries.extend([dict(entry, **fact) for fact in facts if isinstance(fact, dict)]
                                if facts else [entry])
    bindings = defaultdict(set)
    for entry in expanded_entries:
        child = department_nodes.get(entry.get('department_id'))
        if not child or nodes[child]['entity_type'] == 'source' or not entry.get('node_key'):
            continue
        for evidence in entry.get('evidence', []):
            proof = web_evidence(evidence)
            if proof:
                bindings[(proof['reference_url'], entry['node_key'])].add(child)
    from backend.services.publisher_identity import DECLARATION
    declared_units, verified_declarations = set(), {}
    for ident, row in department_rows.items():
        identity = _object(row.get('identity_json'))
        if identity.get('kind') != DECLARATION:
            continue
        node_id = department_nodes[ident]
        declared_units.add(node_id)
        proofs = [proof for item in identity.get('evidence', []) if (proof := web_evidence(item))]
        locations = {(p['reference_url'], p['locator']) for p in proofs}
        home = checked_address(identity.get('website_url'))
        required = {(home, identity.get('locator')),
            (checked_address(identity.get('profile_url')), identity.get('profile_locator')),
            (checked_address(identity.get('entrance_reference_url')), identity.get('entrance_locator'))}
        statement = identity.get('statement') or {}
        if 'profile_route' in statement:
            from backend.services.publisher_identity import checked_profile_route
            route = checked_profile_route(home, checked_address(identity.get('profile_url')),
                statement.get('profile_entry_locator'), statement['profile_route'], page_rows, root_url)
            if not route:
                gap('publisher_profile_route_unverified', department_id=ident)
                continue
            required.update((hop['reference_url'], hop['locator']) for hop in route)
            required.add((checked_address(identity.get('profile_url')), statement.get('profile_home_locator')))
        if 'profile_home_witness'in statement:
            from types import SimpleNamespace
            from .profile_home_reference import verify_home_return
            returned=verify_home_return(home,checked_address(identity.get('profile_url')),
                statement.get('profile_home_locator'),statement['profile_home_witness'],row.get('name'),
                SimpleNamespace(pages=page_rows,site=site))
            if not returned:
                gap('publisher_profile_home_unverified',department_id=ident)
                continue
            required.update((proof['reference_url'],proof['locator'])for proof in returned)
        declaration = next((record for record in _rows(structure_nodes)
            if record.get('relation') == DECLARATION and record.get('node_key') == identity.get('node_key')
            and record.get('name') == row.get('name') and checked_address(record.get('reference_url')) == home
            and record.get('locator') == identity.get('locator') and web_evidence(record)), None)
        if (row.get('kind') != 'unit' or identity.get('writer') != 'official_structure'
                or identity.get('schema') != 1 or identity.get('name') != row.get('name')
                or not home or home != checked_address(row.get('list_url')) or not declaration
                or not required <= locations or len(proofs) != len(identity.get('evidence', []))
                or identity.get('administrative_parent_asserted') is not False):
            gap('publisher_identity_unverified', department_id=ident)
            continue
        bindings[(home, identity['node_key'])].add(node_id)
        verified_declarations[node_id] = dict(identity, evidence=proofs)
        nodes[node_id]['identity_basis'] = 'website_publisher_declaration'
        nodes[node_id]['administrative_parent_status'] = 'unknown'
    observations, observations_by_key = {}, defaultdict(list)
    for record in _rows(structure_nodes):
        reference = checked_address(record.get('reference_url'))
        proof = web_evidence(record)
        if not proof:
            continue
        record_key = (reference, record.get('node_key'), record.get('locator'), record.get('parent_key'))
        explicit_id = record.get('department_id')
        matching = structure_keys.get(record.get('node_key'), [])
        saved_bindings = bindings.get((reference, record.get('node_key')), set())
        bound = department_nodes.get(explicit_id) if explicit_id is not None else (
            next(iter(saved_bindings)) if len(saved_bindings) == 1 else matching[0] if len(matching) == 1 else None)
        if bound and nodes[bound]['entity_type'] == 'source':
            bound = None  # A page/roster node cannot silently become a widget.
        if explicit_id is not None and not bound:
            gap('unresolved_entity_binding', department_id=explicit_id, reference_url=reference)
        if record.get('relation') == 'page_identity':
            node_id = page_node(reference)
        elif bound:
            node_id = bound
            if checked_address(record.get('url')):
                identity = verified_declarations.get(node_id)
                if record.get('relation') == DECLARATION and identity:
                    for witness in identity['evidence']:
                        add_relation(node_id, page_node(record['url']), 'entity_page', witness,
                            observed_relation=DECLARATION,
                            assertion={key: value for key, value in identity.items() if key not in ('statement', 'evidence')})
                else:
                    add_relation(node_id, page_node(record['url']), 'entity_page', proof)
        else:
            node_id = 'observation:' + key + ':' + _digest(record_key)[:24]
            entity_type = 'directory_group' if record.get('relation') in GROUP_RELATIONS else 'structure_observation'
            nodes[node_id] = dict(id=node_id, entity_type=entity_type, label=record.get('name') or '',
                observed_kind=record.get('kind'), observed_relation=record.get('relation'),
                reference_url=reference, url=checked_address(record.get('url')), identity_status='unresolved')
        observations[record_key] = (record, node_id, proof)
        observations_by_key[(reference, record.get('node_key'))].append(node_id)

    for record, target, proof in observations.values():
        observed_type = record.get('relation')
        if (observed_type in {'menu_group', 'navigation_entry', 'unit_channel',
                              'publication_column', 'website_entrance', 'directory_entry',
                              'hidden_directory_entry', 'nested_directory_entry'}
                and checked_address(record.get('url'))):
            add_relation(page_node(proof['reference_url']), page_node(record['url']),
                         'navigation_link', proof)
        if observed_type not in DIRECTORY_RELATIONS | ORGANIZATION_RELATIONS:
            continue
        parents = set(observations_by_key.get((proof['reference_url'], record.get('parent_key')), []))
        if not record.get('parent_key'):
            parents = {page_node(proof['reference_url'])}
        if len(parents) != 1:
            gap('missing_or_ambiguous_structural_parent', node_id=target, reference_url=proof['reference_url'])
            continue
        source = next(iter(parents))
        if observed_type in ORGANIZATION_RELATIONS:
            if not all(nodes[n]['entity_type'] == 'organization' for n in (source, target)):
                gap('organization_identity_not_bound', source=source, target=target,
                    observed_relation=observed_type, evidence=proof)
                continue
            # A parser's enum or DOM nesting is an observation, not a verified
            # administrative claim. The typed membership pass below requires
            # a material-checked assertion with explicit direction.
            gap('organization_assertion_unverified', source=source, target=target, evidence=proof)
            relation_type = 'directory_listing'
        else:
            relation_type = 'directory_listing'
        add_relation(source, target, relation_type, proof, observed_relation=observed_type)

    def valid_website_binding(assertion, unit, evidence):
        locations = {(p['reference_url'], p['locator']) for p in evidence}
        directory = checked_address(assertion.get('directory_reference_url'))
        website = checked_address(assertion.get('website_url'))
        roster_ids = set(observations_by_key.get((directory, assertion.get('unit_key')), []))
        return (unit and nodes[unit]['entity_type'] == 'organization'
            and assertion.get('kind') == 'explicit_directory_website'
            and assertion.get('direction') == 'organization_to_page'
            and assertion.get('unit_name') == nodes[unit]['label']
            and roster_ids == {unit}
            and any(record.get('node_key') == assertion.get('unit_key') and
                    proof['reference_url'] == directory and target == unit and
                    proof['locator'] == assertion.get('unit_locator')
                    for record, target, proof in observations.values())
            and bool(assertion.get('identity_text')) and bool(assertion.get('unit_key'))
            and bool(website) and website == checked_address(assertion.get('identity_reference_url'))
            and (directory, assertion.get('unit_locator')) in locations
            and (directory, assertion.get('entry_locator')) in locations
            and (website, assertion.get('identity_locator')) in locations)

    # Website bindings are saved with the roster occurrence, independently of
    # any source receipt. They bind the existing organization to another page;
    # the membership row's administrative direction is not reused here.
    verified_websites = set()
    for entry in expanded_entries:
        if (entry.get('relation_type') or entry.get('type')) != 'entity_page':
            continue
        unit = department_nodes.get(entry.get('department_id'))
        assertion = entry.get('assertion') or {}
        proofs = [proof for item in entry.get('evidence', []) if (proof := web_evidence(item))]
        if (entry.get('writer') != 'official_structure' or
                entry.get('observed_relation') != 'verified_directory_website' or
                checked_address(entry.get('page_url')) != checked_address(assertion.get('website_url')) or
                not valid_website_binding(assertion, unit, proofs)):
            gap('website_binding_assertion_unverified', department_id=entry.get('department_id'))
            continue
        verified_websites.add((unit, _json(assertion)))
        for proof in proofs:
            add_relation(unit, page_node(assertion['website_url']), 'entity_page', proof,
                observed_relation='verified_directory_website', assertion=assertion)

    # Old table rows carry no provenance. They remain visible as gaps; they do
    # not become authoritative simply because both endpoint IDs are known.
    for entry in expanded_entries:
        parent, child = entry.get('parent_id'), entry.get('department_id')
        source, target = department_nodes.get(parent), department_nodes.get(child)
        relation_type = entry.get('relation_type') or entry.get('type')
        if relation_type == 'entity_page':
            continue
        evidence = entry.get('evidence') or []
        if isinstance(evidence, dict):
            evidence = [evidence]
        allowed = {'directory_listing', 'organization_membership', 'source_ownership', 'cross_entity_reference'}
        if relation_type not in allowed or not evidence:
            gap('legacy_membership_without_evidence', parent_id=parent, department_id=child)
            continue
        if not source or not target:
            gap('missing_membership_endpoint', parent_id=parent, department_id=child)
            continue
        if relation_type == 'source_ownership' and source in declared_units:
            identity = verified_declarations.get(source)
            if not identity or (entry.get('assertion') or {}).get('publisher_declaration_key') != identity['node_key']:
                gap('source_publisher_identity_unverified', source=source, target=target)
                continue
        if relation_type == 'organization_membership' and not all(
                nodes[n]['entity_type'] == 'organization' for n in (source, target)):
            gap('invalid_organization_endpoints', parent_id=parent, department_id=child)
            continue
        if relation_type == 'organization_membership' and entry.get('observed_relation') not in ORGANIZATION_RELATIONS:
            gap('organization_claim_not_explicit', parent_id=parent, department_id=child)
            continue
        if relation_type == 'organization_membership':
            assertion = entry.get('assertion') or {}
            expected = 'attached_unit' if entry.get('observed_relation') == 'attached_unit' else 'subordinate_unit'
            if (assertion.get('kind') != 'explicit_table_header' or assertion.get('predicate') != expected
                    or assertion.get('direction') != 'parent_to_child'
                    or not all(assertion.get(key) for key in ('text', 'locator', 'parent_locator', 'child_locator'))
                    or assertion['locator'] not in {item.get('locator') for item in evidence}):
                gap('organization_assertion_unverified', parent_id=parent, department_id=child)
                continue
        if relation_type == 'source_ownership' and (
                nodes[source]['entity_type'] != 'organization' or nodes[target]['entity_type'] != 'source'):
            gap('invalid_ownership_endpoints', parent_id=parent, department_id=child)
            continue
        if relation_type == 'source_ownership' and entry.get('writer') == 'official_structure' and (
                entry.get('config_hash') != source_configs.get(child)
                or entry.get('receipt_id') not in receipt_ids[child]):
            gap('ownership_receipt_not_current', parent_id=parent, department_id=child)
            continue
        checked_evidence = [proof for item in evidence if (proof := web_evidence(item))]
        assertion = entry.get('assertion') or {}
        if relation_type == 'organization_membership':
            material_locations = defaultdict(set)
            for proof in checked_evidence:
                material_locations[(proof['reference_url'], proof['content_hash'])].add(proof['locator'])
            # A current child locator cannot rescue an expired table header.
            # The assertion was verified against a single source document.
            if not any({assertion['locator'], assertion['child_locator']} <= locations
                       for locations in material_locations.values()):
                gap('organization_assertion_unverified', parent_id=parent, department_id=child)
                continue
        if relation_type == 'source_ownership':
            locations = {(proof['reference_url'], proof['locator']) for proof in checked_evidence}
            if (assertion.get('kind') != 'explicit_publisher_identity'
                    or assertion.get('predicate') != 'publication_owner'
                    or assertion.get('direction') != 'publisher_to_source'
                    or not assertion.get('identity_text')
                    or assertion.get('entry_kind') not in ('column_link', 'publication_region')
                    or checked_address(assertion.get('entry_url')) != nodes[target]['url']
                    or (checked_address(assertion.get('identity_reference_url')), assertion.get('identity_locator')) not in locations
                    or (checked_address(assertion.get('entry_reference_url')), assertion.get('entry_locator')) not in locations):
                gap('publisher_identity_assertion_unverified', parent_id=parent, department_id=child)
                continue
            binding = assertion.get('website_binding')
            if binding and ((source, _json(binding)) not in verified_websites or
                    not valid_website_binding(binding, source, checked_evidence)):
                gap('website_binding_assertion_unverified', department_id=parent)
                continue
            witness = assertion.get('profile_identity_witness') or {}
            if 'profile_route' in witness:
                from backend.services.publisher_identity import checked_profile_route
                profile = checked_address(witness.get('reference_url'))
                route = checked_profile_route(checked_address(witness.get('website_url')), profile,
                    witness.get('profile_entry_locator'), witness['profile_route'], page_rows, root_url)
                required = {(profile, witness.get('locator')), (profile, witness.get('profile_home_locator'))}
                if route:
                    required.update((hop['reference_url'], hop['locator']) for hop in route)
                if (not route or witness.get('unit_name') != nodes[source]['label'] or not required <= locations):
                    gap('publisher_profile_route_unverified', parent_id=parent, department_id=child)
                    continue
            if 'profile_home_witness'in witness:
                from types import SimpleNamespace
                from .profile_home_reference import verify_home_return
                returned=verify_home_return(checked_address(witness.get('website_url')),
                    checked_address(witness.get('reference_url')),witness.get('profile_home_locator'),
                    witness['profile_home_witness'],nodes[source]['label'],SimpleNamespace(pages=page_rows,site=site))
                if not returned or not {(p['reference_url'],p['locator'])for p in returned}<=locations:
                    gap('publisher_profile_home_unverified',parent_id=parent,department_id=child)
                    continue
        for proof in checked_evidence:
            add_relation(source, target, relation_type, proof, observed_relation=entry.get('observed_relation'),
                         assertion=assertion)

    verified_organization_materials = {
        (relation['source'], relation['target'], proof['reference_url'], proof['content_hash'])
        for relation in relations.values() if relation['type'] == 'organization_membership'
        for proof in relation['evidence']
    }
    # A verified typed assertion resolves only its corresponding raw parser
    # claim. Other documents, endpoint pairs and invalid typed claims remain
    # unknown; directory listing remains an independently useful fact.
    for ident, item in list(gaps.items()):
        proof = item.get('evidence') or {}
        if (item['code'] == 'organization_assertion_unverified'
                and (item.get('source'), item.get('target'), proof.get('reference_url'),
                     proof.get('content_hash')) in verified_organization_materials):
            del gaps[ident]

    from backend.services.source_access import project_access
    bound_pages = defaultdict(set)
    for relation in relations.values():
        if relation['type'] == 'source_on_page':
            bound_pages[relation['target']].add(nodes[relation['source']]['url'])
    for ident, node_id in department_nodes.items():
        node = nodes[node_id]
        if node['entity_type'] == 'source':
            node.update(project_access(node['url'] if node['configured'] else '', access_candidates[ident],
                bound_pages[node_id], normalize=checked_address))
    for relation in relations.values():
        relation['evidence'].sort(key=_json)
        if relation.get('assertions'):
            relation['assertions'].sort(key=_json)
    return dict(schema_version=1, structure_revision=None, sync_contract='unversioned',
        site_key=key, nodes=sorted(nodes.values(), key=lambda row: row['id']),
        relations=sorted(relations.values(), key=lambda row: row['id']),
        gaps=[gaps[key] for key in sorted(gaps)])
