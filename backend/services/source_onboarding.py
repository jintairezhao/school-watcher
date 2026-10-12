"""Discover a page, execute its parser, and publish its first notices.

Each page is independent durable work. Existing proposal workflows are reserved
for legacy/admin edits; automatic onboarding never creates a review proposal.
"""
from datetime import datetime
from functools import partial
import hashlib
import json

from backend.database.db import db
from backend.database.models import BackgroundTask, Department, DepartmentDirectoryEntry, School, Subscription
from backend.database.source_governance_models import SourceConfigVersion
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.http_client import same_school_url, validate_public_url
from backend.services import tasks
from backend.services.source_inventory import canonical_url

CONFIG_FIELDS = ('name', 'list_url', 'list_selector', 'title_selector', 'link_selector',
                 'date_selector', 'content_selector', 'group_name')


def _validation_upgrade_codes(checkpoint):
    from backend.services.source_validation import VALIDATION_VERSION
    saved = (checkpoint or {}).get('column_validation') or {}
    version = saved.get('validation_version')
    if not isinstance(version, int) or isinstance(version, bool) or version >= VALIDATION_VERSION:
        return set()
    codes = {code for row in saved.get('failures', [])
             for code in row.get('issue', {}).get('codes', [])}
    fixed = set()
    # These are corrections to particular validators, not an invitation to
    # re-open every failure whenever the validation version changes.
    if version < 4 and 'article_sample_mismatch' in codes:
        fixed.add('article_sample_mismatch')
    if version <= 4 and VALIDATION_VERSION >= 5 and 'pagination_repeats_first_page' in codes:
        fixed.add('pagination_repeats_first_page')
    return fixed


def _validation_upgrade(checkpoint):
    return bool(_validation_upgrade_codes(checkpoint))


def _resume_pagination_validation(task, now):
    """Wake known pager false rejections in the same durable task generation."""
    from backend.services.discovery_control import should_pause
    previous_state, previous_generation, previous_update = task.state, task.generation, task.updated_at
    checkpoint = task.checkpoint or {}
    validation = checkpoint.get('column_validation') or {}
    if (task.kind != 'onboard' or task.state not in ('done', 'failed')
            or task.error_code
            or validation.get('stage') != 'done'
            or (checkpoint.get('column_pending_decision') or {}).get('blocked') in ('result', 'usage')
            or should_pause(task.kind, task.payload)
            or 'pagination_repeats_first_page' not in _validation_upgrade_codes(task.checkpoint)):
        return False
    for key in ('website_not_before', 'recovery_not_before'):
        if value := (task.result or {}).get(key):
            try:
                if datetime.fromisoformat(value) > now:
                    return False
            except (ValueError, TypeError):
                return False
    if task.payload.get('parent_task_id'):
        parent = db.session.get(BackgroundTask, task.payload['parent_task_id'])
        if not parent or parent.generation != task.payload.get('parent_generation'):
            return False
    from datetime import timedelta
    paused_school = db.select(BackgroundTask.id).where(
        BackgroundTask.identity == f"discover:{task.payload.get('school_id')}",
        BackgroundTask.payload['discovery_pause_requested'].as_boolean().is_(True)).exists()
    changed = db.session.execute(db.update(BackgroundTask).where(
        BackgroundTask.id == task.id, BackgroundTask.state == previous_state,
        BackgroundTask.generation == previous_generation, BackgroundTask.updated_at == previous_update,
        BackgroundTask.payload['discovery_pause_requested'].as_boolean().is_not(True), ~paused_school
    ).values(state='pending', phase='validation_replay', capability='http',
             available_at=now, queued_at=now, updated_at=now, deadline_at=now + timedelta(hours=2),
             finished_at=None, token=None, worker_id=None, lease_until=None, error='', error_code='')
      .execution_options(synchronize_session=False))
    db.session.refresh(task)
    # Keep the task's payload, result, attempts, checkpoint and generation.
    # This read-only rule replay cannot renew the school exploration allowance.
    return bool(changed.rowcount)


