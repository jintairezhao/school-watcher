"""One read adapter for saved source configuration and published website facts.

Projection and transport caches are disposable views, never a second fact store.
No crawler, model call, source installation or personal-state write happens here.
"""
from collections import Counter, OrderedDict, defaultdict, deque
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.database.source_governance_models import SourceValidation, SourceConfigVersion
from backend.services.source_inventory import canonical_url, site_key
from backend.services.structure_facts import build_structure_facts, _url as public_url

MEMBERSHIP = {'directory_listing', 'organization_membership', 'source_ownership', 'column_membership'}
STRUCTURE_PROJECTION_VERSION = 16
SOURCE_GROUP_LABELS = frozenset({'有依据的栏目归属', '栏目（归属待核实）', '历史通知范围'})
_transport = OrderedDict()
_transport_lock = threading.Lock()


class StructureReadConflict(RuntimeError):
    """A capture crossed repeated committed updates; request a fresh read."""


def _row(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def department_records(capture):
    # ORM objects may expire after a later read-state commit. The captured
    # source rules must remain at the structure version already returned.
    if 'department_records' in capture:
        return capture['department_records']
    return [_row(row) for row in capture['departments']]


def department_views(capture):
    return [SimpleNamespace(**deepcopy(row)) for row in department_records(capture)]


def read_structure(school, *, include_catalog_report=False):
    """Pin a catalogue and reject a read crossing a committed structure version.

    SQL reads share the caller's transaction. The version check additionally
    handles READ COMMITTED databases; a concurrent mutation retries the entire
    projection. It never combines a fresh pointer with cached ORM identities.
    """
    from backend.services.runtime_catalog import published_catalog
    from backend.services.structure_versions import current_version
    for _ in range(3):
        before = current_version(school.id)
        db.session.refresh(school)
        key = site_key(school.url)
        catalog = published_catalog()
        pinned = catalog.pin(key)
        reader = pinned or catalog
        report = reader.monitor_snapshot(key) or {}
        # The provenance page needs raw parser observations as well as the
        # validated projection. Read both from this pinned generation and retry
        # them together if the business structure changes during the capture.
        extra = (dict(catalog_report=reader.report(key), raw_structure_nodes=reader.structure(key))
                 if include_catalog_report else {})
        departments = Department.query.filter_by(school_id=school.id).populate_existing().order_by(Department.id).all()
        records = [deepcopy(_row(row)) for row in departments]
        ids = [row.id for row in departments]
        entries = DepartmentDirectoryEntry.query.filter(
            DepartmentDirectoryEntry.parent_id.in_(ids),
            DepartmentDirectoryEntry.department_id.in_(ids)).populate_existing().all() if ids else []
        validations = SourceValidation.query.filter_by(school_id=school.id).populate_existing().all()
        latest_versions = db.session.query(db.func.max(SourceConfigVersion.id)).filter(
            SourceConfigVersion.department_id.in_(ids)).group_by(SourceConfigVersion.department_id)
        configurations = (SourceConfigVersion.query.filter(SourceConfigVersion.id.in_(latest_versions))
            .populate_existing().order_by(SourceConfigVersion.id).all()) if ids else []
        configuration_rows = [deepcopy(dict(_row(row), school_id=school.id)) for row in configurations]
        from backend.services.execution_observations import capture_ledger
        ledger_rows=capture_ledger(report.get('exploration_events',[]),configuration_rows,school_id=school.id)
        from backend.services.source_lifecycle import states_for
        from backend.services.source_path_receipts import load_source_receipts
        lifecycle = states_for(ids)
        receipts = load_source_receipts({'name': school.name, 'root_url': school.url})
        from backend.services.school_publishers import observe_school_publishers
        school_site = dict(id=school.id, name=school.name, root_url=school.url, site_key=key)
        material_cache={}
        def local_material(url):
            if url not in material_cache:material_cache[url]=reader.snapshot(key,url)
            return material_cache[url]
        school_publishers = observe_school_publishers(site=school_site, pages=report.get('pages', []),
            departments=records, receipts=receipts, material=local_material)
        from backend.services.source_scope_projection import load_joint_scope_observations, resolve_scope_components
        joint_scopes=load_joint_scope_observations(school.id,records,report.get('pages',[]),local_material)
        source_scopes=resolve_scope_components(scopes=joint_scopes,pages=report.get('pages',[]),
            departments=records,receipts=receipts,material=local_material)
        from backend.services.source_relationships import observe_source_page_aliases
        source_page_aliases=observe_source_page_aliases(site=school_site,departments=records,receipts=receipts,
            metadata=lambda urls:reader.page_metadata(key,urls,include_listing_data=True),material=local_material,
            known_pages=report.get('pages',[]))
        from backend.services.discovery_policy import get_attention_policy
        attention = deepcopy(get_attention_policy(school.id))
        from backend.services.source_attention import policy_snapshot
        attention_version=policy_snapshot(attention)['version']
        from backend.services.collection_access import read_entry_observations
        entry_observations = read_entry_observations(school.id)
        facts = build_structure_facts(site=school_site,
            pages=report.get('pages', []), structure_nodes=report.get('structure_nodes', []),
            navigation_edges=report.get('navigation_edges', []), departments=[dict(row, lifecycle=lifecycle.get(row['id'], 'active')) for row in records],
            directory_entries=[_row(row) for row in entries], validations=[_row(row) for row in validations],
            receipts=receipts, school_publishers=school_publishers, source_scopes=source_scopes,
            source_page_aliases=source_page_aliases, entry_observations=entry_observations)
        after = current_version(school.id)
        if before == after:
            facts.update(structure_revision=after['revision'], structure_epoch=after['epoch'],
                         sync_contract='transaction_version', projection_version=STRUCTURE_PROJECTION_VERSION)
            return dict(facts=facts, report=report, departments=departments, version=after,
                        school_id=school.id,
                        department_records=records,
                        directory_entries=[SimpleNamespace(**deepcopy(_row(row))) for row in entries],
                        source_configurations=configuration_rows,
                        model_executions=ledger_rows,
                        attention_policy=attention, attention_policy_version=attention_version, **extra)
    raise StructureReadConflict('Structure changed during reading; retry the snapshot')


def connected_source_ids(facts):
    """Stable source node IDs eligible for the default connected view."""
    return {node['id'] for node in facts['nodes'] if node['entity_type'] == 'source'
        and node.get('configured') and node.get('verification') == 'verified'
        and node.get('lifecycle') not in ('disabled', 'removed')}


def graph_projection(capture, school, *, scope='connected'):
    """Select visible facts; layout and grouping cannot add relationships."""
    facts, report = capture['facts'], capture['report']
    from backend.services.source_attention import policy_snapshot, material_attention, source_attention
    from backend.services.discovery_policy import get_attention_policy
    attention_policy=capture.get('attention_policy') or get_attention_policy(school.id)
    attention_version=policy_snapshot(attention_policy)['version']
    source_configs={row['id']:row for row in department_records(capture)}
    original = {node['id']: node for node in facts['nodes']}
    relations = facts['relations']
    pages = {url:row for row in report.get('pages', []) if (url:=public_url(row['url']))}
    root_url = canonical_url(school.url)
    root_id = next((n['id'] for n in original.values() if n['entity_type'] == 'page' and n['url'] == root_url), None)
    connected = connected_source_ids(facts)
    selected = set(connected)
    # A recorded homepage union is still the subscriber's configured scope.
    # Keep it visible without granting the separate complete columns' receipts.
    selected.update(node['id'] for node in facts['nodes']
        if node['entity_type'] == 'source' and node.get('configured')
        and node.get('scope_kind') in ('configured_multiple_regions', 'configured_module_preview')
        and node.get('lifecycle') not in ('disabled', 'removed'))
    incoming = defaultdict(list)
    for edge in relations:
        incoming[edge['target']].append(edge)
    # All evidenced owners survive. A layout will not choose a unique parent.
    queue = deque(sorted(selected))
    while queue:
        ident = queue.popleft()
        for edge in incoming[ident]:
            if edge['type'] in MEMBERSHIP | {'source_on_page'} and edge['source'] not in selected:
                selected.add(edge['source']); queue.append(edge['source'])
    selected.update(edge['target'] for edge in relations if edge['type'] == 'entity_page' and edge['source'] in selected)
    if root_id:
        selected.add(root_id)
    # Direct access routes first. Keep all equal shortest entrances, without
    # growing backwards through repeated headers or unrelated link cycles.
    outgoing = defaultdict(list)
    for edge in relations:
        if edge['type'] == 'navigation_link':
            outgoing[edge['source']].append(edge['target'])
    distance, predecessors = ({root_id: 0} if root_id else {}), defaultdict(set)
    queue = deque([root_id] if root_id else [])
    while queue:
        ident = queue.popleft()
        for target in sorted(outgoing[ident]):
            next_distance = distance[ident] + 1
            if target not in distance:
                distance[target] = next_distance; queue.append(target)
            if distance[target] == next_distance:
                predecessors[target].add(ident)
    queue = deque(sorted(selected)); expanded = set()
    while queue:
        ident = queue.popleft()
        if ident in expanded:
            continue
        expanded.add(ident)
        for parent in predecessors[ident]:
            selected.add(parent); queue.append(parent)
    if scope == 'exploration':
        # The explicit exploration view shows recorded candidates, still with
        # real identity and provenance. It has no influence on installed rules.
        selected.update(n['id'] for n in original.values() if n['entity_type'] == 'page')
        selected.update(edge['source'] for edge in relations if edge['type'] in MEMBERSHIP)
        selected.update(edge['target'] for edge in relations if edge['type'] in MEMBERSHIP)
        selected.update(n['id'] for n in original.values() if n['entity_type'] == 'source')
    selected &= original.keys()
    nodes = []
    kind_labels = {'root': '学校官网', 'unit': '组织实体', 'directory': '目录导航',
                   'channel': '信息栏目', 'navigation': '官网页面'}
    state_labels = {'connected': '已接入', 'pending': '待验证', 'observed': '已发现入口',
                    'fetched': '已读取', 'failed': '等待恢复', 'blocked': '访问受阻',
                    'running': '正在处理', 'waiting': '等待继续', 'reference_only': '已发现入口',
                    'disabled': '已停用', 'removed': '已移除'}
    for ident in sorted(selected):
        fact = original[ident]
        url = fact.get('url') or fact.get('reference_url') or ''
        page = pages.get(url, {})
        entity = fact['entity_type']
        kind = ('channel' if entity == 'source' else 'unit' if entity == 'organization' else
                'directory' if entity in ('directory_group', 'structure_observation') else
                'root' if ident == root_id else page.get('kind', 'navigation'))
        state = ('connected' if ident in connected else 'pending' if entity == 'source' else
                 'failed' if page.get('state') == 'failed' else page.get('state', 'observed'))
        if fact.get('lifecycle') in ('disabled', 'removed'):
            state = fact['lifecycle']
        sources = ([dict(id=fact['source_id'], name=fact['label'], group=fact.get('display_group', ''))]
                   if entity == 'source' and fact.get('configured') else [])
        node = dict(fact, url=url, kind=kind, kind_label=kind_labels.get(kind, '官网页面'),
            state=state, state_label=state_labels.get(state, '已发现入口'), sources=sources,
            label_source='recorded' if fact.get('label') and fact.get('label') != url else 'url',
            path=[], navigation_path=[], tasks=[], categories=[], cost=None,
            checked_at=fact.get('access_observed_at') if entity == 'source' else page.get('checked_at'),
            status_code=(fact.get('access_observation') or {}).get('status_code') if entity == 'source' else page.get('status_code'),
            reason=('历史通知、订阅和个人状态仍保留' if state in ('disabled', 'removed') else
                    '栏目配置已接入，访问情况见记录' if state == 'connected' else
                    '已保存的栏目配置' if sources else '已记录官网入口'),
            path_basis='按关系类型查看有证据的路径', scope_basis='saved_structure_fact',
            structure_priority=0 if ident == root_id else 1 if entity == 'source' else 2)
        if entity == 'page':
            node['kind_label'] = ('学校官网' if ident == root_id else '院系网页' if kind == 'unit' else
                                  '栏目页面' if kind == 'channel' else '目录页面' if kind == 'directory' else '官网页面')
        elif entity == 'structure_observation':
            node['kind_label'] = '结构观察（身份待确认）'
        elif fact.get('organization_kind') == 'school':
            node['kind_label'] = '学校'
        if fact.get('scope_kind') in ('configured_multiple_regions','configured_module_preview'):
            node['kind_label']='主页预览范围' if fact['scope_kind']=='configured_module_preview' else '组合采集范围'
            node['reason']='保留原订阅；页面预览与完整栏目分别记录。'
        nodes.append(node)
        interest=(source_attention(fact,source_configs.get(fact.get('source_id'),{}),report,attention_policy,pages_by_url=pages)
                  if entity=='source' else material_attention(dict(page,url=url,label=fact['label']),attention_policy))
        node.update(categories=interest['needs'],attention_score=interest['score'],
                    attention_policy_version=attention_version,attention_interest_only=True)
    links = []
    for relation in relations:
        if relation['source'] not in selected or relation['target'] not in selected:
            continue
        proof = relation['evidence'][0]
        links.append(dict(relation, access=relation['type'] == 'navigation_link',
            evidence_url=proof.get('reference_url'), locator=proof.get('locator'),
            content_hash=proof.get('content_hash'), observed_at=proof.get('observed_at')))
    gaps = Counter(gap['code'] for gap in facts['gaps'])
    from backend.services.source_execution_events import merge_execution_events
    events, execution_gaps = merge_execution_events(report.get('exploration_events', []),
        capture.get('source_configurations', []), facts)
    from backend.services.execution_observations import project_ledger
    usage=project_ledger(events,capture.get('model_executions',[]))
    event_ids = {event['id'] for event in events}
    history = dict(report.get('exploration_history') or {'history_before_recording': 'unknown'},
        **execution_gaps, projected_events=len(events), omitted_events=execution_gaps['omitted_page_events'],
        missing_projected_parent_ids=sorted({event['parent_event_id'] for event in events
            if event.get('parent_event_id') and event['parent_event_id'] not in event_ids}))
    history['model_usage']=usage
    page_producers = history.get('unrecorded_producers', [])
    history['unrecorded_page_event_producers'] = list(page_producers)
    history['unrecorded_producers'] = [value for value in page_producers if value != 'source_activation']
    history['unrecorded_producers'].append('legacy_source_configuration_saves')
    from backend.scraper.discovery.attention import CATEGORY_DEFINITIONS
    directions=[dict(id=key,label=value['label'],
        observed=sum(node['entity_type']=='page' and key in node['categories'] for node in nodes),
        connected=sum(node['entity_type']=='source' and node['state']=='connected' and key in node['categories'] for node in nodes))
        for key,value in CATEGORY_DEFINITIONS.items()]
    return dict(nodes=nodes, relations=links, structure_version=capture['version'],
        attention_policy_version=attention_version,directions=directions,profiles=list(attention_policy['profiles']),
        profile_context=attention_policy.get('profile_context',{}),
        structure_schema_version=facts['schema_version'], exploration_events=events,
        structure_projection_version=STRUCTURE_PROJECTION_VERSION,
        exploration_history=history,
        structure_gaps=dict(counts=dict(gaps), total=sum(gaps.values()),
                            exploration_history='partially_recorded' if events else 'not_recorded'),
        structure_scope=dict(mode=scope, total_nodes=len(original), shown_nodes=len(nodes)),
        source_count=sum(node['state'] == 'connected' and node['entity_type'] == 'source' for node in nodes))


def transport_update(graph, *, school_id, since=None):
    """Bounded exact-view deltas; an absent base always returns a full snapshot."""
    payload = {key: graph[key] for key in ('nodes', 'relations', 'structure_version', 'structure_scope', 'exploration_events', 'exploration_history')}
    payload['attention_policy_version']=graph.get('attention_policy_version')
    payload['structure_projection_version']=graph.get('structure_projection_version')
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), default=str).encode()).hexdigest()
    key = (school_id, graph['structure_scope']['mode'], fingerprint)
    with _transport_lock:
        base = _transport.get((school_id, graph['structure_scope']['mode'], since)) if since else None
        _transport[key] = deepcopy(payload)
        _transport.move_to_end(key)
        while len(_transport) > 32:
            _transport.popitem(last=False)
    result = dict(graph, structure_token=fingerprint, update_mode='snapshot')
    if (base and base['structure_version']['epoch'] == graph['structure_version']['epoch']
            and base.get('structure_projection_version') == graph.get('structure_projection_version')):
        # The exact prior representation is known. A gap in the durable journal
        # still requires a snapshot, even when a process cache happens to survive.
        from backend.services.structure_versions import changes_since
        check = changes_since(school_id, base['structure_version']['epoch'], base['structure_version']['revision'])
        if not check['reset_required']:
            delta = {}
            for field in ('nodes', 'relations'):
                previous = {item['id']: item for item in base[field]}
                current = {item['id']: item for item in graph[field]}
                delta[field] = dict(upsert=[item for ident, item in current.items() if previous.get(ident) != item],
                                    remove=sorted(previous.keys() - current.keys()))
            result.update(update_mode='delta', base_token=since, delta=delta)
            result.pop('nodes'); result.pop('relations')
    return result


