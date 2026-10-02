"""Recover missing website placement from a durable, public evidence checklist."""
import json

from flask import current_app
from filelock import FileLock

from backend.database.db import db
from backend.database.models import BackgroundTask, Department, School
from backend.services import tasks
from backend.services.discovery_cache import DiscoveryCache
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_inventory import canonical_url, site_key


def missing_groups(school_id):
    return Department.query.filter(Department.school_id == school_id,
        db.func.coalesce(db.func.trim(Department.group_name), '') == '',
        Department.list_url.is_not(None), Department.list_url != '').all()


def placement_gaps(school_id):
    """Read durable gaps without confusing a finished check with full coverage."""
    task = BackgroundTask.query.filter_by(identity=f'source_grouping:{school_id}').first()
    if not task:
        return []
    data = (task.result or {}) if task.state == 'done' else (task.checkpoint or {})
    gaps = [{**gap, 'state':'placement_unverified'} for gap in
            data.get('gaps', data.get('grouping_gaps', []))]
    if task.state == 'failed' and not gaps:
        gaps.append({'state':'placement_unverified','error_code':task.error_code,
                     'reason':'官网归属核对未完成，已保留进度，需要恢复'})
    return gaps


def run_grouping(payload, *, checkpoint=None, fetcher=None, page_budget=3):
    """Read official directories and requested sources; never invoke a model.

    A slice limit only yields execution. Every discovered dependency remains in
    the checkpoint, including failed reads, and successful evidence is published
    without changing source IDs, extraction rules or subscriptions.
    """
    from backend.scraper.discovery.inventory_crawler import fetch_page, inspect_page
    from backend.services.source_placements import official_source_placements
    school = db.session.get(School, payload['school_id'])
    if not school or not school.enabled:
        return {'continuation_required': False, 'restored_ids': [], 'gaps': []}
    handle = tasks.current_execution()
    state = dict(checkpoint if checkpoint is not None else (handle or {}).get('checkpoint') or {})
    targets = [dict(t) for t in state.get('grouping_targets', [])]
    sources = [dict(s) for s in state.get('grouping_sources', [])]
    if not sources:
        selected = payload.get('source_ids')
        sources = [{'id':d.id, 'name':d.name, 'url':d.list_url} for d in missing_groups(school.id)
                   if selected is None or d.id in selected]
    if not sources:
        return {'continuation_required': False, 'restored_ids': [], 'gaps': []}

    def add(url, label, kind):
        url = canonical_url(url)
        existing = next((t for t in targets if t['url'] == url), None)
        if existing and existing['kind'] == 'channel' and kind in ('directory', 'unit'):
            existing.update(label=label, kind=kind)
        elif url and not existing:
            targets.append({'url':url, 'label':label, 'kind':kind, 'state':'pending'})

    add(school.url, school.name, 'root')
    for source in sources:
        add(source['url'], source['name'], 'channel')
    path = current_app.config['DISCOVERY_CACHE_PATH']
    catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
    with FileLock(str(path) + '.worker.lock', timeout=0):
        inventory = DiscoveryCache(path)
        key = inventory.ensure_site(school.name, school.url)
        for _ in range(page_budget):
            pending = [t for t in targets if t['state'] == 'pending']
            if not pending:
                break
            target = min(pending, key=lambda t: {'root':0, 'directory':1, 'unit':2}.get(t['kind'], 3))
            from backend.services.discovery_control import pause_if_requested
            pause_if_requested()
            tasks.assert_owned()
            inventory.enqueue(key, target['url'], target['label'], target['kind'], 1, [], 'school_domain')
            report = inventory.report(key)
            page = next(p for p in report['pages'] if p['url'] == target['url'])
            inspect_page(inventory, report['site'], dict(page, kind=target['kind'], label=target['label']),
                         fetcher=fetcher or fetch_page)
            page = next(p for p in inventory.report(key)['pages'] if p['url'] == target['url'])
            target.update(state=page['state'], reason=page.get('error') or '')
            if page['state'] == 'fetched':
                if target['kind'] in ('root', 'directory'):
                    with inventory.connect() as c:
                        edges = [dict(r) for r in c.execute('SELECT * FROM edges WHERE site_key=? AND parent_url=?',
                                                           (key, target['url']))]
                    # Top-level official navigation can contain a roster even
                    # when its title says teaching, research or admissions.
                    # Probe observed menu entries; category keywords alone must
                    # not decide which official branches are inspected.
                    navigation_urls = {n['url'] for n in inventory.structure(key)
                        if target['kind'] == 'root' and n['reference_url'] == target['url']
                        and n['relation'] == 'navigation_entry' and n['kind'] != 'unit'}
                    for edge in edges:
                        if edge['decision'] not in ('follow', 'official_external_link'):
                            continue
                        if edge['kind'] == 'directory' or edge['target_url'] in navigation_urls:
                            add(edge['target_url'], edge['label'], 'directory')
                        elif edge['kind'] == 'unit' and any(s['name'] == edge['label'] or
                                any(s['name'].startswith(edge['label'] + sep) for sep in ('-', '－', '—')) for s in sources):
                            add(edge['target_url'], edge['label'], 'unit')
                for feed in json.loads(page.get('feed_json') or '{}').get('lists', []):
                    if target['kind'] == 'unit' and feed.get('column_url'):
                        add(feed['column_url'], feed['name'], 'channel')
                    alias = feed.get('listing_alias') or {}
                    if alias.get('canonical_url'):
                        add(alias['canonical_url'], feed['name'], 'channel')
                    if alias.get('first_page_url'):
                        add(alias['first_page_url'], feed['name'], 'channel')
            state.update(grouping_targets=targets, grouping_sources=sources)
            if handle:
                tasks.checkpoint(state)
        catalog.publish(inventory, key, merge=True)
    ids = [s['id'] for s in sources]
    # Website reads may outlast edits from the UI. Refresh current identities,
    # then apply a conditional update so a late result cannot replace user data.
    db.session.expire_all()
    departments = Department.query.filter(Department.id.in_(ids), Department.school_id == school.id).all()
    placements = official_source_placements(departments)
    restored = set(state.get('restored_ids', []))
    for department in departments:
        groups = {p['group'] for p in placements.get(department.id, [])}
        if not (department.group_name or '').strip() and len(groups) == 1:
            tasks.assert_owned()
            updated = db.session.execute(db.update(Department).where(
                Department.id == department.id, Department.school_id == school.id,
                Department.name == department.name, Department.list_url == department.list_url,
                db.func.coalesce(db.func.trim(Department.group_name), '') == '')
                .values(group_name=groups.pop()).execution_options(synchronize_session=False))
            if updated.rowcount:
                restored.add(department.id)
    tasks.assert_owned()
    db.session.commit()
    state['restored_ids'] = sorted(restored)
    gaps = [{'source_id':d.id, 'name':d.name, 'url':d.list_url,
             'reason':'尚无足够官网证据确定归属'} for d in departments if d.id not in placements]
    gaps += [{'url':t['url'], 'reason':t['reason'] or '官网材料未能读取'} for t in targets
             if t['state'] not in ('pending', 'fetched')]
    state['grouping_gaps'] = gaps
    if handle:
        tasks.checkpoint(state)
    return {'continuation_required': any(t['state'] == 'pending' for t in targets),
            'restored_ids':sorted(restored), 'gaps':gaps, 'checkpoint':state}
