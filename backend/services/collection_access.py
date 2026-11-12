"""Publish attributed entry observations, never collection success or ownership."""
from datetime import timezone
import json

from sqlalchemy import inspect

from backend.database.db import db
from backend.database.models import Department
from backend.database.structure_version_models import SourceAccessObservation
from backend.services.source_access import _time
from backend.services.source_governance import _hash, source_config
from backend.services.structure_facts import _url


def _available():
    # Old, deliberately unmigrated read-only cases remain readable. Reading a
    # structure snapshot must never create or migrate their schema.
    return inspect(db.session.connection()).has_table(SourceAccessObservation.__tablename__)


def save_entry_observation(context, request, result, *, replay_ref=None):
    from backend.services import tasks
    handle = tasks.current_execution() or {}
    stamp = _time(result.observed_at)
    config = context.get('config') or {}
    requested, final = _url(request.url), _url(result.final_url)
    task_kind = context.get('task_kind', 'collect')
    if (task_kind not in ('collect', 'source_health') or handle.get('kind') != task_kind
            or context.get('task_id') != handle.get('id')
            or context.get('task_generation') != handle.get('generation')
            or request.purpose != 'list' or request.policy.get('embedded_pdf_child')
            or not requested or not final or requested != _url(config.get('list_url'))
            or stamp is None or context.get('config_hash') != _hash(config)
            or request.source_id and str(request.source_id) != str(context.get('source_id'))
            or len(requested) > 4096 or len(final) > 4096 or not _available()):
        return False
    tasks.assert_owned(handle)
    # Hold the source configuration stable through the fenced task commit.
    source = db.session.execute(db.select(Department).where(Department.id == context['source_id'])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (source is None or source.school_id != context.get('school_id')
            or source.kind != 'column' or _hash(source_config(source)) != context['config_hash']):
        return False
    from hashlib import sha256
    reference = dict(url=final, requested_url=requested, observed_at=stamp.isoformat(),
        status=result.status if type(result.status) is int and 100 <= result.status <= 599 else None,
        outcome=result.outcome, error_code=result.error_code,
        hash=sha256(result.html.encode('utf-8')).hexdigest() if result.html else None)
    origin = 'health' if task_kind == 'source_health' else 'collection'
    record_id = origin + ':' + _hash([source.id, context['config_hash'], reference])
    candidate = dict(source_id=source.id, school_id=source.school_id, config_hash=context['config_hash'],
        kind=origin + '_entry_response', record_id=record_id, reference=reference,
        task_id=handle['id'], task_generation=handle['generation'],
        log_id=None if replay_ref else context['log_id'], cache_file=replay_ref)
    row = db.session.execute(db.select(SourceAccessObservation).where(SourceAccessObservation.source_id == source.id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    timestamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    payload = dict(records=[], overflow=False)
    if row is not None and row.config_hash == context['config_hash']:
        if row.observed_at > timestamp:
            return False
        if row.observed_at == timestamp:
            payload = json.loads(row.observation_json)
            if any(r['record_id'] == record_id for r in payload['records']) or payload.get('overflow'):
                return False
    if len(payload['records']) == 32:
        payload['overflow'] = True
    else:
        payload['records'].append(candidate)
    payload['records'].sort(key=lambda r:r['record_id'])
    if row is None:
        row = SourceAccessObservation(source_id=source.id)
        db.session.add(row)
    row.school_id, row.config_hash = source.school_id, context['config_hash']
    row.observed_at, row.observation_json = timestamp, json.dumps(payload,ensure_ascii=False,sort_keys=True)
    # Both the observation and its structure revision commit only while this
    # task still owns its generation/token. A later parser failure does not
    # erase an HTTP observation that genuinely already happened.
    tasks.checkpoint(dict(handle.get('checkpoint') or {}))
    return True


def read_entry_observations(school_id):
    if not _available():
        return []
    rows = db.session.execute(db.select(SourceAccessObservation).where(
        SourceAccessObservation.school_id == school_id).execution_options(populate_existing=True)).scalars()
    records = []
    for row in rows:
        try:
            payload = json.loads(row.observation_json)
            candidates = payload['records']
            if not isinstance(candidates,list) or len(candidates)>32:
                continue
            for candidate in candidates:
                if (candidate.get('source_id') == row.source_id and candidate.get('school_id') == row.school_id
                        and candidate.get('config_hash') == row.config_hash
                        and _time(candidate.get('reference',{}).get('observed_at')) == _time(row.observed_at)):
                    records.append(candidate)
            if payload.get('overflow') and candidates:
                # Do not hide an unrepresented same-time result behind success.
                address = candidates[0]['reference']['requested_url']
                records.append(dict(source_id=row.source_id,school_id=row.school_id,config_hash=row.config_hash,
                    kind='collection_entry_overflow',record_id=f'collection:overflow:{row.source_id}',
                    reference=dict(url=address,requested_url=address,observed_at=row.observed_at.isoformat())))
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
    return records
