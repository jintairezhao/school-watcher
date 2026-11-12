"""Opt-in local model materials for controlled sampling, never a decision policy.

Only a caller inside capture_materials enables recording. Credentials, headers,
environment and exception messages never enter the recorder. Model text and public
input are intentionally retained; use only with a declared sampling scope.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

_session = ContextVar('ai_material_session', default=None)
_attempt = ContextVar('ai_material_attempt', default=None)
_action = ContextVar('ai_material_action', default=None)
_claim = ContextVar('ai_material_claim', default=None)
_SAFE_CODE = re.compile(r'[a-z][a-z0-9_]{0,79}')
_CONTROL_RESERVE = 4096


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _usage(value):
    value = value if isinstance(value, dict) else {}
    return {key: item for key, item in value.items() if
        key == 'known' and type(item) is bool or key in (
            'input_tokens', 'output_tokens', 'total_tokens', 'cache_hit_tokens', 'cache_miss_tokens')
        and type(item) is int and item >= 0}


def _code(value):
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else 'unclassified_error'


def _code_fingerprints(root, files, session):
    """Record source origins without importing modules or claiming bytecode identity."""
    digests, origins = {}, {}
    for name in files:
        module_name = name.removesuffix('.py').replace('/', '.')
        loaded = sys.modules.get(module_name)
        if loaded is None:
            main = sys.modules.get('__main__')
            if getattr(main, '__file__', None) and Path(main.__file__).resolve() == (root / name).resolve():
                loaded = main
        origin = dict(module=module_name, loaded=loaded is not None,
            basis='loaded_module_file' if loaded is not None else 'source_file_not_loaded')
        location = getattr(loaded, '__file__', None) if loaded is not None else root / name
        origins[name] = origin
        if location:
            origin['path'] = str(Path(location).resolve())
        try:
            if not location:
                raise OSError('Loaded module has no source file')
            digests[name] = hashlib.sha256(Path(location).read_bytes()).hexdigest()
        except OSError:
            # A disk copy at the baseline path cannot stand in for missing
            # source bytes of an already imported replacement module.
            origin['basis'] = 'loaded_module_file_unavailable' if loaded is not None else 'source_file_unavailable'
            session.gaps.add('loaded_code_file_unavailable' if loaded is not None else 'code_file_unavailable')
    return dict(code_sha256=digests, code_origins=origins, code_provenance_schema=1,
        code_fingerprint_scope='source_file_bytes_at_capture_open_not_loaded_bytecode')


class _Capture:
    def __init__(self, directory, label, max_bytes, max_record_bytes):
        self.path = Path(directory).resolve() / uuid.uuid4().hex
        self.label, self.max_bytes, self.max_record_bytes = label, max_bytes, max_record_bytes
        self.started = time.monotonic()
        self.gaps, self.attempts = set(), []
        self.native = []
        self.claimed_pages = {}
        self.inspections = {}
        self.navigation_origins = {}
        self.bytes = self.sequence = 0
        self.recorded = 0

    @property
    def complete(self):
        return not self.gaps

    def _write(self, name, data):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path / ('.' + uuid.uuid4().hex + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path / name)
        finally:
            temporary.unlink(missing_ok=True)
        self.bytes += len(data)

    def emit(self, event, payload, *, control=None):
        self.sequence += 1
        envelope = dict(schema_version=1, event=event, sequence=self.sequence,
            observed_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - self.started, 6),
            attempt_id=(_attempt.get() or {}).get('id'),
            execution_id=(_attempt.get() or {}).get('execution_id'),
            action_reference=current_action_reference(),
            payload_sha256=_digest(payload), payload=payload)
        data = _json(envelope) + b'\n'
        if not control and len(data) > self.max_record_bytes:
            self.gaps.add('record_too_large')
            return
        if self.bytes + len(data) > self.max_bytes - (0 if control else _CONTROL_RESERVE):
            self.gaps.add('capture_size_limit')
            return
        self._write(control or f'event-{self.sequence:06d}.json', data)
        self.recorded += 1

    def failure(self):
        self.gaps.add('record_write_failed')
        logging.getLogger(__name__).warning('AI material recording unavailable; sample incomplete')


def _emit(event, payload, *, control=None):
    session = _session.get()
    if session is not None:
        try:
            session.emit(event, payload() if callable(payload) else payload, control=control)
        except Exception:
            session.failure()


def current_action_reference():
    """A reference to the actual active action; never find one by URL or time."""
    session, action = _session.get(), _action.get()
    if session is None or not action or action['capture_id'] != session.path.name:
        return None
    return dict(capture_id=action['capture_id'], action_id=action['action_id'],
                decision_references=[dict(item) for item in action['decision_references']])


def _public_url(value):
    from backend.services.structure_facts import _url
    return _url(value) if isinstance(value, str) else ''


def record_navigation_origin(origin):
    """Bind an origin while its actual inspection is active, without another read."""
    session = _session.get()
    if session is None:
        return
    try:
        if not isinstance(origin, dict):
            return
        event = origin.get('event_id')
        reference = session.inspections.get(event)
        if not reference or reference != current_action_reference():
            return
        value = {key: origin.get(key) for key in ('event_id', 'site_key', 'url', 'content_hash')}
        if (not isinstance(value['content_hash'], str)
                or not re.fullmatch('[0-9a-f]{64}', value['content_hash'])
                or not value['site_key'] or _public_url(value['url']) != value['url']):
            return
        # Do not replace an earlier binding if a caller later supplies different
        # material for the same inspection event.
        if event not in session.navigation_origins:
            session.navigation_origins[event] = value
            _emit('navigation_origin_bound', value)
    except Exception:
        session.failure()


@contextmanager
def _record_action(kind, inputs, *, causes=()):
    session = _session.get()
    if session is None:
        yield None
        return
    parent = current_action_reference()
    decisions = []
    for reference in ([parent] if parent else []) + list(causes):
        for decision in reference['decision_references']:
            if decision not in decisions:
                decisions.append(dict(decision))
    action = dict(capture_id=session.path.name, action_id=uuid.uuid4().hex, decision_references=decisions)
    marker = _action.set(action)
    _emit('action_started', dict(kind=kind, input=inputs, parent_action=parent, causes=list(causes)))
    try:
        yield action
    except BaseException as error:
        _emit('action_interrupted', dict(kind=kind, error_type=type(error).__name__))
        raise
    else:
        _emit('action_finished', dict(kind=kind, outcome='returned_not_source_validation'))
    finally:
        _action.reset(marker)


def capture_inventory_claim(function):
    """Bind a real returned page object to this particular claim, in memory only."""
    @wraps(function)
    def call(inventory, key, focus='all', *, resume_url=None):
        session = _session.get()
        if session is None:
            return function(inventory, key, focus=focus, resume_url=resume_url)
        holder = dict(decision_reference=None)
        with _record_action('page_claim', dict(site_key=key, focus=focus, resume_url=_public_url(resume_url))) as action:
            marker = _claim.set(holder)
            try:
                page = function(inventory, key, focus=focus, resume_url=resume_url)
            finally:
                _claim.reset(marker)
            if page is not None:
                if holder['decision_reference'] is not None:
                    action['decision_references'].append(holder['decision_reference'])
                reference = current_action_reference()
                session.claimed_pages[id(page)] = (page, reference)
                _emit('page_claimed', dict(site_key=key, url=_public_url(page.get('url')),
                    selection_mode='resume' if resume_url else focus,
                    selected_decision_reference=holder['decision_reference']))
            return page
    return call


@contextmanager
def capture_inspection(page, observed):
    session = _session.get()
    if session is None:
        yield
        return
    claimed = session.claimed_pages.pop(id(page), None)
    causes = [claimed[1]] if claimed and claimed[0] is page else []
    with _record_action('page_inspection', dict(url=_public_url(page.get('url')),
            claim_reference_status='exact_returned_page' if causes else 'not_recorded'), causes=causes):
        if observed and observed.get('event_id'):
            session.inspections[observed['event_id']] = current_action_reference()
        yield


def _task_origin(handle):
    """Use the task's existing checked page receipt, not a nearest-page lookup."""
    session = _session.get()
    payload = handle.get('payload') or {}
    if handle.get('kind') == 'navigation_review':
        origin = payload.get('inspection_origin')
        origin = origin if isinstance(origin, dict) else {}
        page, site = payload.get('page') or {}, payload.get('site') or {}
        url = _public_url(page.get('snapshot_url') or page.get('url'))
        material_hash = page.get('snapshot_hash') or (page.get('snapshot_ref') or {}).get('hash') or page.get('content_hash')
        recorded = session.navigation_origins.get(origin.get('event_id'))
        matched = (recorded is not None and recorded == {key: origin.get(key) for key in recorded}
                   and site.get('site_key') == recorded['site_key']
                   and url == recorded['url'] and material_hash == recorded['content_hash'])
        causes = [session.inspections[origin['event_id']]] if matched else []
        return dict(task={key: handle.get(key) for key in ('id', 'generation', 'kind')},
            declared_parent_task_id=payload.get('parent_task_id'),
            declared_parent_generation=payload.get('parent_generation'), url=url,
            inspection_event_id=origin.get('event_id'),
            material_reference=dict(url=url, content_hash=material_hash),
            origin_resolution='recorded_navigation_inspection_origin' if matched else
                'navigation_origin_mismatch' if recorded else 'navigation_origin_not_recorded_in_current_capture'), causes
    context = payload.get('discovery_attention') or {}
    material = context.get('material') or {}
    url = _public_url(material.get('final_url') or material.get('reference_url') or payload.get('url'))
    from backend.services.source_execution_events import _receipt_events
    events = _receipt_events(context.get('inspection_receipt'), context.get('inspection_event_id'),
        material.get('content_hash'), url)
    causes = []
    for event in events or []:
        reference = session.inspections.get(event['id'])
        if reference and reference not in causes:
            causes.append(reference)
    return dict(task={key:handle.get(key) for key in ('id', 'generation', 'kind')},
        declared_parent_task_id=payload.get('parent_task_id'), declared_parent_generation=payload.get('parent_generation'),
        url=_public_url(payload.get('url')), inspection_event_id=context.get('inspection_event_id'),
        material_reference=dict(url=url, content_hash=material.get('content_hash')),
        origin_resolution='recorded_inspection_receipt' if causes else
            'receipt_outside_current_capture' if events else 'not_recorded_or_invalid_receipt'), causes


