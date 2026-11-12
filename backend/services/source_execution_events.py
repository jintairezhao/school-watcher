"""Execution provenance on configuration history, never an ownership authority.

Page inspection and configuration records have separate sequence domains. Their
only cross-stream ordering is an explicit, validated parent event reference.
Reads use captured rows, never the mutable Inventory or task queue.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json

from .structure_facts import _digest, _url

MAX_RECEIPT_EVENTS = 256
MAX_RECEIPT_BYTES = 512 * 1024
PAGE_TYPES = {'exploration_started', 'page_discovered', 'page_inspection_started',
              'page_inspection_finished', 'page_inspection_interrupted',
              'navigation_application_started', 'navigation_application_finished', 'navigation_application_interrupted',
              'navigation_model_observed'}


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def capture_page_receipt(by_id, target_id, site_key):
    """Copy only recorded ancestors, preserving a missing or truncated upstream."""
    if not target_id or not site_key:
        return None
    from .exploration_events import public_events
    events, seen, cursor, size = [], set(), target_id, 0
    while cursor and cursor in by_id and cursor not in seen and len(events) < MAX_RECEIPT_EVENTS:
        event = public_events([by_id[cursor]], dict(nodes=[], site_key=site_key), allow_historical=True)
        if not event:
            break
        record = event[0]
        for name in ('node_id', 'stream_id', 'historical_only'):
            record.pop(name, None)
        size += len(json.dumps(record, ensure_ascii=False).encode())
        if size > MAX_RECEIPT_BYTES:
            break
        seen.add(cursor)
        events.insert(0, record)
        cursor = record.get('parent_event_id')
    if not events:
        return None
    body = dict(schema_version=1, site_key=site_key, target_event_id=target_id,
                events=events, missing_parent_id=cursor, truncated=bool(cursor))
    receipt = dict(body, checksum=_digest(body))
    while len(json.dumps(receipt).encode()) > MAX_RECEIPT_BYTES and len(events) > 1:
        events.pop(0)
        body.update(missing_parent_id=events[0].get('parent_event_id'), truncated=True)
        receipt = dict(body, checksum=_digest(body))
    return receipt if len(json.dumps(receipt).encode()) <= MAX_RECEIPT_BYTES else None


def _receipt_events(receipt, target_id, material_hash, url):
    if not isinstance(receipt, dict):
        return None
    body = {key: value for key, value in receipt.items() if key != 'checksum'}
    events = body.get('events')
    if (body.get('schema_version') != 1 or body.get('target_event_id') != target_id
            or not body.get('site_key') or receipt.get('checksum') != _digest(body)
            or not isinstance(events, list) or not 0 < len(events) <= MAX_RECEIPT_EVENTS
            or len(json.dumps(receipt).encode()) > MAX_RECEIPT_BYTES):
        return None
    seen, previous = set(), None
    for event in events:
        if (not isinstance(event, dict) or not event.get('id') or event['id'] in seen
                or event.get('event_type') not in PAGE_TYPES or not _url(event.get('url'))
                or type(event.get('sequence')) is not int or event['sequence'] <= 0
                or not _stamp(event.get('observed_at'))):
            return None
        if previous and (event.get('parent_event_id') != previous['id']
                or event['sequence'] <= previous['sequence']
                or _stamp(event['observed_at']) < _stamp(previous['observed_at'])):
            return None
        previous = event
        seen.add(event['id'])
    terminal = events[-1]
    if (terminal['id'] != target_id or terminal['event_type'] != 'page_inspection_finished'
            or terminal.get('state') != 'fetched' or not material_hash
            or terminal.get('content_hash') != material_hash
            or url not in {_url(terminal['url']), _url(terminal.get('final_url'))}):
        return None
    return deepcopy(events)


def record_configuration_saved(version, source, validation, *, writer):
    """Called after flush, before the caller's final fenced commit. No commit here."""
    from .tasks import current_execution
    from backend.scraper.discovery.parser_revision import PARSER_REVISION
    handle = current_execution() or {}
    # Take context from the server-owned executing task, not a caller's hint.
    context = (handle.get('payload') or {}).get('discovery_attention') or {}
    reference, region = validation.get('list_snapshot') or {}, validation.get('region') or {}
    material_hash = reference.get('hash')
    url = _url(source.list_url)
    receipt = context.get('inspection_receipt')
    target = context.get('inspection_event_id')
    ancestors = _receipt_events(receipt, target, material_hash, url)
    locator = region.get('container') or source.list_selector
    valid_parent = bool(ancestors and locator)
    reason = ('recorded' if valid_parent else 'material_changed' if context
              and material_hash != (context.get('material') or {}).get('content_hash')
              else 'invalid_page_receipt' if receipt else 'not_recorded')
    version.execution_json = dict(schema_version=1, recorder_version=1, parser_revision=PARSER_REVISION,
        id=f'source-config:{version.id}',
        event_type='source_configuration_saved', source_id=source.id, school_id=source.school_id,
        config_version_id=version.id, config_hash=version.config_hash,
        observed_at=datetime.now(timezone.utc).isoformat(timespec='microseconds'),
        writer=writer, task_id=handle.get('id'), task_generation=handle.get('generation'),
        url=url, label=source.name, parent_event_id=target if valid_parent else None,
        upstream_status=reason, page_receipt=deepcopy(receipt) if valid_parent else None,
        material_hash=material_hash, locator=locator, establishes_ownership=False)
    from backend.ai.material_capture import record_native
    record_native(version, 'source_configuration', version.execution_json)


