"""Use AI on the first official pages; only observed, same-school links can be queued."""
import hashlib
import json
import re

from bs4 import BeautifulSoup


def queue_navigation(site, page):
    """Fast crawl never waits for a paid model response; AI gets its own turn."""
    from backend.services import tasks
    from backend.database.models import BackgroundTask, School
    from backend.ai.configuration import get_model_binding, AIConfigError
    handle = tasks.current_execution()
    if not handle or page.get('kind') == 'article' or handle.get('payload', {}).get('ai_assist') is False:
        return
    try:
        get_model_binding('directory')
    except AIConfigError:
        return
    parent = handle['id']
    school_id = handle.get('payload', {}).get('school_id')
    if not school_id:
        from backend.services.source_inventory import canonical_url
        school = next((s for s in School.query.all() if canonical_url(s.url) == canonical_url(site['root_url'])), None)
        if not school:
            return
        school_id = school.id
    discover = BackgroundTask.query.filter_by(identity=f'discover:{school_id}').first()
    school_generation = discover.generation if discover else handle.get('generation', 1)
    page_key = hashlib.sha256(page['url'].encode()).hexdigest()[:20]
    tasks.enqueue('navigation_review', f'{parent}:{handle.get("generation", 1)}:{page_key}',
        {'parent_task_id': parent, 'parent_generation': handle.get('generation', 1),
         'school_id': school_id, 'school_generation': school_generation, 'site': dict(site), 'page': dict(page)},
        capability='directory', replace_finished=False)


def process_navigation(payload):
    from flask import current_app
    from filelock import FileLock, Timeout
    from backend.services import tasks
    from backend.services.discovery_cache import DiscoveryCache
    from backend.database.db import db
    from backend.database.models import BackgroundTask
    from .structure import extract_structure
    from backend.services.discovery_control import pause_if_requested
    pause_if_requested()
    parent = db.session.get(BackgroundTask, payload['parent_task_id'])
    if not parent or parent.generation != payload['parent_generation'] or (parent.result or {}).get('automatic_discovery_disabled'):
        return {'status': 'stale'}
    discover = BackgroundTask.query.filter_by(identity=f'discover:{payload["school_id"]}').first()
    if discover and payload.get('school_generation', payload['parent_generation']) != discover.generation:
        return {'status': 'stale'}
    path = current_app.config['DISCOVERY_CACHE_PATH']
    try:
        with FileLock(str(path) + '.worker.lock', timeout=0):
            inventory = DiscoveryCache(path)
            site, page = payload['site'], payload['page']
            html = inventory.snapshot(site['site_key'], page.get('snapshot_url', page['url']))
            if not html:
                ref = page.get('snapshot_ref')
                if not ref:
                    stored = inventory.get_page(site['site_key'], page.get('snapshot_url', page['url'])) or {}
                    note = next((n for n in json.loads(stored.get('notes_json') or '[]') if n.startswith('full_snapshot:')), '')
                    ref = json.loads(note.split(':', 1)[1]) if note else None
                if ref:
                    from backend.services.source_governance import read_snapshot
                    try:
                        html = read_snapshot(ref)
                    except (OSError, ValueError):
                        pass
                if not html:
                    from backend.services.runtime_catalog import RuntimeCatalog
                    html = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH']).snapshot(
                        site['site_key'], page.get('snapshot_url', page['url']))
                if not html:
                    return {'status': 'needs_recovery', 'error_code': 'material_expired'}
            parsed = extract_structure(html, page['url'], site['root_url'], page['kind'], page['label'], json.loads(page['path_json']))
            if page.get('discovery_policy') == 'layered':
                from .layered import route_structure
                from .structure import publication_evidence, add_publication_structure
                feed = publication_evidence(html, page['url'])
                add_publication_structure(parsed, feed)
                route_structure(parsed, page, html, site['root_url'], bool(feed))
            before = {(x['url'], x['decision']) for x in parsed['links']}
        # Model latency must not hold the shared cache writer lock and stall
        # other schools or the next crawl slice.
        if page.get('discovery_policy') == 'layered':
            from .layered import assist
            result = assist(site, page, html, parsed) or {}
        else:
            result = assist_navigation(site, page, html, parsed) or {}
        with FileLock(str(path) + '.worker.lock', timeout=0):
            db.session.refresh(parent)
            if parent.generation != payload['parent_generation']:
                return {'status': 'stale'}
            from backend.services.discovery_changes import remember_navigation
            remember_navigation(inventory, site['site_key'], page.get('snapshot_url', page['url']), parsed,
                                result.get('status') in ('processed', 'succeeded'))
            from backend.services.runtime_catalog import RuntimeCatalog
            RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH']).publish(inventory, site['site_key'], merge=True)
            added = 0
            for link in parsed['links']:
                if link['decision'] in ('follow', 'official_external_link') and (link['url'], link['decision']) not in before:
                    from .structure import document_reference
                    if document_reference(link['url'], link['label']):
                        continue
                    from backend.scraper.http_client import same_school_url
                    if not same_school_url(page['url'], site['root_url']):
                        from backend.services.source_ownership import saved_domain_evidence
                        if not saved_domain_evidence(inventory, site['site_key'], page['url']):
                            continue
                    inventory.enqueue(site['site_key'], link['url'], link['label'], link['kind'], page['depth'] + 1,
                                      link['path'], 'school_domain' if same_school_url(link['url'], site['root_url']) else 'official_backlink')
                    added += 1
            if added:
                # Preserve full provenance alongside the pre-existing parser edges.
                inventory.record_edges(site['site_key'], page['url'], parsed['links'], hashlib.sha256(html.encode()).hexdigest())
                from datetime import datetime
                # Continue the same generation and budget. A fresh enqueue would
                # erase its journal and give every automatic continuation a new allowance.
                db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == parent.id,
                    BackgroundTask.generation == payload['parent_generation'], BackgroundTask.state == 'done',
                    BackgroundTask.phase != 'user_paused', BackgroundTask.deadline_at > datetime.utcnow()).values(state='pending', phase='directory_slice',
                    finished_at=None, available_at=datetime.utcnow(), token=None, worker_id=None, lease_until=None))
                # A last-page crawl can be paused while this independent call
                # finishes. Keep the pause, but remember its newly found work.
                from backend.services.discovery_control import RESUME_KEY, requested
                if requested(parent) and parent.phase == 'user_paused':
                    saved = dict(parent.payload)
                    resume = dict(saved.get(RESUME_KEY) or {})
                    if resume.get('state') == 'done':
                        saved[RESUME_KEY] = dict(resume, state='pending', phase='directory_slice')
                        db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id == parent.id,
                            BackgroundTask.generation == payload['parent_generation'],
                            BackgroundTask.state == 'waiting', BackgroundTask.phase == 'user_paused').values(
                                payload=saved, finished_at=None))
                db.session.commit()
        if result.get('status') == 'pending':
            tasks.defer(capability='directory', phase='ai_resource', delay=result.get('next_delay') or 1,
                        reason='逐项识别已保存，等待处理剩余材料')
        return dict(result, status=result.get('status', 'processed'), queued_pages=added)
    except Timeout:
        tasks.defer(capability='directory', delay=1, reason='等待目录缓存空闲', checkpoint=tasks.current_execution().get('checkpoint'))