def source_choices(capture, *, retained_ids=(), allowed_ids=None):
    """Template adapter. Display groups are explicitly separate from ownership."""
    facts = capture['facts']
    by_id = {node['id']: node for node in facts['nodes']}
    records = {row['id']:row for row in department_records(capture)}
    departments = {ident:SimpleNamespace(**deepcopy(row)) for ident,row in records.items()}
    owners = defaultdict(list)
    for edge in facts['relations']:
        if edge['type'] == 'source_ownership':
            owners[edge['target']].append(edge['source'])
    groups = defaultdict(list)
    owner_groups = {}
    retained = set(retained_ids)
    allowed = None if allowed_ids is None else set(allowed_ids)
    from backend.services.source_attention import policy_snapshot, source_attention
    from backend.services.discovery_policy import get_attention_policy
    policy=capture.get('attention_policy')
    if policy is None:
        school_id=next((row['school_id'] for row in records.values()),None)
        policy=get_attention_policy(school_id)
    attention_version=policy_snapshot(policy)['version']
    page_index={url:page for page in capture['report'].get('pages', []) if (url:=public_url(page['url']))}
    for node in facts['nodes']:
        if node['entity_type'] != 'source' or not (node.get('configured') or node['source_id'] in retained):
            continue
        if allowed is not None and node['source_id'] not in allowed:
            continue
        row = departments[node['source_id']]
        column = dict(department=row, label=node['label'], url=node.get('url') or '', lifecycle=node.get('lifecycle', 'active'),
            display_group=node.get('display_group') or '',scope_kind=node.get('scope_kind'),source_scope=node.get('source_scope'))
        column.update({key: deepcopy(node.get(key)) for key in
            ('access', 'access_scope', 'access_observed_at', 'access_observation', 'access_observations')})
        interest=source_attention(node,records[row.id],capture['report'],policy,pages_by_url=page_index)
        column.update(attention_score=interest['score'],attention_policy_version=attention_version)
        if owners[node['id']]:
            for owner in sorted(set(owners[node['id']])):
                parent = by_id[owner]
                if owner not in owner_groups:
                    owner_groups[owner] = dict(key=owner, name=parent['label'], own_columns=[],
                        columns=[], children=[], expandable=True)
                    groups['有依据的栏目归属'].append(owner_groups[owner])
                owner_groups[owner]['own_columns'].append(column)
                owner_groups[owner]['columns'].append(column)
        else:
            # group_name remains a user-facing display group, never a parent.
            groups['栏目（归属待核实）'].append(dict(key=node['id'], name=node.get('display_group') or '',
                own_columns=[column], columns=[column], children=[], expandable=False))
    for entries in groups.values():
        for entry in entries:
            for field in ('own_columns','columns'):
                entry[field].sort(key=lambda column:(-column['attention_score'],column['department'].id))
        entries.sort(key=lambda entry:(-max((column['attention_score'] for column in entry['columns']),default=0),
            min((column['department'].id for column in entry['columns']),default=0)))
    return dict(groups)