def queue_columns(school_id, inventory, key):
    """Use observed official pages, not model-proposed URLs or client flags."""
    from backend.services.source_catalog import publication_candidates
    from backend.services.source_governance import _snapshot
    report = inventory.report(key)
    candidates = publication_candidates(report, inventory.structure(key))
    by_url = {}
    for candidate in candidates:
        address = canonical_url(candidate['list_url'])
        # A homepage preview with a real "more" link is not a second source.
        if candidate.get('column_url') and canonical_url(candidate['column_url']) != address:
            continue
        by_url.setdefault(address, []).append(candidate)
    jobs = []
    for page in report['pages']:
        url = canonical_url(page.get('final_url') or page['url'])
        configs = by_url.get(url, [])
        previous_url = canonical_url(page['url'])
        existing = Department.query.filter(Department.school_id == school_id,
                    Department.list_url.in_({url, previous_url})).all()
        if page['kind'] == 'directory' and not configs and not any(d.list_selector for d in existing):
            # A plain roster is not publication work. Mixed directory pages
            # with actual lists still require the same scope and body checks.
            continue
        if page['state'] != 'fetched' or not (configs or page['kind'] == 'channel' or any(d.list_selector for d in existing)):
            continue
        if not configs and not same_school_url(url, report['site']['root_url']):
            continue
        from backend.services.discovery_changes import baseline
        shape = baseline(page)
        changed = shape.get('change') == 'changed' or bool(existing and previous_url != url)
        html = inventory.snapshot(key, page['url'])
        from backend.scraper.selector_monitor import _quick_stats
        failed_rule = bool(html) and any(not (stats := _quick_stats(html, d)) or not stats['matched'] or stats['junk']
                                       for d in existing if d.list_selector)
        changed |= failed_rule
        if not changed and not configs and any(d.list_selector for d in existing):
            continue
        if not changed and configs and all(any(d.list_selector == c['list_selector'] and d.name == c['name']
                               for d in existing) for c in configs):
            continue
        identity = f'{school_id}:' + hashlib.sha256(url.encode()).hexdigest()[:24]
        old = BackgroundTask.query.filter_by(identity='onboard:' + identity).first()
        # Finished pages only reopen on an explicit refresh. Crawl slices and
        # process restarts must not repeat paid calls for the same unresolved page.
        handle = tasks.current_execution() or {}
        run_key = f"{handle.get('id', 0)}:{handle.get('generation', 0)}"
        manual = handle.get('payload', {}).get('refresh') is True
        validation_upgrade = bool(old and _validation_upgrade(old.checkpoint))
        if old and 'pagination_repeats_first_page' in _validation_upgrade_codes(old.checkpoint):
            if _resume_pagination_validation(old, datetime.utcnow()):
                db.session.commit()
                jobs.append(old.id)
            # Pauses, website waits and active work also apply when a discovery
            # slice sees this page again. Never fall through to enqueue a fresh
            # generation for the same retained pager correction.
            continue
        preserved_checkpoint = dict(old.checkpoint or {}) if validation_upgrade else None
        if old:
            unresolved = failed_rule or (old.result or {}).get('state') != 'connected' or bool((old.result or {}).get('issues'))
            if (old.state not in ('done', 'failed') or
                    old.payload.get('source_shape') == shape.get('hash') and not validation_upgrade and not (manual and unresolved and old.payload.get('refresh_key') != run_key) or
                    not changed and not (manual and unresolved) and not validation_upgrade):
                continue
        snapshot = _snapshot(html, url, role='list') if html else None
        groups = {c['group_name'] for c in configs if c.get('group_name')}
        payload = {'school_id': school_id, 'url': url,
                   'group_name': next(iter(groups)) if len(groups) == 1 else '',
                   'label': page['label'], 'snapshot': snapshot,
                   'source_shape': shape.get('hash'), 'refresh_key': run_key,
                   'previous_url': previous_url if previous_url != url else None,
                   'revalidate': changed or bool(manual and existing), 'parent_task_id': handle.get('id'),
                   'parent_generation': handle.get('generation'),
                   'ai_assist': handle.get('payload', {}).get('ai_assist', True),
                   'official_external': bool(configs and not same_school_url(url, report['site']['root_url']))}
        job = tasks.enqueue('onboard', identity, payload, replace_finished=bool(old))
        if preserved_checkpoint is not None:
            job.checkpoint = preserved_checkpoint
            db.session.commit()
        jobs.append(job.id)
    return {'onboarding_ids': jobs, 'proposal_ids': [], 'activated_ids': [], 'remaining_candidates': 0}


def _unconnected_columns(school_id, *, automatic=False):
    """Share eligibility between scheduling and bounded execution."""
    from datetime import timedelta
    from backend.services.discovery_control import requested
    checked = db.func.coalesce(BackgroundTask.finished_at, BackgroundTask.checked_at, BackgroundTask.updated_at)
    query = BackgroundTask.query.filter(BackgroundTask.kind == 'onboard',
        BackgroundTask.payload['school_id'].as_integer() == school_id,
        BackgroundTask.state.in_(('done', 'failed')))
    if automatic:
        from backend.services.discovery_policy import get_policy
        query = query.filter(checked <= datetime.utcnow() - timedelta(seconds=get_policy()['recovery_seconds']))
    for row in query.order_by(checked, BackgroundTask.id):
        result = row.result or {}
        if requested(row):
            continue
        website_wait = result.get('website_not_before')
        if website_wait:
            try:
                if datetime.fromisoformat(website_wait) > datetime.utcnow():
                    continue
            except (ValueError, TypeError):
                pass
        not_before = result.get('recovery_not_before')
        if automatic and not_before:
            try:
                if datetime.fromisoformat(not_before) > datetime.utcnow():
                    continue
            except (ValueError, TypeError):
                pass
        if automatic and not (row.state == 'failed' or result.get('state') in (
                              'unsupported', 'empty', 'access_limited', 'material_limited')
                              or result.get('state') == 'connected' and result.get('issues')):
            continue
        if automatic and result.get('state') in ('not_column', 'skipped', 'rejected'):
            continue
        configured = result.get('department_ids', [])
        if result.get('state') == 'connected' and not result.get('issues') and configured and all(
                (source := db.session.get(Department, ident)) and source.list_selector for ident in configured):
            continue
        yield row


def pending_onboarding_recovery(school_id):
    """An unreadable source remains recoverable even if its neighbours work."""
    return next(_unconnected_columns(school_id, automatic=True), None) is not None


