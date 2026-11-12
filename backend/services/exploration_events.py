"""Append actual page inspections, separately from navigation and ownership.

Events describe work that ran. They never infer old execution from a current
graph, measure semantic yield, or duplicate the model/network billing ledger.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import re
import uuid

from .exploration_event_store import EventReader

_active = ContextVar('recorded_page_inspection', default=None)


def ensure_schema(connection):
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS exploration_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE, site_key TEXT NOT NULL, url TEXT NOT NULL,
            event_type TEXT NOT NULL, parent_event_id TEXT, observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS exploration_site_sequence ON exploration_events(site_key,sequence);
        CREATE INDEX IF NOT EXISTS exploration_page_origin ON exploration_events(site_key,url,event_type,sequence);
        CREATE INDEX IF NOT EXISTS exploration_parent ON exploration_events(site_key,parent_event_id,event_type,sequence);
    ''')


def _url(value):
    # Reuse exactly the public identity policy, including fragment query secrets.
    from .structure_facts import _url as public_url
    return public_url(value)


def _append(connection, key, url, event_type, parent=None, **payload):
    from backend.scraper.discovery.parser_revision import PARSER_REVISION
    payload.update(recorder_version=1, parser_revision=PARSER_REVISION)
    observed_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    event_id = 'event:' + uuid.uuid4().hex
    connection.execute('INSERT INTO exploration_events(event_id,site_key,url,event_type,parent_event_id,observed_at,payload_json) '
        'VALUES(?,?,?,?,?,?,?)', (event_id, key, url, event_type, parent, observed_at,
        json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    return event_id


def observe_links(inventory, key, parent_url, links, material_hash):
    active = _active.get()
    if not active or active['path'] != str(inventory.path.resolve()) or active['site_key'] != key or active['url'] != parent_url:
        return
    active['links'] = [dict(url=_url(link.get('url')), label=link['label'], kind=link['kind'],
        evidence=dict(reference_url=parent_url, locator=link.get('locator'), content_hash=material_hash))
        for link in links if _url(link.get('url')) and link.get('locator') and material_hash]


def record_enqueue(connection, inventory, key, url, label, kind):
    """Called only for a genuinely new page, inside its enqueue transaction."""
    active = _active.get()
    if not active or active['path'] != str(inventory.path.resolve()) or active['site_key'] != key:
        return
    proof = list({json.dumps(link['evidence'], sort_keys=True): link['evidence'] for link in active['links']
        if link['url'] == url and link['label'] == label and link['kind'] == kind}.values())
    if not proof or not _url(url):
        return
    _append(connection, key, url, 'page_discovered', active['event_id'], label=label,
        evidence=proof, origin='first_enqueue_during_recorded_'+active.get('activity', 'inspection'))


def current_inspection_origin(key, url, material_hash):
    """Reference the inspection actually queueing this work, never a URL guess."""
    active = _active.get()
    if (not active or active.get('activity', 'inspection') != 'inspection'
            or active['site_key'] != key or active['url'] != _url(url) or not material_hash):
        return None
    origin = dict(event_id=active['event_id'], site_key=key, url=active['url'], content_hash=material_hash)
    from backend.ai.material_capture import record_navigation_origin
    record_navigation_origin(origin)
    return origin


def _review_summary(result):
    summary = dict(status=result.get('status') if result.get('status') in {
        'processed', 'succeeded', 'pending', 'needs_recovery', 'branch_stopped', 'skipped'} else 'unknown')
    for field in ('reviewed_routes', 'cached_routes', 'remaining_routes', 'deferred_routes'):
        value = result.get(field)
        summary[field] = value if type(value) is int and value >= 0 else None
    reference = result.get('decision_id')
    summary['decision_reference'] = reference if isinstance(reference, str) and re.fullmatch(r'routes:[0-9a-f]{64}', reference) else None
    return summary


def _inspection_parent(connection,key,url,material_hash,origin):
    if not (isinstance(origin,dict) and origin.get('site_key')==key
            and _url(origin.get('url'))==url and origin.get('content_hash')==material_hash):return None
    reader = EventReader(connection)
    original = reader.get(key, origin.get('event_id'), url=url, types=('exploration_started', 'page_inspection_started'))
    if not original:return None
    for item in reader.query(key, parent_id=original['event_id'], types=('page_inspection_finished',)):
        payload=json.loads(item['payload_json'])
        if payload.get('state')=='fetched' and payload.get('content_hash')==material_hash:return item['event_id']
    return original['event_id']


@contextmanager
def navigation_model_observation(inventory,site,page,material_hash,origin):
    """Record actual runtime/cache uses even if applying their result later fails."""
    from .execution_observations import observe_model_uses,public_uses,public_cache_uses
    from .tasks import current_execution
    task=current_execution() or {};holder=dict(result={},event_id=None);error=None
    with observe_model_uses() as observed:
        try:yield holder
        except BaseException as caught:
            error=type(caught).__name__;raise
        finally:
            uses=public_uses(observed['uses']);cache=public_cache_uses(observed['cache_reuses'])
            if uses or cache or observed['unrecorded_attempts']:
                try:
                    key=site['site_key'];url=_url(page.get('snapshot_url') or page['url'])
                    if url:
                        with inventory.connect() as connection:
                            connection.execute('BEGIN IMMEDIATE')
                            parent=_inspection_parent(connection,key,url,material_hash,origin)
                            holder['event_id']=_append(connection,key,url,'navigation_model_observed',parent,
                                label=page['label'],content_hash=material_hash,task_id=task.get('id'),
                                task_generation=task.get('generation'),model_uses=uses,cache_reuses=cache,
                                unrecorded_model_attempts=observed['unrecorded_attempts'],
                                review_summary=_review_summary(holder['result']),error_type=error,
                                upstream_status='recorded_queueing_inspection' if parent else 'material_inspection_not_recorded',
                                scope='model_result_use_not_invoice_or_http_confirmation',cost=None,cost_source='ledger_reference_only')
                except Exception as recording_error:
                    if error is None:raise
                    logging.getLogger(__name__).error('Model observation recording failed: %s',type(recording_error).__name__)


@contextmanager
def navigation_application(inventory, site, page, material_hash, result, origin, *, review_event_id=None):
    """Observe actual result writes, not model dispatch, billing or a new fetch.

    The caller already holds the cache writer lock and has checked generation,
    material and parser freshness. Individual Inventory commits remain separate;
    an interrupted application can therefore contain genuinely committed pages.
    """
    key, url = site['site_key'], _url(page.get('snapshot_url') or page['url'])
    if not url:
        yield
        return
    from .tasks import current_execution
    task = current_execution() or {}
    with inventory.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        parent = _inspection_parent(connection, key, url, material_hash, origin)
        if review_event_id:
            review = EventReader(connection).get(key, review_event_id, url=url, types=('navigation_model_observed',))
            values=json.loads(review['payload_json']) if review else {}
            if (review and review['parent_event_id']==parent and values.get('content_hash')==material_hash
                    and values.get('task_id')==task.get('id') and values.get('task_generation')==task.get('generation')):
                parent=review_event_id
        started = _append(connection, key, url, 'navigation_application_started', parent,
            label=page['label'], content_hash=material_hash, task_id=task.get('id'),
            task_generation=task.get('generation'), review_summary=_review_summary(result),
            upstream_status='recorded_queueing_inspection' if parent else 'material_inspection_not_recorded',
            scope='navigation_result_application_not_model_dispatch', cost=None, cost_source='not_measured')
    marker = _active.set(dict(path=str(inventory.path.resolve()), site_key=key, url=url,
        event_id=started, links=[], activity='navigation_application'))
    error = None
    try:
        yield
    except BaseException as caught:
        error = type(caught).__name__
        raise
    finally:
        try:
            try:
                with inventory.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    count = len(EventReader(connection).query(key, parent_id=started, types=('page_discovered',)))
                    _append(connection, key, url, 'navigation_application_interrupted' if error else 'navigation_application_finished',
                        started, label=page['label'], content_hash=material_hash, new_page_count=count,
                        outcome='interrupted' if error else 'navigation_result_applied', error_type=error,
                        cost=None, cost_source='not_measured')
            except BaseException as recording_error:
                if error is None:
                    raise
                # Preserve the original application/lease exception. Do not
                # print error messages or task payloads into the fallback log.
                logging.getLogger(__name__).error('Navigation interruption recording failed: %s',
                    type(recording_error).__name__)
        finally:
            _active.reset(marker)


@contextmanager
def inspection(inventory, site, page):
    key, url = site['site_key'], _url(page['url'])
    if not url:
        yield
        return
    from .tasks import current_execution
    task = current_execution() or {}
    choice=page.get('exploration_selection') or {}
    demand=choice.get('attention') or {}
    selection=(dict(version=choice.get('version'),reason=choice.get('reason'),claim_number=choice.get('claim_number'),
        policy=deepcopy(choice.get('attention_policy')),score=demand.get('score'),needs=list(demand.get('needs') or [])) if choice else None)
    with inventory.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        candidates = EventReader(connection).query(key, url=url, types=('page_discovered',), limit=1)
        origin = candidates[0] if candidates else None
        parent = origin['event_id'] if origin else None
        is_root = url == _url(site['root_url'])
        started = _append(connection, key, url, 'exploration_started' if is_root else 'page_inspection_started',
            None if is_root else parent, label=page['label'],
            upstream_status='root_inspection' if is_root else 'recorded_first_enqueue' if parent else 'not_recorded',
            task_id=task.get('id'), task_generation=task.get('generation'),
            scope='page_inspection_not_transport_attempt', selection_context=selection,
            cost=None, cost_source='not_measured')
    marker = _active.set(dict(path=str(inventory.path.resolve()), site_key=key, url=url, event_id=started, links=[]))
    try:
        yield dict(event_id=started)
    except BaseException as error:
        with inventory.connect() as connection:
            _append(connection, key, url, 'page_inspection_interrupted', started, label=page['label'],
                outcome='interrupted', error_type=type(error).__name__, cost=None, cost_source='not_measured')
        raise
    else:
        row = inventory.get_page(key, page['url']) or {}
        state, health = row.get('state'), row.get('health')
        outcome = ('read_failed' if state == 'failed' or health == 'unreachable' else
                   'access_blocked' if state == 'blocked' else
                   'reference_only' if state == 'reference_only' else 'material_observed' if state == 'fetched' else 'not_finished')
        with inventory.connect() as connection:
            _append(connection, key, url, 'page_inspection_finished', started,
                label=page['label'], state=state, outcome=outcome,
                content_hash=row.get('content_hash') if state == 'fetched' else None,
                status_code=row.get('status_code'), final_url=_url(row.get('final_url') or ''),
                cost=None, cost_source='not_measured')
    finally:
        _active.reset(marker)


def read_events(connection, key, *, limit=2000, maximum=4000):
    """Bound the published window while retaining its causal ancestors if able."""
    limit, maximum = max(1, int(limit)), max(1, int(maximum))
    reader = EventReader(connection)
    rows = reader.query(key, descending=True, limit=min(limit, maximum))
    selected = {row['event_id']: row for row in rows}
    pending = list(rows)
    while pending and len(selected) < maximum:
        row = pending.pop()
        parent = row['parent_event_id']
        if not parent or parent in selected:
            continue
        found = reader.get(key, parent)
        if found:
            selected[parent] = found
            pending.append(found)
    events = [dict(json.loads(row['payload_json']), id=row['event_id'], sequence=row['sequence'],
        url=row['url'], event_type=row['event_type'], parent_event_id=row['parent_event_id'], observed_at=row['observed_at'])
        for row in sorted(selected.values(), key=lambda item: item['sequence'])]
    missing = sorted({event['parent_event_id'] for event in events if event['parent_event_id'] and event['parent_event_id'] not in selected})
    total = reader.total(key)
    return events, dict(schema_version=1, total_events=total, published_events=len(events),
        selected_recent_events=len(rows), maximum_events=maximum, truncated=len(events) < total,
        missing_parent_ids=missing, history_before_recording='unknown',
        recording_scope='instrumented_page_inspections_navigation_application_and_first_enqueue',
        unrecorded_producers=['legacy_execution', 'external_enqueue', 'unreferenced_model_dispatch', 'source_activation'])


def public_events(events, facts, *, allow_historical=False):
    """Bind recorded page events to public identities without adding fact edges."""
    pages = {node['url']: node['id'] for node in facts['nodes'] if node['entity_type'] == 'page'}
    stream_key = facts.get('site_key') or 'unscoped'
    output = []
    for event in events:
        url = _url(event.get('url'))
        if not url or (url not in pages and (not allow_historical or not facts.get('site_key'))):
            continue
        record = {key: event.get(key) for key in ('id', 'sequence', 'event_type', 'parent_event_id',
            'observed_at', 'label', 'upstream_status', 'task_id', 'task_generation', 'scope', 'state',
            'outcome', 'content_hash', 'status_code', 'error_type', 'cost', 'cost_source',
            'recorder_version', 'parser_revision', 'selection_context', 'review_summary', 'new_page_count') if key in event}
        from .structure_facts import _digest
        record.update(url=url, node_id=pages.get(url) or f"page:{facts['site_key']}:{_digest(url)[:24]}",
                      stream_id=f'page_observations:{stream_key}')
        if url not in pages:
            record['historical_only'] = True
        if event.get('final_url') and _url(event['final_url']):
            record['final_url'] = _url(event['final_url'])
        if event.get('evidence'):
            record['evidence'] = [dict(reference_url=_url(proof.get('reference_url')), locator=proof.get('locator'),
                content_hash=proof.get('content_hash')) for proof in event['evidence'] if _url(proof.get('reference_url'))]
        if event.get('event_type')=='navigation_model_observed':
            from .execution_observations import public_uses,public_cache_uses
            record.update(model_uses=public_uses(event.get('model_uses')),cache_reuses=public_cache_uses(event.get('cache_reuses')),
                unrecorded_model_attempts=event.get('unrecorded_model_attempts')
                    if type(event.get('unrecorded_model_attempts')) is int and event['unrecorded_model_attempts']>=0 else 0)
        output.append(record)
    return output
