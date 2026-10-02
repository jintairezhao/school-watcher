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
        url = canonical_url(page.get('final_url') or page['url'])
        configs = by_url.get(url, [])
        if page['state'] != 'fetched' or not (configs or page['kind'] == 'channel'):
            continue
        if not configs and not same_school_url(url, report['site']['root_url']):
            continue
        existing = Department.query.filter_by(school_id=school_id, list_url=url).all()
        if configs and all(any(d.list_selector == c['list_selector'] and d.name == c['name']
                               for d in existing) for c in configs):
            continue
        identity = f'{school_id}:' + hashlib.sha256(url.encode()).hexdigest()[:24]
        old = BackgroundTask.query.filter_by(identity='onboard:' + identity).first()
        # Finished pages only reopen on an explicit refresh. Crawl slices and
        # process restarts must not repeat paid calls for the same unresolved page.
        if old:
            continue
        html = inventory.snapshot(key, page['url'])
        snapshot = _snapshot(html, url, role='list') if html else None
        groups = {c['group_name'] for c in configs if c.get('group_name')}
        payload = {'school_id': school_id, 'url': url,
                   'group_name': next(iter(groups)) if len(groups) == 1 else '',
                   'label': page['label'], 'snapshot': snapshot,
                   'official_external': bool(configs and not same_school_url(url, report['site']['root_url']))}
        job = tasks.enqueue('onboard', identity, payload, replace_finished=False)
        jobs.append(job.id)
    return {'onboarding_ids': jobs, 'proposal_ids': [], 'activated_ids': [], 'remaining_candidates': 0}


def retry_unconnected_columns(school_id, refresh_key):
    """An explicit new run refreshes failed pages, never replays their old DOM."""
    rows = BackgroundTask.query.filter(BackgroundTask.kind == 'onboard',
        BackgroundTask.payload['school_id'].as_integer() == school_id,
        BackgroundTask.state.in_(('done', 'failed'))).all()
    ids = []
    for row in rows:
        payload, result = dict(row.payload or {}), row.result or {}
        if payload.get('refresh_key') == refresh_key:
            continue
        configured = result.get('department_ids', [])
        if result.get('state') == 'connected' and not result.get('issues') and configured and all(
                (source := db.session.get(Department, ident)) and source.list_selector for ident in configured):
            continue
        payload.pop('snapshot', None)
        payload['refresh_key'] = refresh_key
        job = tasks.enqueue('onboard', row.identity.split(':', 1)[1], payload, replace_finished=True)
        ids.append(job.id)
    return ids


def recognize_column(school_id, url, html):
    """One bounded, replayable model call. Its output is still an untested parser."""
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import canonical, digest
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        return {'status': 'unsupported', 'columns': [], 'reason': '该页面暂未识别，可启用 AI 辅助解析'}
    soup = BeautifulSoup(html, 'lxml')
    # Preserve DOM positions/attributes used by selectors. Replacing large script
    # contents (not their elements) cannot change nth-child paths in the original.
    for node in soup.select('script,style,svg'):
        node.clear()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.startswith('on') or attr in ('style', 'srcset') or (attr == 'src' and str(node[attr]).startswith('data:')):
                del node.attrs[attr]
    evidence = {'school_id': school_id, 'candidates': [{'candidate_id': 'page', 'url': url}],
                'evidence': [{'evidence_id': 'page', 'url': url, 'html': str(soup)}]}
    if len(canonical(evidence).encode()) > 24 * 1024:
        return {'status': 'unsupported', 'columns': [], 'reason': '页面结构超过单次识别容量，需补充适配'}
    handle = tasks.current_execution() or {}
    execution_id = f"column:{handle.get('id', school_id)}:{handle.get('generation', 1)}:{digest(evidence)[:24]}:{binding['id']}:{binding['version']}"
    try:
        result = run_skill('university-source-onboarding', 'column', evidence, 'directory', execution_id, binding=binding)
    except AIConfigError as exc:
        return {'status': 'unsupported', 'columns': [], 'reason': 'AI 辅助暂不可用，其他栏目继续接入', 'error_code': exc.code}
    if result['status'] != 'succeeded':
        return {'status': 'unsupported', 'columns': [], 'reason': 'AI 未返回可执行的栏目规则',
                'error_code': result.get('error_code', '')}
    return result['output']


