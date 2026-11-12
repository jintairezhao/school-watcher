"""Restore scoped token limits and park only this fresh school's test tasks."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

LIMIT_KEYS = frozenset(('ai_token_limit_total', 'ai_token_limit_directory'))


@contextmanager
def scope_lifecycle(instance, scope, output):
    """Restore the declared allowance even if a continuation child fails."""
    state = dict(child_confirmed_stopped=False)
    try:
        apply_temporary_limits(instance, scope, output)
        yield state
    finally:
        try:
            result = park_continuation_tasks(instance, scope,
                child_confirmed_stopped=state['child_confirmed_stopped'])
            _save(Path(output)/'admitted-window-closed.json', dict(result,
                passed=state['child_confirmed_stopped'] and not result['unresolved_owned_ids'],
                child_confirmed_stopped=state['child_confirmed_stopped'], automatic_restart=False))
        finally:
            _save(Path(output)/'temporary-limits-restored.json',
                  restore_temporary_limits(instance, scope, output))


def _save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _limits(scope):
    limits = scope['temporary_token_limits']
    if set(limits) != LIMIT_KEYS:
        raise ValueError('Only the two declared monthly token limits may change')
    for row in limits.values():
        if set(row) != {'before', 'during'} or not all(isinstance(row[k], str) and row[k].isdigit() for k in row):
            raise ValueError('Limits need exact before/during values')
        if not 0 < int(row['before']) <= int(row['during']):
            raise ValueError('A temporary allowance must be finite and cannot lower the existing limit')
    return limits


def apply_temporary_limits(instance, scope, output):
    limits = _limits(scope)
    marker = Path(output) / 'temporary-limit-intent.json'
    if marker.exists():
        raise ValueError('Inspect an existing limit intent; do not reapply')
    with closing(sqlite3.connect(Path(instance) / 'school_watcher.db')) as c:
        with c:
            c.execute('BEGIN IMMEDIATE')
            for key, row in limits.items():
                current = c.execute('SELECT value FROM app_config WHERE key=?', (key,)).fetchone()
                if current != (row['before'],):
                    raise ValueError('Monthly limit changed before scoped initialization')
            # The durable intent precedes the commit so a terminated child
            # cannot leave an unrecorded allowance increase behind.
            _save(marker, dict(limits=limits, written_before_commit=True))
            for key, row in limits.items():
                c.execute('UPDATE app_config SET value=? WHERE key=?', (row['during'], key))
    return dict(applied=True, limits=limits, used_or_reserved_usage_changed=False)


def restore_temporary_limits(instance, scope, output):
    limits = _limits(scope)
    marker = Path(output) / 'temporary-limit-intent.json'
    if not marker.exists():
        return dict(restored=False, reason='limits_never_applied', usage_reset=False)
    if json.loads(marker.read_text(encoding='utf-8'))['limits'] != limits:
        raise ValueError('Limit intent differs from approved scope')
    with closing(sqlite3.connect(Path(instance) / 'school_watcher.db')) as c:
        with c:
            c.execute('BEGIN IMMEDIATE')
            current = {key: c.execute('SELECT value FROM app_config WHERE key=?', (key,)).fetchone() for key in limits}
            if any(current[key] not in ((row['before'],), (row['during'],)) for key, row in limits.items()):
                raise ValueError('An external limit edit must not be overwritten')
            for key, row in limits.items():
                c.execute('UPDATE app_config SET value=? WHERE key=?', (row['before'], key))
    return dict(restored=True, limits={key: row['before'] for key, row in limits.items()}, usage_reset=False)


def park_fresh_school_tasks(instance, scope, *, child_confirmed_stopped):
    maximum_old_id = scope['existing_max_task_id']
    if type(maximum_old_id) is not int or maximum_old_id < 0:
        raise ValueError('An exact old-task boundary is required')
    with closing(sqlite3.connect(Path(instance) / 'school_watcher.db')) as c:
        c.row_factory = sqlite3.Row
        schools = c.execute('SELECT id FROM schools WHERE name=? AND url=?',
                            (scope['school']['name'], scope['school']['root_url'])).fetchall()
        if not schools:
            return dict(parked_ids=[], fenced_ids=[], unresolved_owned_ids=[], school_not_created=True)
        if len(schools) != 1:
            raise ValueError('Fresh school identity is ambiguous')
        school_id = schools[0]['id']
        delayed = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).isoformat(' ')
        parked, fenced, unresolved = [], [], []
        with c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT * FROM background_tasks WHERE id>? ORDER BY id', (maximum_old_id,)).fetchall():
                if str(json.loads(row['payload'] or '{}').get('school_id')) != str(school_id):
                    continue
                if row['state'] not in ('pending', 'waiting', 'running'):
                    continue
                owned = bool(row['token'] or row['worker_id'] or row['lease_until'])
                if owned and not (child_confirmed_stopped and row['worker_id'] == 'declared-native-pilot'):
                    unresolved.append(row['id'])
                    continue
                if owned:
                    c.execute("UPDATE background_tasks SET state='pending',token=NULL,worker_id=NULL,lease_until=NULL,available_at=? WHERE id=? AND generation=? AND worker_id='declared-native-pilot'",
                              (delayed, row['id'], row['generation']))
                    fenced.append(row['id'])
                else:
                    c.execute('UPDATE background_tasks SET available_at=? WHERE id=? AND generation=? AND token IS NULL AND worker_id IS NULL AND lease_until IS NULL',
                              (delayed, row['id'], row['generation']))
                parked.append(row['id'])
    return dict(parked_ids=parked, fenced_ids=fenced, unresolved_owned_ids=unresolved,
                task_ids_generations_and_checkpoints_preserved=True)


@contextmanager
def fresh_school_window(instance, scope, output):
    state = dict(child_confirmed_stopped=False)
    try:
        yield state
    finally:
        try:
            tasks = park_fresh_school_tasks(instance, scope, child_confirmed_stopped=state['child_confirmed_stopped'])
            _save(Path(output) / 'admitted-window-closed.json', dict(tasks, child_confirmed_stopped=state['child_confirmed_stopped'],
                passed=state['child_confirmed_stopped'] and not tasks['unresolved_owned_ids'], automatic_restart=False))
        finally:
            result = restore_temporary_limits(instance, scope, output)
            _save(Path(output) / 'temporary-limits-restored.json', result)


def park_reader_tasks(instance, scope, *, child_confirmed_stopped):
    """Close one approved reader and its new children, preserving other jobs."""
    targets = set(scope.get('resume_expired_task_ids', [])) | set(scope.get('resume_boundary_task_ids', []))
    if len(targets) != 1 or not scope.get('model_free_native_slice'):
        raise ValueError('A reader window needs one exact zero-model target')
    delayed = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).isoformat(' ')
    parked, fenced, unresolved = [], [], []
    with closing(sqlite3.connect(Path(instance) / 'school_watcher.db')) as c:
        c.row_factory = sqlite3.Row
        with c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT * FROM background_tasks ORDER BY id').fetchall():
                if row['id'] not in targets and row['id'] <= scope['existing_max_task_id']:
                    continue
                if str(json.loads(row['payload'] or '{}').get('school_id')) != str(scope['continuation_school_id']):
                    continue
                if row['state'] not in ('pending', 'waiting', 'running'):
                    continue
                owned = bool(row['token'] or row['worker_id'] or row['lease_until'])
                if owned and not (child_confirmed_stopped and row['worker_id'] == 'declared-native-pilot'):
                    unresolved.append(row['id'])
                    continue
                values = dict(available_at=delayed)
                if row['id'] in targets:
                    values['deadline_at'] = scope['original_target_deadline']
                if owned:
                    values.update(state='pending', token=None, worker_id=None, lease_until=None)
                    fenced.append(row['id'])
                names = ','.join(key + '=?' for key in values)
                c.execute('UPDATE background_tasks SET ' + names + ' WHERE id=? AND generation=?',
                          (*values.values(), row['id'], row['generation']))
                parked.append(row['id'])
    return dict(parked_ids=parked, fenced_ids=fenced, unresolved_owned_ids=unresolved,
                task_ids_generations_and_checkpoints_preserved=True, original_target_deadline_restored=True)


def park_continuation_tasks(instance, scope, *, child_confirmed_stopped):
    """Close one declared continuation, including after a hard-stopped child."""
    targets = set(scope.get('resume_parked_task_ids', []))
    if (scope.get('bounded_native_continuation') is not True or not targets
            or any(type(i) is not int or i <= 0 for i in targets)
            or targets & set(scope.get('preserved_old_task_ids', []))
            or not 0 < scope.get('maximum_wall_seconds', 0) <= 900):
        raise ValueError('A bounded continuation needs exact admitted task IDs')
    originals = scope['original_target_deadlines']
    if any(str(i) not in originals for i in targets):
        raise ValueError('Every admitted task needs its original deadline')
    delayed = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).isoformat(' ')
    parked, fenced, unresolved = [], [], []
    with closing(sqlite3.connect(Path(instance) / 'school_watcher.db')) as c:
        c.row_factory = sqlite3.Row
        with c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT * FROM background_tasks ORDER BY id').fetchall():
                if row['id'] not in targets and row['id'] <= scope['existing_max_task_id']:
                    continue
                payload = json.loads(row['payload'] or '{}')
                school_id = payload.get('school_id')
                if school_id is None and payload.get('department_id'):
                    department = c.execute('SELECT school_id FROM departments WHERE id=?',
                                           (payload['department_id'],)).fetchone()
                    school_id = department[0] if department else None
                if str(school_id) != str(scope['continuation_school_id']):
                    continue
                owned = bool(row['token'] or row['worker_id'] or row['lease_until'])
                if owned and not (child_confirmed_stopped and row['worker_id'] == 'declared-native-pilot'):
                    unresolved.append(row['id'])
                    continue
                values = {}
                if row['id'] in targets:
                    values['deadline_at'] = originals[str(row['id'])]
                if row['state'] in ('pending', 'waiting', 'running'):
                    values['available_at'] = delayed
                    parked.append(row['id'])
                    if owned:
                        values.update(state='pending', token=None, worker_id=None, lease_until=None)
                        fenced.append(row['id'])
                if values:
                    names = ','.join(key + '=?' for key in values)
                    c.execute('UPDATE background_tasks SET ' + names + ' WHERE id=? AND generation=?',
                              (*values.values(), row['id'], row['generation']))
    return dict(parked_ids=parked, fenced_ids=fenced, unresolved_owned_ids=unresolved,
                task_ids_generations_and_checkpoints_preserved=True, original_target_deadlines_restored=True)
