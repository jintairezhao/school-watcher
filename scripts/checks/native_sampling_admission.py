"""Read-only sampling admission and explicit, evidence-bounded cache protection.

This is execution tooling, not a crawling strategy or threshold evaluator.
Known request envelopes diagnose reservation capacity; they do not promise that
unseen future requests or the whole approved call allowance will fit.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

CACHE_KEY = 'official_http_route_memory'
CACHE_BASIS = 'observed_proxy_failure_then_direct_http_success'
CACHE_TTL = 600
MAX_CACHE_ENTRIES = 128


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_budget_admission(connection, scope, root, used_calls, *, stamp=None):
    calls = scope.get('model', {}).get('maximum_new_dispatch_attempts')
    if calls is None:
        calls = max(0, scope.get('model', {}).get('maximum_dispatch_attempts', used_calls) - used_calls)
    if type(calls) is not int or calls < 0:
        raise ValueError('Invalid new model allowance')
    if not calls:
        return dict(status='no_new_models', ready=True, lanes=[], known_requests=[],
                    planned_call_count_guaranteed=False, approved_maximum_calls=0)
    month = (stamp or datetime.now(timezone.utc)).strftime('%Y-%m')
    selected = scope.get('continuation_school_id')
    fresh_school = scope.get('new_school_in_existing_instance') is True and selected is None
    round_row = connection.execute('select budget_key from ai_school_rounds where school_id=?', (selected,)).fetchone()
    names = [('total:' + month, 'ai_token_limit_total'), ('directory:' + month, 'ai_token_limit_directory')]
    if round_row:
        names.append((round_row[0], 'ai_token_limit_school_discovery'))
    lanes, missing = [], []
    for key, setting in names:
        config = connection.execute('select value from app_config where key=?', (setting,)).fetchone()
        limit = int(config[0]) if config else None
        if limit is None:
            missing.append(setting)
        if limit is not None and limit < 0:
            raise ValueError('Negative token limit')
        row = connection.execute('select used_tokens,reserved_tokens,active_count,used_requests,reserved_requests '
                                 'from ai_budgets where key=?', (key,)).fetchone()
        used, reserved, active, requests, reserved_requests = tuple(row) if row else (0, 0, 0, 0, 0)
        lanes.append(dict(key=key, limit_tokens=limit, used_tokens=used, reserved_tokens=reserved,
                          active_count=active, used_requests=requests, reserved_requests=reserved_requests,
                          remaining_tokens=None if limit in (None, 0) else max(0, limit - used - reserved)))
    prospective_school_limit = None
    if not round_row and fresh_school:
        config = connection.execute('select value from app_config where key=?', ('ai_token_limit_school_discovery',)).fetchone()
        if config is None:
            missing.append('new_school_token_limit')
        else:
            prospective_school_limit = int(config[0])
            if prospective_school_limit < 0:
                raise ValueError('Negative new-school token limit')
    elif not round_row:
        missing.append('school_round')
    request_limit = scope.get('native_school_request_limit')
    if request_limit is None:
        row = connection.execute('select value from app_config where key=?', ('ai_request_limit_school_discovery',)).fetchone()
        request_limit = int(row[0]) if row else None
    if request_limit is None:
        missing.append('school_request_limit')
    known = []
    for reference in scope.get('model_admission_request_refs', []):
        path = (Path(root) / reference['path']).resolve()
        if not path.is_relative_to((Path(root) / '.local/structure-facts-20261021').resolve()):
            raise ValueError('Request envelope must be a retained isolated record')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('Retained request envelope changed')
        event = json.loads(raw)
        if event.get('event') != 'provider_request':
            raise ValueError('Expected a recorded provider request')
        body = event['payload']['body']
        if body['model'] != scope['model']['model']:
            raise ValueError('Known request model differs from proposed model')
        messages, output = body['messages'], body['max_tokens']
        if (type(output) is not int or not 0 < output <= 16384 or not isinstance(messages, list) or not messages
                or any(not isinstance(m, dict) or not isinstance(m.get('content'), str) for m in messages)):
            raise ValueError('Invalid known request envelope')
        evidence_school = None
        for message in messages:
            if message.get('role') == 'user' and '{' in message['content']:
                try:
                    evidence_school = json.loads(message['content'][message['content'].index('{'):]).get('school_id')
                except (ValueError, AttributeError):
                    continue
                if evidence_school is not None:
                    break
        if selected is None or evidence_school != selected:
            raise ValueError('Known request school differs from proposed continuation')
        # Match backend.ai.runtime._execute, including its conservative byte reservation.
        reservation = len(canonical(messages).encode()) + output
        blocked = [lane['key'] for lane in lanes if lane['remaining_tokens'] is not None
                   and reservation > lane['remaining_tokens']]
        if round_row and request_limit and lanes[-1]['used_requests'] + lanes[-1]['reserved_requests'] >= request_limit:
            blocked.append('school_request_count')
        known.append(dict(path=reference['path'], sha256=reference['sha256'],
                          reservation_tokens=reservation, blocked_by=blocked, fits=not blocked))
    unsettled = any(lane['active_count'] or lane['reserved_tokens'] or lane['reserved_requests'] for lane in lanes)
    if missing:
        status = 'unknown_budget_settings'
    elif fresh_school and not known:
        if unsettled:
            status = 'unsettled_model_resources'
        elif any(lane['remaining_tokens'] is not None and lane['remaining_tokens'] <= 0 for lane in lanes):
            status = 'new_school_no_monthly_headroom'
        else:
            # Initialization creates the school round. A first request cannot
            # be constructed or admitted yet; _execute must still reserve its
            # real bytes and output allowance before any provider dispatch.
            status = 'fresh_school_requires_live_request_check'
    elif not known:
        status = 'unknown_request_envelopes'
    elif unsettled:
        status = 'unsettled_model_resources'
    elif all(row['fits'] for row in known):
        status = 'known_requests_fit'
    elif any(row['fits'] for row in known):
        status = 'limited_known_requests'
    else:
        status = 'known_requests_blocked'
    return dict(status=status, ready=status == 'known_requests_fit', lanes=lanes, known_requests=known,
                bootstrap_ready=status == 'fresh_school_requires_live_request_check',
                actual_request_reservation_required=True, prospective_school_token_limit=prospective_school_limit,
                missing=missing, school_request_limit=request_limit, approved_maximum_calls=calls,
                planned_call_count_guaranteed=False, future_requests='unknown_until_built',
                reservation_basis='canonical_messages_utf8_bytes_plus_output_allowance',
                changes_to_limits_or_usage=0)


def _cache_value(raw):
    if not isinstance(raw, str) or len(raw) >= 65536:
        raise ValueError('Invalid route cache length')
    value = json.loads(raw)
    if not isinstance(value, dict) or len(value) > MAX_CACHE_ENTRIES:
        raise ValueError('Invalid route cache shape')
    for key, row in value.items():
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid route fingerprint')
        if not isinstance(row, dict) or set(row) != {'verified_at', 'expires_at', 'basis'}:
            raise ValueError('Unexpected route cache fields')
        if row['basis'] != CACHE_BASIS:
            raise ValueError('Unknown route cache basis')
        if any(type(row[k]) not in (int, float) or not math.isfinite(row[k]) for k in ('verified_at', 'expires_at')):
            raise ValueError('Invalid route cache clock')
        if abs(row['expires_at'] - row['verified_at'] - CACHE_TTL) > 0.001:
            raise ValueError('Unexpected route cache lifetime')
    return value


def protect_route_cache(connection, tables, context):
    """Version2 certificate; only this named transport cache becomes mutable."""
    tables = json.loads(canonical(tables))
    row = connection.execute('select id,key,value from app_config where key=?', (CACHE_KEY,)).fetchone()
    before = dict(zip(('id', 'key', 'value'), row)) if row else None
    if before:
        _cache_value(before['value'])
        tables['app_config']['rows_sha256'].pop(canonical([before['id']]), None)
    return dict(schema_version=2, tables=tables,
                runtime_route_cache=dict(before=before, context=context,
                                         policy='successful_scoped_direct_hint_or_expired_cleanup_v1'))


def _stamp(value):
    when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise ValueError('Route observation needs a timezone')
    return when.timestamp()


def _allowed(host, context):
    domain = context.get('allowed_school_domain')
    return bool(host and (domain and (host == domain or host.endswith('.' + domain))
                         or host in context.get('allowed_hosts', [])))


def route_success_observations(connection, context, ledger):
    """Use scoped task fingerprints and actual successful HTTP document windows."""
    documents = ledger['documents'][context['initial_documents']:]
    attempts = ledger['http_attempts'][context['initial_http_attempts']:]
    by_document = defaultdict(list)
    for attempt in attempts:
        if attempt.get('status') == 200 and _allowed(attempt.get('host'), context):
            by_document[attempt.get('document')].append(attempt)
    observed, windows = defaultdict(list), []
    for document in documents:
        started = _stamp(document['started_at'])
        windows.append(started)
        if not document.get('finished_at'):
            # Admission can stop between recording a document and receiving
            # its complete response. A partial HTTP200 is not a success proof.
            continue
        finished = _stamp(document['finished_at'])
        windows.extend([started, finished])
        task_id = document['task_id']
        if task_id not in context['admitted_task_ids'] and task_id <= context['existing_max_task_id']:
            continue
        row = connection.execute('select payload,checkpoint from background_tasks where id=?', (task_id,)).fetchone()
        if not row or json.loads(row[0]).get('school_id') != context['school_id']:
            continue
        fingerprints = json.loads(row[1]).get('http_failed_proxy_fingerprints', {})
        for attempt in by_document[document['sequence']]:
            key = fingerprints.get(attempt['host'])
            if key:
                observed[key].append((started, finished))
    return dict(observed), max(windows) if windows else None


def verify_route_cache(before, after, observed, ended_at):
    """No blanket config whitelist: every changed hint needs a scoped success."""
    errors, changes = [], []
    if before and (not after or before['id'] != after['id'] or after['key'] != CACHE_KEY):
        return ['route_cache_identity_changed'], []
    if after and after['key'] != CACHE_KEY:
        return ['route_cache_identity_changed'], []
    try:
        old = _cache_value(before['value']) if before else {}
        new = _cache_value(after['value']) if after else {}
    except (ValueError, TypeError, KeyError):
        return ['invalid_route_cache'], []
    for key in sorted(old.keys() | new.keys()):
        if old.get(key) == new.get(key):
            continue
        if key not in new:
            allowed = ended_at is not None and old[key]['expires_at'] <= ended_at
            reason = 'expired_hint_removed' if allowed else 'live_hint_removed'
            if (not allowed and len(new) == MAX_CACHE_ENTRIES
                    and all(row['verified_at'] > old[key]['verified_at'] for row in new.values())):
                allowed, reason = True, 'oldest_hint_evicted_at_runtime_capacity'
        else:
            verified = new[key]['verified_at']
            allowed = any(start <= verified <= end for start, end in observed.get(key, []))
            reason = 'scoped_success_hint_recorded' if allowed else 'hint_without_scoped_success'
        changes.append(dict(fingerprint=key, change=reason, allowed=allowed))
        if not allowed:
            errors.append(reason)
    return errors, changes


def verify_row_protection(connection, certificate, *, ledger=None):
    modern = certificate.get('schema_version') == 2
    tables = certificate['tables'] if modern else certificate
    errors = []
    for table, record in tables.items():
        where = ' and '.join('"' + key + '"=?' for key in record['primary_key'])
        for identity, expected in record['rows_sha256'].items():
            row = connection.execute('select * from "' + table + '" where ' + where, json.loads(identity)).fetchone()
            value = dict(zip([column[0] for column in connection.execute('select * from "' + table + '" limit 0').description], row)) if row else None
            if value is None or fingerprint(value) != expected:
                errors.append(dict(table=table, identity=identity))
    changes = []
    if modern:
        declaration = certificate['runtime_route_cache']
        if declaration['policy'] != 'successful_scoped_direct_hint_or_expired_cleanup_v1' or ledger is None:
            raise ValueError('Explicit route-cache context and ledger are required')
        row = connection.execute('select id,key,value from app_config where key=?', (CACHE_KEY,)).fetchone()
        after = dict(zip(('id', 'key', 'value'), row)) if row else None
        observations, ended = route_success_observations(connection, declaration['context'], ledger)
        cache_errors, changes = verify_route_cache(declaration['before'], after, observations, ended)
        errors.extend(dict(table='app_config', identity=CACHE_KEY, reason=reason) for reason in cache_errors)
    return dict(errors=errors, runtime_cache_changes=changes,
                legacy_certificate_strict=not modern, all_protected_invariants_passed=not errors)