def _read(url, purpose):
    from backend.services.source_governance import _fetch
    return _fetch(url, purpose)


def _sample_matches(config, records, fetcher):
    from backend.services.source_governance import _normal
    from backend.scraper.acquisition import FetchFailure
    last_error = None
    for record in records[:2]:
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
        if title and title[:20] in visible and len(body_text) >= 15 and len(visible) >= len(title) + 15:
            return True
    if last_error and last_error.outcome in ('network_error', 'unavailable'):
        raise last_error  # The existing queue owns bounded network retries.
    return False


def _install(school_id, config, records):
    """Keep source/version/notices/next collection in the same commit fence."""
    from backend.services.source_governance import _hash, _json
    from backend.services.announcement_identity import upsert_listing
    from backend.services.announcement_sources import record_source
    from backend.scraper.change_detector import parse_date
    from backend.scraper.detectors.title_quality import date_from_url
    from backend.services.inbox_refresh import subscribed_sources
    tasks.assert_owned()
    db.session.execute(db.update(School).where(School.id == school_id).values(name=School.name))
    source = Department.query.filter_by(school_id=school_id, list_url=config['list_url'],
                                        list_selector=config['list_selector']).first()
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
        job = tasks.enqueue('collect', source.id, {'school_id': school_id, 'department_id': source.id},
                            replace_finished=False, commit=False)
        if job.state == 'pending' and job.claim_count == 0:
            job.phase = 'onboarding_collection'
    tasks.assert_owned()
    db.session.commit()
    return source.id, new_count


def onboard_page(payload, *, fetcher=None):
    from backend.services.source_governance import _snapshot, read_snapshot, _column_scope
    from backend.services.discovery_control import pause_if_requested
    pause_if_requested()
    school = db.session.get(School, payload['school_id'])
    if not school or not school.enabled:
        return {'state': 'skipped', 'department_ids': []}
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
    detected = publication_lists(html, url)
    columns = [feed for feed in detected if feed.get('name') and not feed.get('heading_ambiguous')
               and (not feed.get('column_url') or canonical_url(feed['column_url']) == url)]
    if detected and not columns and all(feed.get('column_url') and canonical_url(feed['column_url']) != url for feed in detected):
        return {'state': 'not_column', 'department_ids': [], 'reason': '继续读取该发布区域链接的完整栏目'}
    if not columns:
        result = recognize_column(school_id, url, html)
        if result['status'] != 'ready':
            return {'state': result['status'], 'department_ids': [], 'reason': result.get('reason', '')}
        columns = result['columns']
    ids, issues, new_count, network_error = [], [], 0, None
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
        if existing:
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
        source_id, added = _install(school_id, config, records)
        ids.append(source_id); new_count += added
    if network_error:
        raise network_error
    return {'state': 'connected' if ids else 'unsupported', 'department_ids': ids,
            'new_count': new_count, 'issues': issues,
            'reason': issues[0]['reason'] if issues else ''}


def resume_rule_discovery():
    """Upgrade only jobs parked for missing AI; explicit user pauses stay intact."""
    from datetime import timedelta
    from backend.services.discovery_control import requested
    now = datetime.utcnow()
    for task in BackgroundTask.query.filter_by(kind='discover', state='waiting', phase='ai_setup'):
        if not requested(task):
            task.state, task.phase = 'pending', 'fetch'
            task.available_at = task.updated_at = now
            task.deadline_at = now + timedelta(hours=2)
            task.error = task.error_code = ''
    db.session.commit()