def capture_task(function):
    @wraps(function)
    def call(*args, **kwargs):
        if _session.get() is None:
            return function(*args, **kwargs)
        handle = kwargs.get('handle') if 'handle' in kwargs else args[1]
        try:
            inputs, causes = _task_origin(handle)
        except Exception:
            _session.get().failure()
            inputs, causes = dict(origin_resolution='recording_unavailable'), []
        with _record_action('task', inputs, causes=causes):
            return function(*args, **kwargs)
    return call


def capture_acquisition(function):
    @wraps(function)
    def call(request, *args, **kwargs):
        if _session.get() is None:
            return function(request, *args, **kwargs)
        with _record_action('acquisition', dict(url=_public_url(request.url), purpose=request.purpose,
                source_id=request.source_id)):
            result = function(request, *args, **kwargs)
            _emit('acquisition_result', dict(final_url=_public_url(result.final_url), status=result.status,
                outcome=result.outcome, transport=result.transport,
                content_hash=hashlib.sha256(result.html.encode()).hexdigest() if result.html else None,
                scope='coordinator_result_not_http_dispatch_or_source_success'))
            return result
    return call


@contextmanager
def capture_materials(directory, *, label, max_bytes=32 * 1024 * 1024, max_record_bytes=4 * 1024 * 1024):
    """Enable bounded recording only in this execution context; no IO on import.

    A process interruption leaves manifest.json without completion.json. Missing,
    omitted or unwritable records never qualify as a complete sample. This is an
    input/output recording boundary, not proof of frontier or branch coverage.
    """
    if not isinstance(label, str) or not 1 <= len(label) <= 120:
        raise ValueError('A bounded material label is required')
    if type(max_bytes) is not int or max_bytes < 8192 or type(max_record_bytes) is not int or max_record_bytes < 256:
        raise ValueError('Invalid material size limits')
    session = _Capture(directory, label, max_bytes, max_record_bytes)
    marker = _session.set(session)
    attempt_marker = _attempt.set(None)
    action_marker = _action.set(None)
    claim_marker = _claim.set(None)
    try:
        def manifest():
            # A bounded execution may replace this recorder too. Unloaded
            # sources still belong to the imported backend package's image.
            package_file = getattr(sys.modules.get('backend'), '__file__', None)
            root = Path(package_file).resolve().parents[1] if package_file else Path(__file__).resolve().parents[2]
            files = ['backend/ai/runtime.py', 'backend/ai/providers.py', 'backend/ai/material_capture.py',
                'backend/ai/skill_loader.py', 'backend/services/discovery_policy.py',
                'backend/scraper/discovery/frontier.py', 'backend/services/source_validation.py',
                'backend/services/source_execution_events.py']
            files += ['backend/services/source_inventory.py', 'backend/scraper/discovery/inventory_crawler.py',
                'backend/services/exploration_events.py', 'backend/scraper/acquisition/coordinator.py',
                'backend/worker.py', 'scripts/checks/run_native_branch_pilot.py']
            return dict(label=label, max_bytes=max_bytes, max_record_bytes=max_record_bytes,
                kind='actual_execution_materials_not_threshold_evaluation',
                causal_reference_schema=1,
                **_code_fingerprints(root, files, session))
        _emit('capture_opened', manifest, control='manifest.json')
        yield session
    finally:
        _finish_native(session)
        if any(a['dispatched'] and not (a['request'] and a['response'] and a['result']) for a in session.attempts):
            session.gaps.add('dispatch_material_incomplete')
        _emit('capture_closed', lambda: dict(complete=session.complete, gaps=sorted(session.gaps),
            attempts=len(session.attempts), dispatched=sum(a['dispatched'] for a in session.attempts),
            recorded_events=session.recorded, recorded_bytes_before_close=session.bytes,
            unstarted_recorded_claims=len(session.claimed_pages),
            causal_scope='instrumented_current_context_only_not_complete_experiment',
            cross_thread_context_automatically_inherited=False), control='completion.json')
        _claim.reset(claim_marker)
        _action.reset(action_marker)
        _session.reset(marker)
        _attempt.reset(attempt_marker)


