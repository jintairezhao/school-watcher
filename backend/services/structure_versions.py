"""Commit structure mutations and projection cursors in the same transaction.

ORM changes are compared against their first persisted state in the transaction,
so repeated flushes and edits subsequently undone do not invent new versions.
Core writers must explicitly mark actual successful mutations. The journal is an
invalidation log of identities/fields, not a second store of ownership facts.
"""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from backend.database.db import db
from backend.database.models import School, Department, DepartmentDirectoryEntry
from backend.database.school_registry_models import SchoolRegistryEntry
from backend.database.source_governance_models import SourceProposal, SourceValidation, SourceConfigVersion
from backend.database.structure_version_models import StructureRevision, StructureChange, SourceAccessObservation
from backend.database.source_lifecycle_models import SourceLifecycle

_KEY = 'structure_version_transaction'
_MODELS = {
    'school': (School, ('name', 'url', 'enabled', 'config')),
    'department': (Department, ('school_id', 'name', 'kind', 'structure_key', 'identity_json',
        'list_url', 'list_selector', 'title_selector', 'link_selector',
        'date_selector', 'content_selector', 'group_name')),
    'directory_entry': (DepartmentDirectoryEntry, ('parent_id', 'department_id', 'position', 'facts_json')),
    'proposal': (SourceProposal, ('school_id', 'department_id', 'identity_key',
        'proposal_key', 'origin', 'state', 'candidate_json', 'expected_config_hash',
        'evidence_json', 'evidence_hash', 'validation_json', 'validator_version', 'validated_hash')),
    'validation': (SourceValidation, ('school_id', 'department_id', 'config_hash', 'validation_json')),
    'source_config_version': (SourceConfigVersion, ('department_id', 'proposal_id', 'version',
        'config_json', 'config_hash', 'previous_json', 'execution_json')),
    'school_registry': (SchoolRegistryEntry, ('school_id', 'canonical_name', 'root_url', 'aliases')),
    'source_lifecycle': (SourceLifecycle, ('school_id', 'state')),
    'source_access': (SourceAccessObservation, ('school_id', 'config_hash', 'observed_at', 'observation_json')),
}


def _session(session):
    return session if session is not None else db.session


def _state(session):
    return session.info.setdefault(_KEY, {'baseline': {}, 'explicit': {}, 'resets': {}, 'new': []})


def _columns(kind):
    model, names = _MODELS[kind]
    table = model.__table__
    return list(dict.fromkeys([c.name for c in table.primary_key] + [n for n in names if n in table.c]))


def _row(session, kind, identity):
    table = _MODELS[kind][0].__table__
    result = session.connection().execute(select(*[table.c[n] for n in _columns(kind)]).where(
        *[column == value for column, value in zip(table.primary_key, identity)])).mappings().first()
    return deepcopy(dict(result)) if result is not None else None


def _school(session, kind, row):
    if row is None:
        return None
    if kind == 'school':
        return row['id']
    if kind in ('directory_entry', 'source_config_version'):
        return session.connection().execute(select(Department.school_id).where(
            Department.id == row['parent_id' if kind == 'directory_entry' else 'department_id'])).scalar_one_or_none()
    return row.get('school_id')


def mark_structure_changed(school_id, entity_type, entity_id, operation='upsert', fields=(), *, session=None):
    """Register an actual Core mutation; caller owns commit/rollback.

    Call only after a semantic change (for guarded updates, check rowcount).
    Repeated marks merge into one identity and one revision per school.
    """
    if operation not in ('upsert', 'delete'):
        raise ValueError('Unsupported structure change operation')
    if not school_id:
        raise ValueError('A structure mutation requires a school identity')
    session = _session(session)
    # Begin a transaction even if this is the first operation, so savepoint and
    # rollback lifetimes also apply to explicit registrations.
    session.connection()
    key = (int(school_id), str(entity_type), str(entity_id))
    change = _state(session)['explicit'].setdefault(key, {'operation': operation, 'fields': set()})
    change['operation'] = operation
    change['fields'].update(fields)


