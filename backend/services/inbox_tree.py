"""Reader navigation only; never writes or infers structure/ownership facts."""
from collections import defaultdict
from hashlib import sha256

from backend.services.structure_snapshot import department_records
from backend.services.source_inventory import canonical_url


def display_tree(capture, choices, school_url):
    """Nest the captured choices using saved directory/display placements.

    Verified publishers come from the shared adapter. Older directory and preset
    paths only organize the picker; they are not exported as publisher facts.
    """
    records = {row['id']: row for row in department_records(capture)}
    facts = {node['id']: node for node in capture['facts']['nodes']}
    owners = defaultdict(set)
    for relation in capture['facts']['relations']:
        if relation['type'] == 'source_ownership' and relation['target'] in facts:
            source_id = facts[relation['target']].get('source_id')
            publisher = facts.get(relation['source'], {})
            if source_id and publisher.get('department_id') in records:
                owners[source_id].add(publisher['department_id'])

    def navigation_node(ident):
        row = records[ident]
        # Old configurations can use a publisher's own notice source as a
        # directory heading. Reuse its explicitly proved publisher identity,
        # never merge two units merely because their names match.
        if len(owners[ident]) == 1:
            publisher = records[next(iter(owners[ident]))]
            if row['name'] == publisher['name']:
                row = publisher
        return dict(key=f"record:{row['id']}", name=row['name'], url=row.get('list_url') or '')

    parents = defaultdict(list)
    for entry in capture['directory_entries']:
        parent, child = records.get(entry.parent_id), records.get(entry.department_id)
        if parent and child and parent['school_id'] == child['school_id'] and parent['id'] != child['id']:
            parents[child['id']].append(parent['id'])
    groups = {}

    def stored_paths(ident, seen=()):
        if ident in seen or len(seen) >= 8:
            return []
        row = records[ident]
        if not parents[ident]:
            if row.get('kind') == 'group':
                return [(row['name'], [])]
            return [(row.get('group_name') or '', [navigation_node(ident)])]
        result = []
        for parent in parents[ident]:
            for group, nodes in stored_paths(parent, (*seen, ident)):
                node = navigation_node(ident)
                result.append((group, nodes if nodes and nodes[-1]['key'] == node['key'] else nodes + [node]))
                if len(result) >= 32:
                    return result
        return result

    def put(group, path, column, basis):
        siblings = groups.setdefault(group, {})
        chain = [group]
        for node in path:
            chain.append(str(node['key']))
            key = 'picker-' + sha256('\0'.join(chain).encode()).hexdigest()[:20]
            unit = siblings.setdefault(key, dict(key=key, name=node['name'], own_columns=[],
                children={}, columns=[], expandable=True, basis=basis,
                navigation_url=node.get('url') or '', navigation_key=str(node['key']),
                preset_heading=bool(node.get('preset_heading'))))
            ident = node.get('department_id')
            if ident is None and str(node['key']).startswith('record:'):
                ident = int(str(node['key']).split(':',1)[1])
            if ident in records:
                unit.update(department_id=ident,url=node.get('url') or '')
            if basis == 'verified_owner':
                unit['basis'] = basis
            siblings = unit['children']
        if not path and column is not None:
            key = 'leaf-' + str(column['department'].id)
            unit = siblings.setdefault(key, dict(key=key, name='', own_columns=[],
                children={}, columns=[], expandable=False, basis=basis))
        if column is not None and not any(c['department'].id == column['department'].id for c in unit['own_columns']):
            unit['own_columns'].append(dict(column, display_group=''))

    # Reuse captured directory and page context for both reading and source
    # preferences. These remain display placements, never publisher relations.
    from backend.services.structure_snapshot import recorded_source_choices
    columns = {c['department'].id:c for units in choices.values() for unit in units
        for c in unit['columns']}
    recorded = recorded_source_choices(capture, columns)
    recorded_paths = defaultdict(list)
    def collect(unit, group, trail):
        path = trail
        if unit['expandable']:
            ident = unit.get('department_id')
            node = dict(key=f'record:{ident}' if ident is not None else 'recorded:'+unit['key'],
                name=unit['name'],url=unit.get('url') or '',department_id=ident)
            path = trail + [node]
            if not unit['columns']:
                put(group,path,None,'display_directory')
        for column in unit['own_columns']:
            if path:
                recorded_paths[column['department'].id].append((group,path,column['label']))
        for child in unit['children']:
            collect(child,group,path)
    for group, units in recorded.items():
        for unit in units:
            collect(unit,group,[])

    from backend.services.starter_catalog import placements
    for fallback_group, units in choices.items():
        for unit in units:
            owner = facts.get(unit['key']) if unit['expandable'] else None
            for column in unit['own_columns']:
                row = column['department']
                if fallback_group == '历史通知范围':
                    put(fallback_group, [], column, 'historical_scope')
                    continue
                if owner:
                    owner_id = owner.get('department_id')
                    paths = stored_paths(owner_id) if owner_id in records else []
                    if not paths:
                        group = column.get('display_group') or fallback_group
                        if group == owner['label']:
                            group = '学校栏目' if owner.get('organization_kind') == 'school' else fallback_group
                        paths = [(group, [dict(key=unit['key'], name=unit['name'])])]
                    for group, path in paths:
                        put(group or fallback_group, path, column, 'verified_owner')
                    continue
                # Explicit saved directory memberships are useful navigation,
                # even when they do not prove an administrative publisher.
                observed = recorded_paths.get(row.id, [])
                if observed:
                    for group, path, label in observed:
                        put(group,path,dict(column,label=label),'display_directory')
                    continue
                paths = [(group, path[:-1]) for group, path in stored_paths(row.id)
                         if parents[row.id] and column.get('display_group') and path]
                if not paths:
                    paths = [(item['group'], [dict(node, preset_heading=True) for node in item['nodes']])
                             for item in placements(school_url, row)]
                if not paths:
                    paths = [(column.get('display_group') or fallback_group, [])]
                for group, path in paths:
                    put(group or fallback_group, path, column, 'display_directory')

    def refresh_totals(unit):
        unique = {c['department'].id: c for c in unit['own_columns']}
        for child in unit['children']:
            for column in child['columns']:
                unique.setdefault(column['department'].id, column)
        unit['columns'] = list(unique.values())
        rank = lambda item: (-max((c.get('attention_score', 0) for c in item['columns']), default=0),
                             min((c['department'].id for c in item['columns']), default=0))
        unit['children'].sort(key=rank)
        unit['own_columns'].sort(key=lambda c: (-c.get('attention_score', 0), c['department'].id))
        return unit

    def finish(unit):
        unit['children'] = [finish(child) for child in unit['children'].values()]
        return refresh_totals(unit)

    def display_entrance(unit):
        if unit['basis'] != 'display_directory' or not unit['expandable']:
            return None
        try:
            address = canonical_url(unit.get('navigation_url') or '')
        except (ValueError, UnicodeError):
            address = ''
        if address:
            return ('entrance', unit['name'], address)
        identity = unit.get('navigation_key') or ''
        if identity.startswith('record:'):
            return ('record', unit['name'], identity)
        return None

    def fold_repeated_displays(units):
        kept, entrances, identical_controls = [], {}, {}
        addressed = defaultdict(set)
        for unit in units:
            identity = display_entrance(unit)
            if identity and identity[0] == 'entrance':
                addressed[unit['name']].add(identity[2])
        def control_signature(unit):
            return (unit['key'] if unit['basis'] == 'verified_owner' else '', unit['name'],
                unit.get('navigation_url') or '', tuple(sorted(c['department'].id for c in unit['own_columns'])),
                tuple(sorted(control_signature(child) for child in unit['children'])))
        for unit in units:
            unit['children'] = fold_repeated_displays(unit['children'])
            # A preset is an explicit display grouping of an exact saved
            # configuration, not another organization identity. It may reuse
            # the one unambiguous same-named entrance in these siblings only.
            if (unit.get('preset_heading') and not unit.get('navigation_url')
                    and len(addressed[unit['name']]) == 1):
                unit['navigation_url'] = next(iter(addressed[unit['name']]))
            # Directory copies can contain overlapping or complementary items.
            # Reuse their exact display entrance and union sources/children;
            # never merge stored entities or independently verified publishers.
            identity = display_entrance(unit)
            previous = entrances.get(identity) if identity else None
            signature = control_signature(unit)
            if (not previous and unit['basis'] == 'display_directory'
                    and not unit.get('navigation_url') and unit['expandable']):
                previous = identical_controls.get(signature)
            if previous:
                own = {c['department'].id:c for c in previous['own_columns']}
                for column in unit['own_columns']:
                    own.setdefault(column['department'].id, column)
                previous['own_columns'] = list(own.values())
                previous['children'] = fold_repeated_displays(previous['children'] + unit['children'])
                refresh_totals(previous)
                continue
            if identity:
                entrances[identity] = unit
            if unit['basis'] == 'display_directory' and not unit.get('navigation_url') and unit['expandable']:
                identical_controls[signature] = unit
            kept.append(refresh_totals(unit))
        return kept

    result = {name: [finish(unit) for unit in entries.values()] for name, entries in groups.items()}
    result = {name: fold_repeated_displays(units) for name, units in result.items()}
    for name, units in result.items():
        nested = {c['department'].id for unit in units if unit['expandable'] for c in unit['columns']}
        verified = [unit for unit in units if unit['basis'] == 'verified_owner']
        def redundant(unit):
            ids = {c['department'].id for c in unit['columns']}
            if not unit['expandable']:
                return ids <= nested
            # Retire a redundant legacy display branch only when the current
            # branch already exposes every one of its exact source IDs. Two
            # verified publishers (including shared sources) remain separate.
            return unit['basis'] == 'display_directory' and any(
                parent['name'] == unit['name'] and ids <= {c['department'].id for c in parent['columns']}
                for parent in verified)
        result[name] = [unit for unit in units if not redundant(unit)]
    return result