def capture_identity(execution_id, *, dispatched=False):
    attempt = _attempt.get()
    if attempt is not None:
        attempt['execution_id'] = execution_id
        attempt['dispatched'] |= dispatched


def capture_runtime(function):
    @wraps(function)
    def call(*args, **kwargs):
        session = _session.get()
        if session is None:
            return function(*args, **kwargs)
        attempt = dict(id=uuid.uuid4().hex, execution_id=kwargs.get('execution_id'),
            dispatched=False, request=False, response=False, result=False)
        session.attempts.append(attempt)
        marker = _attempt.set(attempt)
        _emit('execution_invoked', lambda: dict(
            requested_execution_id=kwargs.get('execution_id'),
            messages_sha256=_digest(kwargs.get('messages')),
            **{key: kwargs.get(key) for key in ('purpose', 'skill_id', 'skill_version',
                'skill_digest', 'input_digest', 'mode', 'school_id', 'output_tokens')}))
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            if isinstance(error, Exception):
                attempt['result'] = True
                _emit('execution_error', lambda: dict(code=_code(getattr(error, 'code', None)),
                    dispatched=attempt['dispatched']))
            else:
                session.gaps.add('execution_interrupted')
                _emit('execution_interrupted', {'dispatched': attempt['dispatched']})
            raise
        else:
            attempt['result'] = True
            _emit('execution_result', lambda: dict(
                status=result.get('status'), error_code=_code(result['error_code']) if result.get('error_code') else '',
                output=result.get('output'), usage=_usage(result.get('usage')),
                provenance={key: (result.get('provenance') or {}).get(key) for key in (
                    'execution_id', 'profile_id', 'config_version', 'provider', 'model',
                    'skill_id', 'skill_version', 'skill_digest', 'input_digest', 'validator_version', 'request_id')},
                dispatch_attempted=attempt['dispatched'],
                use_kind='request_attempt' if attempt['dispatched'] else 'no_new_dispatch'))
            return result
        finally:
            _attempt.reset(marker)
    return call