def configuration_events(rows, facts):
    """Validate execution receipts independently of the current source state."""
    from .exploration_events import public_events
    sources = {node['source_id']: node for node in facts['nodes'] if node['entity_type'] == 'source'}
    rows = list(rows)
    represented = {row['department_id'] for row in rows}
    events, pages, gaps = [], [], dict(legacy_source_configurations=0, invalid_source_receipts=0,
        configurations_without_version=sum(node.get('configured', False) and ident not in represented
            for ident, node in sources.items()), configurations_with_upstream_gap=0,
        outdated_configuration_receipts=0)
    for row in rows:
        record = row.get('execution_json')
        if record is None:
            gaps['legacy_source_configurations'] += 1
            continue
        source = sources.get(row['department_id'])
        try:
            config = json.loads(row['config_json'])
            if not isinstance(config, dict):
                raise ValueError('Invalid saved configuration')
            config_hash = _digest(config)
        except (TypeError, ValueError):
            config, config_hash = {}, None
        if (not source or not isinstance(record, dict) or record.get('schema_version') != 1
                or record.get('event_type') != 'source_configuration_saved'
                or record.get('id') != f"source-config:{row['id']}"
                or record.get('source_id') != row['department_id']
                or record.get('school_id') != row.get('school_id')
                or record.get('config_version_id') != row['id']
                or record.get('config_hash') != row['config_hash'] or config_hash != row['config_hash']
                or not _stamp(record.get('observed_at')) or not _url(record.get('url'))
                or _url(record.get('url')) != _url(config.get('list_url'))):
            gaps['invalid_source_receipts'] += 1
            continue
        receipt, parent = record.get('page_receipt'), record.get('parent_event_id')
        ancestry = _receipt_events(receipt, parent, record.get('material_hash'), _url(record['url'])) if parent else None
        if ancestry and (receipt['site_key'] != facts['site_key']
                or _stamp(ancestry[-1]['observed_at']) > _stamp(record['observed_at'])):
            ancestry = None
        event = {key: record.get(key) for key in ('id', 'event_type', 'observed_at', 'label',
            'task_id', 'task_generation', 'config_hash', 'config_version_id', 'source_id',
            'writer', 'upstream_status', 'recorder_version', 'parser_revision')}
        event.update(sequence=row['id'], stream_id='source_config_versions', node_id=source['id'],
            url=_url(record['url']), parent_event_id=parent if ancestry else None,
            current_configuration=source['config_hash'] == config_hash,
            establishes_ownership=False, scope='configuration_save_not_website_click',
            evidence=[dict(kind='source_configuration_execution', reference_url=_url(record['url']),
                locator=record.get('locator'), content_hash=record.get('material_hash'),
                record_id=f"source_config_versions:{row['id']}", config_hash=config_hash)],
            cost=None, cost_source='not_measured')
        if parent and not ancestry:
            event['upstream_status'] = 'invalid_page_receipt'
        if not ancestry:
            gaps['configurations_with_upstream_gap'] += 1
        if not event['current_configuration']:
            gaps['outdated_configuration_receipts'] += 1
        if ancestry:
            pages.extend(public_events(ancestry, facts, allow_historical=True))
        events.append(event)
    return events, pages, gaps


def merge_execution_events(page_events, configuration_rows, facts):
    """Same snapshot in, bounded history out; historical links add no facts."""
    from .exploration_events import public_events
    current = public_events(page_events, facts)
    sources, retained, gaps = configuration_events(configuration_rows, facts)
    by_id, conflicts = {}, set()
    for event in [*current, *retained, *sources]:
        ident = event['id']
        if ident in by_id and by_id[ident] != event:
            conflicts.add(ident)
        by_id[ident] = event
    for ident in conflicts:
        by_id.pop(ident, None)
    ordered = sorted(by_id.values(), key=lambda event: (event['observed_at'], event['stream_id'], event['sequence']))
    # Configuration receipts are individually bounded; bound the shared API as
    # well, and expose every omission. Never reconnect across a lost ancestor.
    maximum = 8000
    output = ordered[-maximum:]
    return output, dict(gaps, omitted_page_events=len(page_events) - len(current),
        configuration_recording_scope='latest_saved_configuration_per_source',
        conflicting_event_ids=sorted(conflicts), maximum_projected_events=maximum,
        projected_window_truncated=len(ordered) > maximum,
        retained_page_receipt_events=len({event['id'] for event in retained}),
        sequence_contract='per_stream_explicit_parent_references')