def inbox_choices(capture, *, allowed_ids, historical_ids=(), retained_ids=()):
    """Reader view of the same facts, with explicit historical record scopes."""
    allowed, history = set(allowed_ids), set(historical_ids)
    groups = source_choices(capture, allowed_ids=allowed, retained_ids=history | set(retained_ids))
    for units in groups.values():
        for unit in units:
            for column in unit['own_columns']:
                row = column['department']
                column['historical_scope'] = row.id in history and not bool(row.list_selector)
                column['unavailable'] = not column['url'] and row.id not in history
    # Old articles can reference a row later identified as an organization.
    # Preserve that reading scope without manufacturing a current source fact.
    for record in department_records(capture):
        ident = record['id']
        if record.get('kind', 'column') == 'column' or ident not in history or ident not in allowed:
            continue
        column = dict(department=SimpleNamespace(**deepcopy(record)), label=record['name'],
            url=public_url(record.get('list_url')), unavailable=False, historical_scope=True,
            display_group=record.get('group_name') or '', record_scope='historical_department_reference')
        groups.setdefault('历史通知范围', []).append(dict(key=f'historical:{ident}', name='',
            expandable=False, own_columns=[column], columns=[column], children=[]))
    return groups


def preview_source_choices(capture, school, *, historical_ids=()):
    """Current public choices, plus explicitly marked existing message scopes."""
    from backend.services.source_channels import descriptors_from_structure
    current_nodes = connected_source_ids(capture['facts'])
    current_ids = {node['source_id'] for node in capture['facts']['nodes'] if node['id'] in current_nodes}
    history = set(historical_ids)
    tree = inbox_choices(capture, allowed_ids=current_ids | history, historical_ids=history)
    descriptors = descriptors_from_structure(capture, school)
    columns = {}
    for units in tree.values():
        for unit in units:
            for column in unit['own_columns']:
                ident = column['department'].id
                if ident in columns or column['unavailable']:
                    continue
                columns[ident] = dict(column, historical_scope=ident not in current_ids,
                    breadcrumb=descriptors[ident]['breadcrumb'])
    return list(columns.values())


def source_paths(capture):
    """Evidenced owners, directory entrances and configured pages, without guessing."""
    facts = capture['facts']; nodes = {n['id']: n for n in facts['nodes']}
    result = defaultdict(list)
    for edge in facts['relations']:
        target = nodes[edge['target']]
        if target['entity_type'] != 'source' or edge['type'] not in {'source_ownership', 'directory_listing', 'source_on_page'}:
            continue
        source = nodes[edge['source']]
        references = [dict(url=proof['reference_url'], title=source['label'],
            checked_at=proof.get('observed_at') or '', locator=proof['locator'], material=proof['content_hash'])
            for proof in edge['evidence']]
        result[target['source_id']].append(dict(nodes=[dict(name=source['label'])], entry_nodes=[],
            entry_names=[target['label']], references=references, relation_type=edge['type']))
    return dict(result)