def reset_epoch(school_id, reason, *, session=None):
    """Invalidate old cursors after an authorized restore, in its DB transaction.

    This does not restore a backup or commit. Whole-database replacement entry
    points call it when replacement is ready; additive imports keep their epoch.
    """
    if not school_id or not str(reason).strip():
        raise ValueError('An epoch reset requires a school and a reason')
    session = _session(session)
    session.connection()
    _state(session)['resets'][int(school_id)] = str(reason).strip()[:200]


def reset_restored_structure_epochs(connection, reason):
    """Fence cursors in an offline restored/moved target, without committing.

    This connection belongs to the target, never the running application's
    session. Preserve facts, reader state and old journal entries. Old backups
    without version tables receive fresh epochs when migration 27 runs; creating
    only those tables here would conflict with the pending migration.
    """
    reason = str(reason).strip()
    if not reason:
        raise ValueError('An epoch reset requires a reason')
    names = set(inspect(connection).get_table_names())
    version_tables = {'structure_revisions', 'structure_changes'}
    present = names & version_tables
    if not present:
        return {'schools_reset': 0, 'deferred_to_migration': True}
    if present != version_tables or School.__tablename__ not in names:
        raise ValueError('Restored structure version schema is incomplete')
    if connection.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    elif connection.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise RuntimeError('Unsupported structure version dialect')
    table = StructureRevision.__table__
    school_ids = set(connection.execute(select(School.id)).scalars())
    # A deleted school's retained cursor must also reject its pre-restore client.
    school_ids.update(connection.execute(select(table.c.school_id)).scalars())
    for school_id in sorted(school_ids):
        epoch, stamp = uuid4().hex, datetime.utcnow()
        connection.execute(insert(table).values(school_id=school_id, epoch=epoch,
            revision=1, updated_at=stamp).on_conflict_do_update(index_elements=['school_id'],
                set_={'epoch': epoch, 'revision': 1, 'updated_at': stamp}))
        connection.execute(StructureChange.__table__.insert().values(school_id=school_id,
            epoch=epoch, revision=1, created_at=stamp, changes=[dict(entity_type='epoch',
                entity_id=str(school_id), operation='reset', fields=[], reason=reason[:200])]))
    return {'schools_reset': len(school_ids), 'deferred_to_migration': False}


def current_version(school_id, *, session=None):
    session = _session(session)
    with session.no_autoflush:
        row = session.execute(select(StructureRevision.epoch, StructureRevision.revision).where(
            StructureRevision.school_id == school_id)).first()
    return {'epoch': row.epoch if row else None, 'revision': row.revision if row else 0}


def changes_since(school_id, epoch, revision, *, limit=200, session=None):
    session = _session(session)
    current = current_version(school_id, session=session)
    result = dict(epoch=current['epoch'], from_revision=revision,
                  to_revision=current['revision'], changes=[], reset_required=False)
    if (epoch != current['epoch'] or not isinstance(revision, int) or revision < 0
            or revision > current['revision'] or limit < 1):
        result['reset_required'] = True
        return result
    if revision == current['revision']:
        return result
    with session.no_autoflush:
        rows = session.execute(select(StructureChange.revision, StructureChange.changes).where(
            StructureChange.school_id == school_id, StructureChange.epoch == epoch,
            StructureChange.revision > revision,
            StructureChange.revision <= current['revision']).order_by(StructureChange.revision).limit(limit + 1)).all()
    expected = current['revision'] - revision
    if len(rows) != expected or len(rows) > limit or any(r.revision != revision + i + 1 for i, r in enumerate(rows)):
        result['reset_required'] = True
    else:
        result['changes'] = [{'revision': r.revision, 'changes': deepcopy(r.changes)} for r in rows]
    return result