def retry_unconnected_columns(school_id, refresh_key, *, automatic=False, limit=4, commit=True):
    """Refresh stale failures once per run, with a small automatic retry budget."""
    handle = tasks.current_execution() or {}
    ids = []
    for row in _unconnected_columns(school_id, automatic=automatic):
        payload = dict(row.payload or {})
        if payload.get('refresh_key') == refresh_key:
            continue
        payload.pop('snapshot', None)
        payload.update(refresh_key=refresh_key, revalidate=True)
        reusable = ((row.result or {}).get('recovery_rules', []) + (row.result or {}).get('unresolved_rules', [])
                    + (row.result or {}).get('empty_rules', []))
        if reusable:
            # The column was already observed and its list scope executed. Read
            # it again with those rules before buying a new recognition decision.
            payload['observed_columns'] = reusable
        if handle.get('payload', {}).get('school_id') == school_id:
            payload.update(parent_task_id=handle['id'], parent_generation=handle['generation'],
                           ai_assist=handle['payload'].get('ai_assist', True))
        job = tasks.enqueue('onboard', row.identity.split(':', 1)[1], payload,
                            replace_finished=True, commit=commit)
        ids.append(job.id)
        if automatic and len(ids) >= max(1, limit):
            break
    return ids


def recognize_column(school_id, url, html, *, feedback=None):
    """Compatibility entry point for bounded, evidence-based source recognition."""
    from backend.services.source_agent import recognize_column as recognize
    return recognize(school_id, url, html, feedback=feedback)


def _read(url, purpose, *, config=None, exploration=None):
    from backend.services.source_governance import _fetch
    return (_fetch(url, purpose, config=config, exploration=exploration)
            if config is not None or exploration is not None else _fetch(url, purpose))


def _sample_matches(config, records, fetcher):
    from backend.services.source_validation import article_samples
    return article_samples(config, records, fetcher)['passed']


