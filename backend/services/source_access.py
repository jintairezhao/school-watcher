"""Attributed entry-response records, independent of source admission and ownership."""
from datetime import datetime, timezone
import json
import re


def _time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and ('T' in value or ' ' in value):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def _state(status, outcome, code):
    code_status = int(code[5:]) if re.fullmatch(r'http_[1-5]\d\d', code) else None
    if status is not None and code_status is not None and status != code_status:
        return 'unknown'
    response = status if status is not None else code_status
    if response in (404, 410):
        return 'not_found'
    if response in (401, 403, 451):
        return 'restricted'
    if response in (408, 425, 429) or response is not None and response >= 500:
        return 'failed'
    if outcome in ('denied', 'needs_manual') or code in (
            'source_login_required', 'source_access_restricted', 'access_denied_page',
            'human_verification', 'access_challenge'):
        return 'restricted'
    if outcome in ('network_error', 'unavailable'):
        return 'failed'
    return 'responded' if status is not None and 200 <= status < 300 else 'unknown'


def project_access(configured_url, candidates, bound_pages, *, normalize):
    """Use only current-config candidates supplied by the shared fact reader.

    Missing HTTP metadata remains unknown even if extraction was validated.
    The observations describe their recorded times, never live availability.
    """
    result = dict(access='unknown', access_scope='entry_response', access_observed_at=None,
        access_observation=None, access_observations=[])
    if not configured_url:
        return result
    observations = {}
    for candidate in candidates:
        reference = candidate['reference']
        address = normalize(reference.get('url'))
        requested = normalize(reference.get('requested_url'))
        stamp = _time(reference.get('observed_at') or reference.get('captured_at'))
        status = reference.get('status')
        if (not address or stamp is None or not candidate.get('record_id')
                or requested and requested != configured_url
                or address != configured_url and requested != configured_url
                or status is not None and (type(status) is not int or not 100 <= status <= 599)
                or candidate.get('expected_hash') and reference.get('hash') != candidate['expected_hash']):
            continue
        outcome, code = reference.get('outcome') or '', reference.get('error_code') or ''
        if not isinstance(outcome, str) or not isinstance(code, str):
            continue
        state = _state(status, outcome, code)
        # A redirected success must identify the source's validated response
        # page. An unrelated login/listing response cannot become access proof.
        if address != configured_url and state == 'responded' and address not in bound_pages:
            continue
        observation = dict(kind=candidate['kind'], record_id=candidate['record_id'],
            reference_url=address, requested_url=requested or None, observed_at=stamp.isoformat(),
            status_code=status, outcome=outcome, state=state,
            content_hash=reference.get('hash') or None)
        key = (observation['record_id'], address, observation['observed_at'], status,
               observation['state'], observation['content_hash'])
        observations.setdefault(key, observation)
    values = sorted(observations.values(), key=lambda row: (row['observed_at'], row['record_id'],
        row['reference_url'], json.dumps(row, sort_keys=True)), reverse=True)
    if not values:
        return result
    stamp = values[0]['observed_at']
    latest = [row for row in values if row['observed_at'] == stamp]
    states = {row['state'] for row in latest}
    result.update(access=next(iter(states)) if len(states) == 1 else 'conflicting',
        access_observed_at=stamp, access_observation=latest[0] if len(states) == 1 else None,
        access_observations=values)
    return result