@event.listens_for(Session, 'before_flush')
def _capture_changes(session, flush_context, instances):
    kinds = {model: kind for kind, (model, _) in _MODELS.items()}
    for obj in list(session.deleted) + list(session.dirty) + list(session.new):
        kind = kinds.get(type(obj))
        if kind is None:
            continue
        inspection = inspect(obj)
        if obj not in session.new and obj not in session.deleted:
            relevant = any(inspection.attrs[name].history.has_changes() for name in _columns(kind))
            if not relevant and not (kind == 'department' and inspection.attrs.school.history.has_changes()):
                continue
        state = _state(session)
        if obj in session.new:
            state['new'].append((kind, obj))
            continue
        identity = inspection.identity
        key = (kind, identity)
        if key not in state['baseline']:
            before = _row(session, kind, identity)
            state['baseline'][key] = (before, _school(session, kind, before))


@event.listens_for(Session, 'after_flush_postexec')
def _capture_inserted_identities(session, flush_context):
    state = session.info.get(_KEY)
    if state is None:
        return
    for kind, obj in state['new']:
        identity = inspect(obj).identity
        if identity is not None:
            state['baseline'].setdefault((kind, identity), (None, None))
    state['new'] = []


def _net_changes(session, state):
    changes = deepcopy(state['explicit'])
    for (kind, identity), (before, school_before) in state['baseline'].items():
        after = _row(session, kind, identity)
        if before == after:
            continue
        if kind == 'source_lifecycle' and (before or {}).get('state', 'active') == (after or {}).get('state', 'active'):
            continue
        school_after = _school(session, kind, after)
        fields = {n for n in _MODELS[kind][1] if (before or {}).get(n) != (after or {}).get(n)}
        ident = ':'.join(str(v) for v in identity)
        for school_id in {school_before, school_after} - {None}:
            key = (int(school_id), kind, ident)
            operation = 'delete' if after is None or school_id != school_after else 'upsert'
            change = changes.setdefault(key, {'operation': operation, 'fields': set()})
            change['operation'] = operation
            change['fields'].update(fields)
    grouped = {}
    for (school_id, kind, ident), change in sorted(changes.items()):
        grouped.setdefault(school_id, []).append(dict(entity_type=kind, entity_id=ident,
            operation=change['operation'], fields=sorted(change['fields'])))
    for school_id, reason in state['resets'].items():
        grouped.setdefault(school_id, []).append(dict(entity_type='epoch', entity_id=str(school_id),
            operation='reset', fields=[], reason=reason))
    return grouped


@event.listens_for(Session, 'before_commit')
def _commit_versions(session):
    # Releasing a savepoint is not a published structure change.
    if session.in_nested_transaction():
        return
    session.flush()
    state = session.info.get(_KEY)
    if not state:
        return
    grouped = _net_changes(session, state)
    connection = session.connection()
    if connection.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    elif connection.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise RuntimeError('Unsupported structure version dialect')
    table = StructureRevision.__table__
    for school_id in sorted(grouped):
        stamp = datetime.utcnow()
        connection.execute(insert(table).values(school_id=school_id, epoch=uuid4().hex, revision=0,
            updated_at=stamp).on_conflict_do_nothing(index_elements=['school_id']))
        values = {'revision': table.c.revision + 1, 'updated_at': stamp}
        if school_id in state['resets']:
            values.update(epoch=uuid4().hex, revision=1)
        connection.execute(update(table).where(table.c.school_id == school_id).values(**values))
        row = connection.execute(select(table.c.epoch, table.c.revision).where(table.c.school_id == school_id)).one()
        connection.execute(StructureChange.__table__.insert().values(school_id=school_id,
            epoch=row.epoch, revision=row.revision, changes=grouped[school_id], created_at=stamp))


@event.listens_for(Session, 'after_transaction_create')
def _savepoint_state(session, transaction):
    if transaction.nested:
        transaction._structure_version_savepoint = deepcopy(session.info.get(_KEY))


@event.listens_for(Session, 'after_soft_rollback')
def _restore_savepoint_state(session, previous_transaction):
    if previous_transaction.nested:
        prior = getattr(previous_transaction, '_structure_version_savepoint', None)
        if prior is None:
            session.info.pop(_KEY, None)
        else:
            session.info[_KEY] = prior


@event.listens_for(Session, 'after_transaction_end')
def _clear_transaction_state(session, transaction):
    if transaction.parent is None:
        session.info.pop(_KEY, None)