def _install(school_id, config, records, *, repair=False, previous_url=None, ai_assist=True, repair_department_id=None, validation=None):
    """Keep source/version/notices/next collection in the same commit fence."""
    from backend.services.source_governance import _hash, _json
    from backend.services.announcement_identity import upsert_listing
    from backend.services.announcement_sources import record_source
    from backend.scraper.change_detector import parse_date
    from backend.scraper.detectors.title_quality import date_from_url
    from backend.services.inbox_refresh import subscribed_sources
    from backend.services.source_validation import same_column, record_validation
    tasks.assert_owned()
    db.session.execute(db.update(School).where(School.id == school_id).values(name=School.name))
    source = db.session.get(Department, repair_department_id) if repair_department_id else None
    if source is not None and (source.school_id != school_id or canonical_url(source.list_url) not in {
            canonical_url(config['list_url']), canonical_url(previous_url or '')}):
        raise ValueError('修复目标与已读取的官网页面不一致')
    if source is not None and not same_column(source, config, records, validation or {}):
        raise ValueError('column_identity_changed')
    if source is None:
        candidates = Department.query.filter(Department.school_id == school_id,
            Department.kind == 'column', Department.list_selector.is_not(None),
            Department.list_url.in_({config['list_url'], previous_url} if previous_url else {config['list_url']})).all()
        matches = [row for row in candidates if (repair or row.list_selector == config['list_selector'])
                   and same_column(row, config, records, validation or {})]
        source = matches[0] if len(matches) == 1 else None
    if source is not None and repair:
        previous = {field: getattr(source, field) or '' for field in CONFIG_FIELDS}
        if previous != config:
            last = SourceConfigVersion.query.filter_by(department_id=source.id).order_by(SourceConfigVersion.version.desc()).first()
            db.session.add(SourceConfigVersion(department_id=source.id, version=last.version + 1 if last else 1,
                config_json=_json(config), config_hash=_hash(config), previous_json=_json(previous)))
            for field, value in config.items():
                setattr(source, field, value)
    if source is None:
        source = Department(school_id=school_id, **config)
        db.session.add(source); db.session.flush()
        db.session.add(SourceConfigVersion(department_id=source.id, version=1,
            config_json=_json(config), config_hash=_hash(config), previous_json=_json(None)))
    if config['group_name']:
        from backend.services.source_placements import _within_unit
        legacy = Department.query.filter_by(school_id=school_id, name=config['group_name'], kind='column').all()
        for parent in legacy if len(legacy) == 1 else []:
            if (not parent.list_selector and parent.id != source.id
                    and _within_unit(config['list_url'], parent.list_url, source.school.url)
                    and not DepartmentDirectoryEntry.query.filter_by(
                    parent_id=parent.id, department_id=source.id).first()):
                db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=source.id, position=0))
        db.session.flush()
    # An existing valid rule is not silently overwritten by rediscovery.
    new_count = 0
    for record in records:
        article, created = upsert_listing(source, record['title'], record['url'],
            published_at=parse_date(record['date']) or date_from_url(record['url']))
        record_source(article, source, record['url'])
        new_count += int(created)
    source.last_scraped_at = datetime.utcnow()
    if validation:
        record_validation(school_id, config, validation, source.id)
    from flask import current_app
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services.school_structure import sync_official_structure
    sync_official_structure(source.school, RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH']), commit=False)
    subscriptions = Subscription.query.filter_by(school_id=school_id).all()
    if any(source.id in {d.id for d in subscribed_sources(source.school, sub.department_ids)} for sub in subscriptions):
        job = tasks.enqueue('collect', source.id, {'school_id': school_id, 'department_id': source.id,
                            'ai_assist': ai_assist},
                            replace_finished=False, commit=False)
        if job.state == 'pending' and job.claim_count == 0:
            job.phase = 'onboarding_collection'
    tasks.assert_owned()
    db.session.commit()
    return source.id, new_count


def _automatic_repair_source(payload, school):
    """Recheck subscription scope when a queued single-page repair executes."""
    source = db.session.get(Department, payload.get('repair_department_id'))
    if (not source or source.school_id != school.id or not source.list_selector or not source.list_url or not school.is_effectively_active()
            or canonical_url(source.list_url) not in {
                canonical_url(payload.get('url', '')), canonical_url(payload.get('previous_url') or '')}):
        return None
    from backend.services.directory_options import directory_entries_for, expand_directory_ids
    entries = directory_entries_for(school.id)
    scopes = Subscription.query.with_entities(Subscription.department_ids).filter_by(school_id=school.id)
    return source if any(ids is None or source.id in expand_directory_ids(school.id, ids, entries) for ids, in scopes) else None


def onboard_page(payload, *, fetcher=None, _force_ai=False):
    from backend.services.source_governance import _snapshot, read_snapshot, _column_scope
    from backend.services.discovery_control import pause_if_requested
    pause_if_requested()
    if payload.get('parent_task_id'):
        parent = db.session.get(BackgroundTask, payload['parent_task_id'])
        if not parent or parent.generation != payload.get('parent_generation'):
            return {'state': 'stale', 'department_ids': []}
    school = db.session.get(School, payload['school_id'])
    if not school or not school.enabled:
        return {'state': 'skipped', 'department_ids': []}
    repair_source = None
    if payload.get('automatic_repair'):
        repair_source = _automatic_repair_source(payload, school)
        if not repair_source:
            return {'state': 'skipped', 'department_ids': [], 'reason': '该信息来源已不在订阅范围内'}
        discovery = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
        if discovery and (discovery.payload or {}).get('ai_assist') is False:
            payload = dict(payload, ai_assist=False)
    school_id, root_url = school.id, school.url
    url = canonical_url(payload['url'])
    validate_public_url(url, resolve=False)
    if not same_school_url(url, root_url) and not payload.get('official_external'):
        return {'state': 'unsupported', 'department_ids': [], 'reason': '栏目地址尚未确认属于学校官网'}
    default_reader = fetcher is None
    fetcher = _read if default_reader else fetcher
    handle = tasks.current_execution()
    checkpoint = dict((handle or {}).get('checkpoint') or {})
    from backend.services.source_agent import recover_unknown_column_checkpoint, pause_unknown_column
    checkpoint, unknown_recovered = recover_unknown_column_checkpoint(checkpoint)
    if unknown_recovered and handle:
        tasks.checkpoint(checkpoint)
    if pause_unknown_column(checkpoint):
        return {'state': 'waiting', 'department_ids': [], 'error_code': 'ai_result_unknown',
                'continuation_required': True, 'reason': '模型请求结果或用量尚未确认，进度已保存'}
    reference = checkpoint.get('onboarding_page') or payload.get('snapshot')
    html = ''
    if reference:
        try:
            html = read_snapshot(reference)
        except (ValueError, OSError):
            pass
        if html:
            # Old workers could save a dated, convincing async template as
            # usable. Recheck it before honoring a completed parser checkpoint.
            from backend.scraper.acquisition import FetchRequest, FetchResult
            from backend.scraper.acquisition.classifier import classify_result
            from backend.scraper.selector_monitor import _repairable_result
            snapshot_url = canonical_url(reference['url'])
            observed = FetchResult.from_dict(dict(reference, final_url=snapshot_url, html=str(html),
                status=reference.get('status', 200), transport=reference.get('transport') or 'http'))
            if observed.ok or _repairable_result(observed):
                observed = classify_result(FetchRequest(url=snapshot_url, purpose='directory'), observed)
            if not observed.ok and not _repairable_result(observed):
                html = ''
                url = snapshot_url
    if not html:
        db.session.commit()
        html = fetcher(url, 'directory')
        final = canonical_url(getattr(html, 'final_url', url))
        if not same_school_url(final, root_url) and not (payload.get('official_external') and same_school_url(final, url)):
            return {'state': 'unsupported', 'department_ids': [], 'reason': '网页跳转到了未确认的外部网站'}
        url = final
        reference = _snapshot(html, url, role='list')
        if getattr(html, 'result', None):
            reference['transport'] = html.result.transport
    else:
        url = canonical_url(reference['url'])
    if handle:
        tasks.checkpoint(dict(checkpoint, onboarding_page=reference))
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.wordpress_publications import page_kind
    if page_kind(BeautifulSoup(html, 'lxml')) in ('article', 'attachment', 'search'):
        # The primary page is a post/file/search result. Repeated recommendations
        # are not independent publishing entrances. Discovery retains its actual
        # category/navigation links; existing source/history rows stay intact.
        return {'state': 'not_column', 'department_ids': [], 'new_count': 0,
                'reason': '该页面为正文、附件详情或搜索结果，继续沿已观察的栏目入口处理'}
    from backend.services.source_agent import validation_feedback, selector_observation
    from backend.services.source_governance import _normal, _column_label
    from backend.services.source_validation import article_samples, pagination_sample, empty_region, record_validation, sample_failure_kind, VALIDATION_VERSION
    from backend.services.discovery_policy import progress_state, advance_progress
    assistance = payload.get('ai_assist') is not False
    if payload.get('parent_task_id') and parent.payload.get('ai_assist') is False:
        assistance = False
    page_hash = hashlib.sha256((url + '\\0' + html).encode()).hexdigest()
    saved = dict(checkpoint.get('column_validation') or {})
    same_retained_page = saved.get('page_hash') == page_hash
    upgrade_codes = _validation_upgrade_codes({'column_validation': saved})
    pending_replay = ((saved.get('validation_replay') or {}).get('validation_version') == VALIDATION_VERSION
                      and saved.get('stage') != 'done')
    if (saved.get('page_hash') != page_hash and 'pagination_repeats_first_page' not in upgrade_codes
            and not pending_replay):
        saved = {'page_hash': page_hash, 'stage': 'ai' if _force_ai else 'rules',
                 'department_ids': [], 'new_count': 0, 'completed': [], 'failures': [],
                 'progress': progress_state(), 'progress_ids': [], 'empty_columns': [], 'empty_rules': []}
    elif 'pagination_repeats_first_page' in upgrade_codes:
        corrected = [failure for failure in saved['failures']
                     if 'pagination_repeats_first_page' in failure.get('issue', {}).get('codes', [])]
        # A failed validation saved the executed column rules. Prefer those
        # exact candidates, preserving other failed columns and paid plans.
        columns = [failure['config'] for failure in corrected if failure.get('config')]
        if not columns:
            retained = saved.get('trial_columns') or (checkpoint.get('column_regions') or {}).get('columns', [])
            def matches(failure, column):
                observation = failure.get('observation') or {}
                identity = {**failure.get('issue', {}), **observation}
                return any(identity.get(key) and identity[key] == column.get(key)
                           for key in ('list_selector', 'container_selector', 'heading_selector', 'name'))
            columns = [column for column in retained if any(matches(failure, column) for failure in corrected)]
            if not columns and len(retained) == 1:
                columns = retained
        if columns:
            saved.update(stage='trial', new_count=0, validation_replay={
                'from_version': saved['validation_version'], 'validation_version': VALIDATION_VERSION,
                'columns': columns})
            if not saved.get('trial_columns'):
                saved.update(trial_columns=columns, trial_origin_stage='rules',
                    trial_decision_ids=(checkpoint.get('column_regions') or {}).get('decision_ids', []),
                    trial_request_key=saved.get('last_request_key', ''))
            saved['failures'] = [failure for failure in saved['failures'] if failure not in corrected]
    elif _validation_upgrade({'column_validation': saved}):
        # Retry a program-confirmed validation correction with retained page and
        # model observations. Connected identities and the school fee ledger
        # remain intact. This is not a new exploration round.
        saved.update(stage='trial' if saved.get('trial_columns') else 'rules', failures=[],
                     progress=progress_state(), progress_ids=[], new_count=0)
    saved['validation_version'] = VALIDATION_VERSION
    saved['page_hash'] = page_hash
    saved.setdefault('progress', progress_state())
    saved.setdefault('progress_ids', [])
    saved.setdefault('empty_columns', [])
    saved.setdefault('empty_rules', [])
    if same_retained_page and saved['stage'] != 'done' and not saved.get('trial_columns'):
        retained = (checkpoint.get('column_regions') or {}).get('columns', [])
        if retained:
            # Earlier versions saved paid observations under column_regions but
            # retried recognition before executing them. They are candidate
            # rules, never installed facts: all scope/body checks still execute.
            saved.update(trial_columns=retained, trial_origin_stage=saved['stage'],
                trial_decision_ids=(checkpoint.get('column_regions') or {}).get('decision_ids', []),
                trial_request_key=saved.get('last_request_key', ''), stage='trial')

    def persist(**values):
        saved.update(values)
        if handle:
            # Recognition also checkpoints its own region cursor. Preserve it
            # when saving trial results or moving into the single repair turn.
            latest = dict(tasks.current_execution().get('checkpoint') or {})
            tasks.checkpoint(dict(latest, onboarding_page=reference, column_validation=dict(saved)))

    def finish(state='unsupported', reason='', error_code='', extra_issues=()):
        from datetime import timedelta
        from backend.services.discovery_policy import get_policy
        issues = [failure['issue'] for failure in saved['failures']] + list(extra_issues)
        blocked = [failure for failure in saved['failures'] if failure.get('repairable') is False]
        if state == 'unsupported' and blocked and len(blocked) == len(saved['failures']) and not extra_issues:
            state = 'access_limited' if all(f.get('failure_kind') == 'access_restricted' for f in blocked) else 'material_limited'
        result = {'state': 'connected' if saved['department_ids'] else state,
                  'department_ids': saved['department_ids'], 'new_count': saved['new_count'],
                  'issues': issues, 'reason': issues[0]['reason'] if issues else reason}
        if not saved['department_ids'] and state == 'unsupported' and saved['empty_columns'] and not issues:
            result.update(state='empty', reason='栏目暂无内容，后续将自动更新')
        result['empty_columns'] = saved['empty_columns']
        result['empty_rules'] = saved['empty_rules']
        if blocked:
            result['recovery_rules'] = [failure['config'] for failure in blocked]
            result['unresolved_rules'] = [failure['config'] for failure in saved['failures']
                                         if failure.get('repairable') is not False and failure.get('config')]
            wait = max(get_policy()['recovery_seconds'], max(f.get('retry_after', 0) for f in blocked))
            result['recovery_not_before'] = (datetime.utcnow() + timedelta(seconds=wait)).isoformat()
            website_wait = max(f.get('retry_after', 0) for f in blocked)
            if website_wait:
                result['website_not_before'] = (datetime.utcnow() + timedelta(seconds=website_wait)).isoformat()
        if error_code:
            result['error_code'] = error_code
        persist(stage='done', result=result)
        return result

    if saved['stage'] == 'done':
        return saved['result']
    persist()
    while True:
        stage = saved['stage']
        replaying = (saved.get('validation_replay') or {}).get('validation_version') == VALIDATION_VERSION
        decision_ids = []
        material_pending = False
        recognition_issues = []
        origin_stage = saved.get('trial_origin_stage', 'rules') if stage == 'trial' else stage
        request_key = saved.get('trial_request_key', '')
        if stage in ('rules', 'trial'):
            detected = ((saved['validation_replay']['columns'] if replaying else saved.get('trial_columns')) if stage == 'trial' else
                        payload.get('observed_columns') or saved.get('trial_columns')) or publication_lists(html, url)
            if stage == 'trial' or saved.get('material_pending'):
                # Older handoffs already persisted rules with a pending model
                # cursor. Recover that progress as well as new trial handoffs.
                material_pending = bool(saved.get('material_pending'))
                decision_ids = saved.get('trial_decision_ids', [])
                recognition_issues = saved.get('trial_recognition_issues', [])
            columns = [feed for feed in detected if feed.get('name') and not feed.get('heading_ambiguous')
                       and (not feed.get('column_url') or canonical_url(feed['column_url']) == url)]
            if detected and not columns and all(feed.get('column_url') and canonical_url(feed['column_url']) != url for feed in detected):
                return finish('not_column', '继续读取该发布区域链接的完整栏目')
            if not columns:
                if not assistance:
                    return finish(reason='该页面暂未识别，其他栏目继续接入', error_code='rules_unresolved')
                persist(stage='ai')
                continue
        else:
            if not assistance:
                return finish(reason='该页面暂未识别，其他栏目继续接入', error_code='rules_unresolved')
            feedback = (saved.get('material_feedback', []) if saved.get('material_pending') else
                        [failure['observation'] for failure in saved['failures'] if failure.get('repairable') is not False])
            request_key = hashlib.sha256(json.dumps(feedback, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if not saved.get('material_pending') and saved.get('last_request_key') == request_key:
                return finish(error_code='no_new_validation_evidence')
            result = recognize_column(school_id, url, html, feedback=feedback or None)
            material_pending = result.get('material_complete') is False
            decision_ids = result.get('decision_ids') or ['validation-trial:' + request_key + ':' + hashlib.sha256(
                json.dumps(result.get('columns', []), sort_keys=True, ensure_ascii=False).encode()).hexdigest()]
            if result.get('error_code') in ('budget_exhausted', 'school_budget_exhausted'):
                persist(material_pending=material_pending, material_feedback=feedback)
                if handle:
                    tasks.defer(state='waiting', phase='ai_budget', capability='directory',
                        reason='额度已用完，进度已保存', error_code='budget_exhausted',
                        checkpoint=dict(tasks.current_execution()['checkpoint']))
                return {'state': 'connected' if saved['department_ids'] else 'budget_limited',
                    'department_ids': saved['department_ids'], 'new_count': saved['new_count'],
                    'issues': [f['issue'] for f in saved['failures']], 'error_code': result['error_code'],
                    'continuation_required': True, 'reason': result.get('reason', '本轮识别预算已用完，已保存后续入口')}
            if result['status'] not in ('ready', 'partial'):
                return finish(result['status'], result.get('reason', ''), result.get('error_code', ''))
            persist(material_pending=material_pending, material_feedback=feedback)
            columns = result.get('columns', [])
            if result.get('unresolved_regions'):
                recognition_issues.append({'name': payload.get('label', ''), 'reason': '部分发布区域仍待识别，已接入的栏目可以使用'})

        # A handoff caused by network I/O resumes this execution plan first.
        # Changed prompt/skill hashes cannot turn a pending read into a new fee.
        if replaying:
            persist(stage='trial')
        else:
            persist(stage='trial', trial_columns=columns, trial_origin_stage=origin_stage,
                    trial_decision_ids=decision_ids, trial_request_key=request_key,
                    trial_recognition_issues=recognition_issues)

        def retried(observation):
            # A corrected selector/name can change; matching either the named
            # column or its observed heading/list keeps omitted failures visible.
            return any(_normal(_column_label(column.get('name', ''), payload.get('group_name', ''))) ==
                       _normal(_column_label(observation['name'], payload.get('group_name', '')))
                       or any(column.get(key) and column.get(key) == observation.get(key)
                              for key in ('heading_selector', 'list_selector')) for column in columns)

        failures = (list(saved['failures']) if replaying else
                    [failure for failure in saved['failures'] if failure.get('repairable') is False
                     or not retried(failure['observation'])])
        network_error = None
        for column in columns:
            pause_if_requested()
            config = {field: column.get(field, '') for field in CONFIG_FIELDS}
            config.update(list_url=url, group_name=payload.get('group_name', ''))

            def failed(reason, codes, records=(), *, failure_kind='rule_error', retry_after=0, trial_key=''):
                repairable = failure_kind == 'rule_error'
                failures.append({'issue': {'name': config['name'], 'reason': reason, 'codes': codes},
                    'repairable': repairable, 'failure_kind': failure_kind, 'trial_key': trial_key,
                    'retry_after': retry_after, 'config': dict(config, **(scope or {})),
                    'observation': validation_feedback(column, codes, records,
                        execution=selector_observation(column, html), articles=validation.get('articles'))})
                status = 'rejected' if repairable else 'access_limited' if failure_kind == 'access_restricted' else 'material_limited'
                record_validation(school_id, config, dict(validation, status=status, errors=codes))
                db.session.commit()

            # A prior explicit rejection remains a user decision after upgrading.
            from backend.database.source_governance_models import SourceProposal
            rejected = [json.loads(p.candidate_json) for p in SourceProposal.query.filter_by(school_id=school_id, state='rejected')]
            if any(canonical_url(item['list_url']) == url and
                   (not item.get('list_selector') or item['list_selector'] == config['list_selector']) for item in rejected):
                continue
            scope = ({'container_selector': column['container_selector'], 'heading_selector': column['heading_selector']}
                     if column.get('container_selector') and column.get('heading_selector') else None)
            records, errors, region = _column_scope(config, reference, scope)
            validation = {'status': 'pending', 'page_hash': page_hash, 'list_snapshot': reference,
                          'region': region, 'articles': {}, 'pagination': {}}
            if errors or not records:
                empty = empty_region(html, config, scope) if not records else None
                if empty:
                    validation.update(status='empty', region=empty)
                    record_validation(school_id, config, validation)
                    db.session.commit()
                    if empty['name'] not in saved['empty_columns']:
                        saved['empty_columns'].append(empty['name'])
                        saved['empty_rules'].append(dict(config, **(scope or {})))
                    persist()
                    continue
                failed('提取规则未能正确读取该栏目', errors or ['list_empty'], records)
                continue
            if scope and region.get('name'):
                # The model locates the heading; only executed official text
                # names the installed column, never an invented publisher prefix.
                config['name'] = region['name']
            config_key = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            trial_key = hashlib.sha256(json.dumps({'config': {k: v for k, v in config.items() if k != 'content_selector'},
                'urls': [canonical_url(record['url']) for record in records]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if any(f.get('repairable') is False and f.get('trial_key') == trial_key for f in failures):
                # A new explanation or body selector does not make the same
                # rejected HTTP read new evidence. Other columns can still repair.
                continue
            if config_key in saved['completed']:
                continue
            existing = Department.query.filter_by(school_id=school_id, list_url=url,
                list_selector=config['list_selector'], name=config['name']).first()
            if existing and not payload.get('revalidate'):
                if existing.id not in saved['department_ids']:
                    saved['department_ids'].append(existing.id)
                saved['completed'].append(config_key)
                persist()
                continue
            try:
                for record in records:
                    validate_public_url(record['url'], resolve=False)
            except ValueError:
                failed('列表包含非公开或无效的通知地址', ['invalid_publication_url'])
                continue
            from backend.scraper.acquisition import FetchFailure
            # Execute this candidate's actual list/body region while reading
            # validation material. The initial page was read without a rule;
            # custom readers retain their original two-argument contract.
            validation_reader = partial(fetcher, config=config, exploration=True) if default_reader else fetcher
            try:
                pagination = pagination_sample(config, records, html, reference, scope, validation_reader)
            except FetchFailure as exc:
                kind = sample_failure_kind([{'reason': exc.outcome, 'error_code': exc.result.error_code}])
                if kind != 'rule_error':
                    validation['pagination'] = {'state': 'unavailable', 'url': exc.result.final_url,
                        'error_code': exc.result.error_code, 'outcome': exc.outcome}
                    failed('官网后续分页暂时无法读取，进度已保存，稍后自动重试',
                        ['pagination_access_restricted' if kind == 'access_restricted' else 'pagination_material_incomplete'],
                        records, failure_kind=kind, retry_after=exc.result.retry_after or 0, trial_key=trial_key)
                    continue
                network_error = exc
                failed('官网分页暂时无法读取，将自动重试', ['pagination_fetch_unavailable'])
                continue
            validation['pagination'] = pagination
            if pagination['errors']:
                failed('后续分页尚未通过实际读取验证', pagination['errors'], records)
                continue
            try:
                samples = article_samples(config, records + pagination.get('sample_records', []), validation_reader)
                validation['articles'] = samples
                for sample in samples['samples']:
                    if sample.get('content_hash') and sample.get('matched'):
                        proof = 'article:' + sample['final_url'] + ':' + sample['content_hash']
                        if proof not in saved['progress_ids']:
                            saved['progress_ids'].append(proof)
            except FetchFailure as exc:
                network_error = exc
                failed('官网暂时无法读取，将自动重试', ['article_fetch_unavailable'])
                continue
            if not samples['passed']:
                kind = samples.get('failure_kind', 'rule_error')
                if kind != 'rule_error':
                    code = 'article_access_restricted' if kind == 'access_restricted' else 'article_material_incomplete'
                    failed('部分原文暂时无法公开读取，进度已保存，稍后自动重试', [code], records,
                        failure_kind=kind, retry_after=samples.get('retry_after', 0), trial_key=trial_key)
                else:
                    failed('暂未读到与列表对应的公开通知正文', ['article_sample_mismatch'], records)
                continue
            validation['status'] = samples['status']
            if repair_source and not _automatic_repair_source(payload, school):
                return {'state': 'skipped', 'department_ids': [], 'reason': '该信息来源已不在订阅范围内'}
            try:
                source_id, added = _install(school_id, config, records, repair=payload.get('revalidate') is True,
                                           previous_url=payload.get('previous_url'), ai_assist=assistance,
                                           repair_department_id=repair_source.id if repair_source and len(columns) == 1 else None,
                                           validation=validation)
            except ValueError as exc:
                if str(exc) != 'column_identity_changed':
                    raise
                failed('新规则尚不能对应原栏目，现有来源继续保留', ['column_identity_changed'], records)
                continue
            if source_id not in saved['department_ids']:
                saved['department_ids'].append(source_id)
            def same_blocked_region(failure):
                old = failure.get('config') or {}
                if old.get('container_selector') and config.get('list_selector'):
                    return bool(scope and old['container_selector'] == scope['container_selector']
                                and old.get('heading_selector') == scope['heading_selector'])
                return (old.get('list_selector') == config['list_selector']
                        and _normal(old.get('name', '')) == _normal(config['name']))
            if not replaying:
                failures = [failure for failure in failures if not (failure.get('repairable') is False
                    and same_blocked_region(failure))]
            saved['new_count'] += added
            saved['completed'].append(config_key)
            if 'source:' + str(source_id) not in saved['progress_ids']:
                saved['progress_ids'].append('source:' + str(source_id))
            # Assess freshly read evidence independently of website grouping.
            from backend.services.student_information import queue_source_assessment, queue_listing_assessment
            queue_source_assessment(source_id, ai_assist=assistance)
            queue_listing_assessment(source_id, ai_assist=assistance)
            persist()
        if network_error:
            # Keep the current stage and its original model input. The queue
            # retries network I/O; connectivity failures never buy a new model turn.
            raise network_error
        persist(failures=failures)
        if replaying:
            # A validator upgrade executes only already-paid candidates once.
            # Remaining failures and unread model regions stay saved; they must
            # not turn this bounded replay into another paid repair decision.
            return finish(extra_issues=recognition_issues)
        for decision_id in decision_ids:
            saved['progress'] = advance_progress(saved['progress'], decision_id, saved['progress_ids'])
        if material_pending and not saved['progress']['stopped']:
            # Install each completed region before continuing the same model
            # material cursor. Freeze its input feedback until all regions end.
            # A legacy validator upgrade may have entered via rules. That
            # stage can execute saved plans but cannot continue the unread
            # model-material cursor; returning there would silently finish.
            continuation_stage = (origin_stage if origin_stage in ('ai', 'repair') else
                                  'repair' if saved.get('material_feedback') else 'ai')
            persist(stage=continuation_stage)
            if handle:
                tasks.defer(capability='directory', phase='source_material', delay=0,
                    reason='已接入可用栏目，继续识别其余发布区域',
                    checkpoint=dict(tasks.current_execution()['checkpoint']))
            continue
        if origin_stage != 'rules':
            persist(last_request_key=request_key)
        if any(failure.get('repairable') is not False for failure in failures) and assistance and not saved['progress']['stopped']:
            # More evidence may justify further repair decisions. Replayed
            # decisions and repeated explanations cannot manufacture progress.
            persist(stage='repair')
            continue
        return finish(error_code='no_progress' if saved['progress']['stopped'] else '', extra_issues=recognition_issues)


def resume_rule_discovery():
    """Resume obsolete internal blockers without resetting progress or user pauses."""
    from datetime import timedelta
    from backend.services.discovery_control import requested
    now = datetime.utcnow()
    from backend.services.source_agent import recover_unknown_column_checkpoint
    from backend.scraper.discovery.ai_navigation import unknown_navigation_execution
    for task in BackgroundTask.query.filter(BackgroundTask.kind.in_(('onboard', 'navigation_review')),
            BackgroundTask.state.in_(('done', 'failed', 'pending', 'waiting'))).all():
        if requested(task):
            continue
        if task.kind == 'onboard':
            checkpoint, recovered = recover_unknown_column_checkpoint(task.checkpoint)
        else:
            checkpoint = dict(task.checkpoint or {})
            recovered = not (task.state == 'waiting' and task.phase == 'ai_result') and bool(
                unknown_navigation_execution(checkpoint))
        if recovered:
            # This upgrade is a saved-work pause, never a new task generation,
            # fee allowance, read or model dispatch.
            task.checkpoint = checkpoint
            task.state, task.phase = 'waiting', 'ai_result'
            task.error_code = 'ai_result_unknown'
            task.error = '模型请求结果尚未确认，进度已保存'
            task.finished_at = task.token = task.worker_id = task.lease_until = None
            task.updated_at = now
        elif _resume_pagination_validation(task, now):
            continue
    # The old mixed cache limit could exhaust all retries before the next page.
    # Preserve generation/checkpoints so existing column children remain valid.
    for task in BackgroundTask.query.filter(
            BackgroundTask.kind.in_(('discover', 'directory')),
            BackgroundTask.state.in_(('failed', 'pending')),
            BackgroundTask.error.contains('调查缓存已达到容量上限')).all():
        if requested(task):
            continue
        from sqlalchemy import update
        db.session.execute(update(BackgroundTask).where(
            BackgroundTask.id == task.id, BackgroundTask.state == task.state,
            BackgroundTask.error == task.error,
            BackgroundTask.payload['discovery_pause_requested'].as_boolean().is_not(True)
        ).values(state='pending', phase='fetch', capability='directory',
                 available_at=now, updated_at=now, deadline_at=now + timedelta(hours=2),
                 attempts=0, finished_at=None, token=None, worker_id=None, lease_until=None,
                 error='', error_code=''), execution_options={'synchronize_session': False})
    for task in BackgroundTask.query.filter_by(kind='discover', state='waiting', phase='ai_setup'):
        if not requested(task):
            task.state, task.phase = 'pending', 'fetch'
            task.available_at = task.updated_at = now
            task.deadline_at = now + timedelta(hours=2)
            task.error = task.error_code = ''
    db.session.commit()