def assist_navigation(site, page, html, parsed):
    from backend.services import tasks
    from backend.services.onboarding_progress import record_progress
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.scraper.http_client import same_school_url, validate_public_url
    from backend.services.source_inventory import resolve_page_link
    from backend.services.directory_work import fragments, prepare, run_batch, ready_to_dispatch, waiting_result, reconcile_groups
    from backend.database.db import db
    from backend.database.source_governance_models import DiscoveryWorkItem, SchoolOnboarding
    handle = tasks.current_execution()
    if not handle or handle.get('payload', {}).get('ai_assist') is False:
        return
    try:
        binding = get_model_binding('directory')
    except AIConfigError as exc:
        record_progress(ai_state=exc.code)
        return {'status': 'needs_recovery', 'error_code': exc.code}
    school_id = handle['payload']['school_id']
    generation = handle['payload'].get('school_generation', handle['payload'].get('parent_generation', handle.get('generation', 1)))
    page_key = hashlib.sha256(page['url'].encode()).hexdigest()[:20]
    known = {link['url']: link for link in parsed['links']}
    from backend.services.source_inventory import canonical_url
    onboarding = db.session.get(SchoolOnboarding, school_id)
    scopes = json.loads(onboarding.scope_json or '[]') if onboarding else []
    trail = json.loads(page['path_json'])
    entities = [{'id': 'school', 'name': site['name']}]
    entities.extend({'id': scope['key'], 'name': scope['name'], 'kind': 'unit'} for scope in scopes
        if canonical_url(scope.get('url', '')) == canonical_url(page['url']) or
        scope['name'] in trail or scope['name'] == page.get('label'))
    candidates, groups = {}, []
    snapshot_hash = hashlib.sha256(html.encode()).hexdigest()
    for index, document in enumerate(fragments(html, limit=12 * 1024)):
        soup = BeautifulSoup(document, 'lxml')
        anchors = {}
        for anchor in soup.select('a[href]'):
            url = resolve_page_link(page['url'], anchor.get('href', ''))
            name = anchor.get_text(' ', strip=True) or anchor.get('title') or anchor.get('aria-label') or '无文字链接'
            if not url:
                continue
            try:
                validate_public_url(url, resolve=False)
            except ValueError:
                continue
            ident = 'link-' + hashlib.sha256(url.encode()).hexdigest()[:24]
            anchors[ident] = {'candidate_id': ident, 'name': name, 'url': url}
        if not anchors:
            ident = 'region-' + str(index)
            anchors[ident] = {'candidate_id': ident, 'name': page['label'] or site['name'],
                'url': page['url'], 'kind_hint': 'unknown'}
        candidates.update(anchors)
        values = list(anchors.values())
        for offset in range(0, len(values), 8):
            selected = values[offset:offset + 8]
            evidence = {'school_id': school_id, 'candidates': selected,
                'entities': entities,
                'observed_urls': list(dict.fromkeys([page['url'], *(a['url'] for a in anchors.values())])),
                'evidence': [{'evidence_id': 'page', 'url': page['url'], 'html': document,
                    'snapshot_hash': snapshot_hash, 'fragment_number': index + 1}]}
            from backend.services.directory_work import partition
            for part, request in enumerate(partition(evidence, 'classify')):
                group = f'page:{page_key}:region:{index}:batch:{offset // 8}:part:{part}'
                prepare(school_id, generation, group, 'classify', request)
                groups.append((group, request))
    reconcile_groups(school_id, generation, f'page:{page_key}:', {key for key, _ in groups})
    accepted, remaining, dispatched, error = [], [], False, ''
    for group, evidence in groups:
        rows = DiscoveryWorkItem.query.filter_by(school_id=school_id, generation=generation, group_key=group).filter(DiscoveryWorkItem.state != 'superseded').all()
        if all(r.state == 'succeeded' for r in rows):
            accepted.extend(r.result_json for r in rows)
            continue
        if not dispatched and ready_to_dispatch(rows):
            record_progress(phase='ai', ai_state='running', current_label=page['label'] or site['name'])
            result = run_batch(school_id, generation, group, 'classify', evidence, binding,
                existing_attempts=handle['payload'].get('legacy_ai_attempts', 0))
            dispatched = True
            accepted.extend((result.get('output') or {}).get('results', []))
            if result['status'] != 'succeeded':
                remaining.append(result)
                error = result.get('error_code', '')
        else:
            accepted.extend(r.result_json for r in rows if r.state == 'succeeded')
            remaining.append(waiting_result(rows))
    for row in accepted:
        actions = list(row.get('actions', []))
        anchor = candidates.get(row.get('candidate_id'))
        if anchor and row.get('decision') == 'propose' and row.get('kind') in ('directory', 'unit', 'channel'):
            actions.append({'type': 'read_page', 'url': anchor['url'], 'purpose':
                {'channel': 'list', 'unit': 'directory', 'directory': 'directory'}[row['kind']]})
        for action in actions:
            target = action.get('url')
            observed = next((a for a in candidates.values() if a['url'] == target), None)
            kind = {'article': 'article', 'list': 'channel', 'directory': 'directory'}.get(action.get('purpose'))
            if action.get('type') != 'read_page' or not observed or not kind:
                continue
            decision = 'follow' if same_school_url(target, site['root_url']) else 'official_external_link'
            kind = row.get('kind') if anchor and target == anchor['url'] and row.get('kind') in ('unit', 'channel', 'directory') else kind
            label = row.get('name') or observed['name']
            if target in known:
                known[target].update(kind=kind, label=label, decision=decision)
            else:
                link = {'url': target, 'label': label, 'kind': kind, 'path': json.loads(page['path_json']),
                    'locator': 'ai-observed-link', 'decision': decision}
                parsed['links'].append(link); known[target] = link
    runnable = [r for r in remaining if r['status'] == 'pending']
    state = 'pending' if runnable else 'needs_recovery' if remaining else 'succeeded'
    record_progress(phase='crawl', ai_state=state, ai_error_code=error)
    return {'status': state, 'error_code': error,
        'next_delay': min((r.get('next_delay') or 1 for r in runnable), default=0)}
