"""Discover a page, execute its parser, and publish its first notices.

Each page is independent durable work. Existing proposal workflows are reserved
for legacy/admin edits; automatic onboarding never creates a review proposal.
"""
from datetime import datetime
import hashlib
import json

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import BackgroundTask, Department, DepartmentDirectoryEntry, School, Subscription
from backend.database.source_governance_models import SourceConfigVersion
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.article_resources import has_public_body_resource as _public_body_resource
from backend.scraper.http_client import same_school_url, validate_public_url
from backend.services import tasks
from backend.services.source_inventory import canonical_url

CONFIG_FIELDS = ('name', 'list_url', 'list_selector', 'title_selector', 'link_selector',
                 'date_selector', 'content_selector', 'group_name')


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
        if page['kind'] == 'directory':
            # Institution rosters establish ownership, not publication columns.
            continue
        url = canonical_url(page.get('final_url') or page['url'])
        configs = by_url.get(url, [])
        previous_url = canonical_url(page['url'])
        existing = Department.query.filter(Department.school_id == school_id,
                    Department.list_url.in_({url, previous_url})).all()
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
        if old:
            unresolved = failed_rule or (old.result or {}).get('state') != 'connected' or bool((old.result or {}).get('issues'))
            if (old.state not in ('done', 'failed') or
                    old.payload.get('source_shape') == shape.get('hash') and not (manual and unresolved and old.payload.get('refresh_key') != run_key) or
                    not changed and not (manual and unresolved)):
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
        query = query.filter(checked <= datetime.utcnow() - timedelta(hours=6))
    for row in query.order_by(checked, BackgroundTask.id):
        result = row.result or {}
        if requested(row):
            continue
        if automatic and not (row.state == 'failed' or result.get('state') == 'unsupported'
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
        if handle.get('payload', {}).get('school_id') == school_id:
            payload.update(parent_task_id=handle['id'], parent_generation=handle['generation'],
                           ai_assist=handle['payload'].get('ai_assist', True))
        job = tasks.enqueue('onboard', row.identity.split(':', 1)[1], payload,
                            replace_finished=True, commit=commit)
        ids.append(job.id)
        if automatic and len(ids) >= max(1, limit):
            break
    return ids


def recognize_column(school_id, url, html):
    """One bounded, replayable model call. Its output is still an untested parser."""
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import digest, load_skill
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        return {'status': 'unsupported', 'columns': [], 'reason': '该页面暂未识别，可启用 AI 辅助解析'}
    from backend.scraper.discovery.column_regions import materials
    regions = materials(school_id, url, html)
    if not regions:
        return {'status': 'unsupported', 'columns': [], 'reason': '尚未定位到可独立读取的发布区域，入口已保留'}
    handle = tasks.current_execution() or {}
    checkpoint = dict(handle.get('checkpoint') or {})
    material_hash = digest(regions)
    saved = checkpoint.get('column_regions', {})
    if saved.get('hash') != material_hash:
        saved = {'hash': material_hash, 'cursor': 0, 'columns': [], 'unresolved': []}
    for index in range(saved['cursor'], len(regions)):
        evidence = regions[index]
        execution_id = 'column-region:' + digest([load_skill('university-source-onboarding', 'column').resource_digest,
            evidence, binding['id'], binding['version']])
        try:
            result = run_skill('university-source-onboarding', 'column', evidence, 'directory', execution_id, binding=binding)
        except AIConfigError as exc:
            if exc.code == 'concurrency_limit' and handle:
                tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲', checkpoint=checkpoint)
            columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
            return {'status': 'ready' if columns else 'unsupported', 'columns': columns, 'reason':
                    '本轮 AI 用量已达上限，未完成入口已保留' if exc.code == 'budget_exhausted' else 'AI 辅助暂不可用，其他栏目继续接入',
                    'unresolved_regions': len(saved['unresolved']) + len(regions) - index,
                    'error_code': exc.code}
        if result['status'] == 'pending' and handle:
            tasks.defer(capability='directory', delay=10, reason='等待已提交的栏目识别', checkpoint=checkpoint)
        if result['status'] == 'succeeded' and result['output']['status'] == 'ready':
            saved['columns'].extend(result['output']['columns'])
        elif result['status'] != 'succeeded' or result['output']['status'] == 'unsupported':
            saved['unresolved'].append(index)
        saved['cursor'] = index + 1
        checkpoint['column_regions'] = saved
        if handle:
            tasks.checkpoint(checkpoint)
            if index + 1 < len(regions):
                tasks.defer(capability='directory', delay=1, reason='继续识别下一个发布区域', checkpoint=checkpoint)
    columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
    return {'status': 'ready' if columns else 'unsupported' if saved['unresolved'] else 'not_column', 'columns': columns,
            'reason': '部分发布区域仍待识别' if saved['unresolved'] else '',
            'unresolved_regions': len(saved['unresolved'])}


def _read(url, purpose):
    from backend.services.source_governance import _fetch
    return _fetch(url, purpose)


def _sample_matches(config, records, fetcher):
    from backend.services.source_governance import _normal
    from backend.scraper.acquisition import FetchFailure
    last_error = None
    # Pinned items may have expired or require login. A small bounded fallback
    # sample must not reject an otherwise public list based on only those items.
    for record in records[:5]:
        db.session.commit()
        validate_public_url(record['url'], resolve=False)
        try:
            html = fetcher(record['url'], 'article')
        except FetchFailure as exc:
            last_error = exc
            continue
        soup = BeautifulSoup(html, 'lxml')
        for node in soup.select('script,style,nav,header,footer'):
            node.decompose()
        visible = _normal(soup.get_text(' ', strip=True))
        title = _normal(record['title'])
        body = soup.select(config['content_selector']) if config.get('content_selector') else [soup]
        body_text = _normal(' '.join(node.get_text(' ', strip=True) for node in body))
        resources = len(body_text) < 15 and bool(config.get('content_selector')) and _public_body_resource(body, record['url'])
        if title and title[:20] in visible and (len(body_text) >= 15 or resources) and len(visible) >= len(title) + 15:
            return True
    if last_error and last_error.outcome in ('network_error', 'unavailable'):
        raise last_error  # The existing queue owns bounded network retries.
    return False


def _install(school_id, config, records, *, repair=False, previous_url=None, ai_assist=True, repair_department_id=None):
    """Keep source/version/notices/next collection in the same commit fence."""
    from backend.services.source_governance import _hash, _json
    from backend.services.announcement_identity import upsert_listing
    from backend.services.announcement_sources import record_source
    from backend.scraper.change_detector import parse_date
    from backend.scraper.detectors.title_quality import date_from_url
    from backend.services.inbox_refresh import subscribed_sources
    tasks.assert_owned()
    db.session.execute(db.update(School).where(School.id == school_id).values(name=School.name))
    source = db.session.get(Department, repair_department_id) if repair_department_id else None
    if source is not None and (source.school_id != school_id or canonical_url(source.list_url) not in {
            canonical_url(config['list_url']), canonical_url(previous_url or '')}):
        raise ValueError('修复目标与已读取的官网页面不一致')
    if source is None:
        source = Department.query.filter_by(school_id=school_id, list_url=config['list_url'],
                                            list_selector=config['list_selector']).first()
    if source is None and repair:
        matches = Department.query.filter_by(school_id=school_id, list_url=config['list_url'], name=config['name']).all()
        if len(matches) == 1:
            source = matches[0]
        elif not matches and previous_url:
            # Only an observed HTTP redirect proves a moved source. Similar
            # names at unrelated new URLs do not justify moving a subscription.
            prior = Department.query.filter_by(school_id=school_id, list_url=previous_url, name=config['name']).all()
            if len(prior) == 1:
                source = prior[0]
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
        legacy = Department.query.filter_by(school_id=school_id, name=config['group_name'], kind='column').all()
        for parent in legacy if len(legacy) == 1 else []:
            if not parent.list_selector and parent.id != source.id and not DepartmentDirectoryEntry.query.filter_by(
                    parent_id=parent.id, department_id=source.id).first():
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
    fetcher = fetcher or _read
    handle = tasks.current_execution()
    checkpoint = dict((handle or {}).get('checkpoint') or {})
    reference = checkpoint.get('onboarding_page') or payload.get('snapshot')
    html = ''
    if reference:
        try:
            html = read_snapshot(reference)
        except (ValueError, OSError):
            pass
    if not html:
        db.session.commit()
        html = fetcher(url, 'directory')
        final = canonical_url(getattr(html, 'final_url', url))
        if not same_school_url(final, root_url) and not (payload.get('official_external') and same_school_url(final, url)):
            return {'state': 'unsupported', 'department_ids': [], 'reason': '网页跳转到了未确认的外部网站'}
        url = final
        reference = _snapshot(html, url, role='list')
    else:
        url = canonical_url(reference['url'])
    if handle:
        tasks.checkpoint(dict(checkpoint, onboarding_page=reference))
    detected = [] if _force_ai else publication_lists(html, url)
    columns = [feed for feed in detected if feed.get('name') and not feed.get('heading_ambiguous')
               and (not feed.get('column_url') or canonical_url(feed['column_url']) == url)]
    if detected and not columns and all(feed.get('column_url') and canonical_url(feed['column_url']) != url for feed in detected):
        return {'state': 'not_column', 'department_ids': [], 'reason': '继续读取该发布区域链接的完整栏目'}
    recognition_issues = []
    if not columns:
        # Explicitly disabled assistance also applies to inherited child jobs.
        # Rules still run normally; an unresolved page cannot start a paid call.
        parent_ai = parent.payload.get('ai_assist') if payload.get('parent_task_id') else None
        if payload.get('ai_assist') is False or parent_ai is False:
            return {'state': 'unsupported', 'department_ids': [],
                    'reason': '该页面暂未识别，其他栏目继续接入', 'error_code': 'rules_unresolved'}
        result = recognize_column(school_id, url, html)
        if result['status'] != 'ready':
            return {'state': result['status'], 'department_ids': [], 'reason': result.get('reason', ''),
                    'error_code': result.get('error_code', '')}
        columns = result['columns']
        if result.get('unresolved_regions'):
            recognition_issues.append({'name': payload.get('label', ''), 'reason': '部分发布区域仍待识别，已接入的栏目可以使用'})
    assistance = payload.get('ai_assist') is not False
    if payload.get('parent_task_id') and parent.payload.get('ai_assist') is False:
        assistance = False
    ids, issues, new_count, network_error = [], recognition_issues, 0, None
    for column in columns:
        pause_if_requested()
        config = {field: column.get(field, '') for field in CONFIG_FIELDS}
        config.update(list_url=url, group_name=payload.get('group_name', ''))
        # A prior explicit rejection remains a user decision after upgrading.
        from backend.database.source_governance_models import SourceProposal
        rejected = [json.loads(p.candidate_json) for p in SourceProposal.query.filter_by(school_id=school_id, state='rejected')]
        if any(canonical_url(item['list_url']) == url and
               (not item.get('list_selector') or item['list_selector'] == config['list_selector']) for item in rejected):
            continue
        existing = Department.query.filter_by(school_id=school_id, list_url=url,
            list_selector=config['list_selector'], name=config['name']).first()
        if existing and not payload.get('revalidate'):
            ids.append(existing.id)
            continue
        scope = ({'container_selector': column['container_selector'], 'heading_selector': column['heading_selector']}
                 if column.get('container_selector') and column.get('heading_selector') else None)
        records, errors, _ = _column_scope(config, reference, scope)
        if errors or not records:
            issues.append({'name': config['name'], 'reason': '提取规则未能正确读取该栏目', 'codes': errors})
            continue
        try:
            for record in records:
                validate_public_url(record['url'], resolve=False)
        except ValueError:
            issues.append({'name': config['name'], 'reason': '列表包含非公开或无效的通知地址'})
            continue
        from backend.scraper.acquisition import FetchFailure
        try:
            matches = _sample_matches(config, records, fetcher)
        except FetchFailure as exc:
            network_error = exc
            issues.append({'name': config['name'], 'reason': '官网暂时无法读取，将自动重试'})
            continue
        if not matches:
            issues.append({'name': config['name'], 'reason': '暂未读到与列表对应的公开通知正文'})
            continue
        if repair_source and not _automatic_repair_source(payload, school):
            return {'state': 'skipped', 'department_ids': [], 'reason': '该信息来源已不在订阅范围内'}
        source_id, added = _install(school_id, config, records, repair=payload.get('revalidate') is True,
                                    previous_url=payload.get('previous_url'), ai_assist=assistance,
                                    repair_department_id=repair_source.id if repair_source and len(columns) == 1 else None)
        ids.append(source_id); new_count += added
        # Assess the evidence we just read, independently of website grouping.
        # Queueing is cheap, consent follows enabled directory assistance, and
        # the shared AI runtime owns budgets and deduplication.
        from backend.services.student_information import queue_source_assessment, queue_listing_assessment
        queue_source_assessment(source_id, ai_assist=assistance)
        queue_listing_assessment(source_id, ai_assist=assistance)
    if network_error:
        raise network_error
    if not ids and issues and detected and not _force_ai:
        # A plausible but invalid generic rule must not suppress the AI route.
        return onboard_page(dict(payload, snapshot=reference), fetcher=fetcher, _force_ai=True)
    return {'state': 'connected' if ids else 'unsupported', 'department_ids': ids,
            'new_count': new_count, 'issues': issues,
            'reason': issues[0]['reason'] if issues else ''}


def resume_rule_discovery():
    """Resume obsolete internal blockers without resetting progress or user pauses."""
    from datetime import timedelta
    from backend.services.discovery_control import requested
    now = datetime.utcnow()
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
