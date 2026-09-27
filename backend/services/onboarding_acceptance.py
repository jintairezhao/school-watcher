"""Frozen school denominator and honest acceptance reports, including unsubscribed schools."""
from datetime import datetime, timezone
import hashlib
import json

from backend.database.models import School
from backend.services.catalog import catalog_entries, normalize_name
from backend.services.school_registry import registry_key, stable_registry_key, registered_school


def freeze_scope():
    entries = {}
    for item in catalog_entries():
        bound = registered_school(item['name'])
        key = stable_registry_key(bound) if bound else registry_key(item['name'])
        entries[key] = {'registry_key': key, 'name': item['name'], 'root_url': item['url'], 'origin': 'catalog'}
    for school in School.query.order_by(School.id):
        key = stable_registry_key(school)
        if key not in entries:
            entries[key] = {'registry_key': key, 'name': normalize_name(school.name),
                            'root_url': school.url, 'origin': 'registered'}
    rows = sorted(entries.values(), key=lambda row: row['registry_key'])
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'scope_digest': digest, 'schools': rows}


def validate_scope(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1:
        raise ValueError('名单版本不受支持')
    rows = snapshot.get('schools')
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError('名单内容无效')
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != snapshot.get('scope_digest'):
        raise ValueError('名单内容与固定版本不一致')
    identities = [row['registry_key'] for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError('名单包含重复身份')
    return rows


def acceptance_report(snapshot):
    from backend.database.db import db
    from backend.database.source_governance_models import SourceConfigVersion
    from backend.database.models import Department
    from backend.services.source_governance import school_governance_status
    schools = {}
    for school in School.query.all():
        schools.setdefault(stable_registry_key(school), []).append(school)
    items = []
    for item in validate_scope(snapshot):
        candidates = schools.get(item['registry_key'], [])
        status = {'state': 'not_registered', 'coverage_verified': False, 'official_units': [],
                  'pending_pages': 0, 'checked_pages': 0, 'proposals': {}}
        verified_columns = 0
        if len(candidates) == 1:
            school = candidates[0]
            status = school_governance_status(school.id)
            verified_columns = db.session.query(SourceConfigVersion.department_id).join(
                Department, Department.id == SourceConfigVersion.department_id).filter(
                    Department.school_id == school.id).distinct().count()
        elif candidates:
            status['state'] = 'identity_conflict'
        units = status['official_units']
        items.append(dict(item, **status, official_units_total=len(units),
                          verified_columns=verified_columns,
                          subscribed=any(s.subscriber_count for s in candidates)))
    ready = sum(bool(item['coverage_verified']) for item in items)
    return {'schema_version': 1, 'scope_digest': snapshot['scope_digest'],
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'total_schools': len(items), 'ready_schools': ready,
            'all_schools_ready': ready == len(items), 'schools': items}


def enqueue_scope(snapshot):
    from backend.services.school_registry import ensure_school
    from backend.services.source_inventory import site_key
    from backend.services.tasks import enqueue
    results = []
    for item in validate_scope(snapshot):
        school, _ = ensure_school(item['name'], item['root_url'], origin=item['origin'])
        task = enqueue('discover', school.id, {'school_id': school.id,
                       'name': school.name, 'root_url': school.url}, replace_finished=False)
        results.append({'school_id': school.id, 'task_id': task.id})
    return results