def record_provider_request(profile, body, policy):
    if _session.get() is None:
        return
    attempt = _attempt.get()
    if attempt is not None:
        attempt['request'] = True
    else:
        _session.get().gaps.add('runtime_context_missing')
    _emit('provider_request', lambda: dict(body=body, effective_policy=policy,
        delivery='prepared_for_single_adapter_attempt_not_proven_sent',
        profile={key: profile.get(key) for key in ('id', 'version', 'provider', 'model', 'region')}))


def capture_provider(function):
    @wraps(function)
    def call(*args, **kwargs):
        if _session.get() is None:
            return function(*args, **kwargs)
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            attempt = _attempt.get()
            if attempt is not None:
                attempt['response'] = isinstance(error, Exception)
            _emit('provider_error', lambda: dict(code=_code(getattr(error, 'code', None)),
                uncertain=getattr(error, 'uncertain', True),
                content=getattr(error, 'content', '') if isinstance(getattr(error, 'content', ''), str) else '',
                usage=_usage(getattr(error, 'usage', None)), request_id=getattr(error, 'request_id', None)))
            raise
        else:
            attempt = _attempt.get()
            if attempt is not None:
                attempt['response'] = True
            _emit('provider_response', lambda: dict(content=result.content, finish_reason=result.finish_reason,
                usage=_usage(result.usage), request_id=result.request_id))
            return result
    return call


def capture_frontier(function):
    """Freeze only arguments actually visible to the existing route chooser."""
    signature = inspect.signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        if _session.get() is None:
            return function(*args, **kwargs)
        decision_id = uuid.uuid4().hex
        def inputs():
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(decision_id=decision_id,
                arguments={key: value for key, value in bound.arguments.items() if key != 'explain'})
        _emit('frontier_inputs', inputs)
        try:
            selected = function(*args, **kwargs)
        except BaseException:
            _emit('frontier_error', {'decision_id': decision_id})
            raise
        _emit('frontier_result', lambda: dict(decision_id=decision_id,
            selected_url=selected.get('url') if selected is not None else None))
        if _claim.get() is not None and selected is not None:
            _claim.get()['decision_reference'] = dict(capture_id=_session.get().path.name, decision_id=decision_id)
        return selected
    return call


def record_native(instance, kind, detail):
    """Remember an existing writer's row, without flushing or committing it."""
    session = _session.get()
    if session is None:
        return
    try:
        from backend.database.db import db
        from backend.services.tasks import current_execution
        task = current_execution() or {}
        session.native.append((instance, db.engine, kind, _digest(detail),
            {key: task.get(key) for key in ('id', 'generation', 'kind')}, current_action_reference()))
    except Exception:
        session.failure()


def _finish_native(session):
    """Read committed rows using their original identity and exact material hash.

    An independent connection sees no caller's uncommitted rows. No session read,
    flush, commit, rollback, or database creation is performed by this observer.
    """
    from sqlalchemy import inspect as inspect_row, select
    for instance, engine, kind, expected, task, origin in session.native:
        try:
            table = instance.__table__
            column = {'native_validation': 'validation_json',
                      'source_configuration': 'execution_json'}[kind]
            identity = inspect_row(instance).identity
            record_id = identity[0] if identity else None
            row = None
            if record_id is not None:
                if engine.dialect.name == 'sqlite':
                    database = engine.url.database
                    if not database or database == ':memory:':
                        session.gaps.add('native_database_not_file_backed')
                    else:
                        # Read-only mode cannot recreate a removed sampling database.
                        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                            row = connection.execute('SELECT id,' + column + ',created_at FROM ' +
                                table.name + ' WHERE id=?', (record_id,)).fetchone()
                else:
                    with engine.connect() as connection:
                        row = connection.execute(select(table.c.id, table.c[column], table.c.created_at)
                            .where(table.c.id == record_id)).first()
            actual = json.loads(row[1]) if row and isinstance(row[1], str) else row[1] if row else None
            committed = row is not None and _digest(actual) == expected
            payload = dict(record_id=record_id, table=table.name, task=task, origin_action_reference=origin,
                persistence='committed' if committed else 'not_visible_as_committed_at_capture_close',
                expected_material_sha256=expected)
            if committed:
                payload.update(detail=actual, row_created_at=str(row[2]))
            elif row is not None:
                session.gaps.add('native_identity_or_material_changed')
            _emit(kind, payload)
        except Exception:
            session.failure()
