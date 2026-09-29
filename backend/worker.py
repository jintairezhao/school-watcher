"""Supervised role workers sharing one durable queue and database leases."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import os
import socket
import threading
import time
from uuid import uuid4

from backend.database.db import db
from backend.database.models import BackgroundTask, School, Subscription, AppConfig
from backend.services import tasks

logger = logging.getLogger(__name__)


def _schedule_due(lease=None):
    from backend.services.inbox_refresh import collection_interval_seconds
    now = datetime.utcnow()
    interval = collection_interval_seconds()

    def due(kind, key, payload, seconds):
        if lease:
            from backend.services.runtime_leases import renew
            if not renew(lease, seconds=90):
                raise tasks.LeaseLost('Scheduler lease was lost')
        task = BackgroundTask.query.filter_by(identity=f'{kind}:{key}').first()
        checked = (task.checked_at or task.updated_at) if task else None
        next_run = task.next_run_at if task and task.kind == 'scrape' else None
        if not task or (task.state in ('done', 'failed') and (next_run <= now if next_run else checked < now - timedelta(seconds=seconds))):
            import hashlib
            delay = int(hashlib.sha256(f'{kind}:{key}'.encode()).hexdigest()[:8], 16) % min(60, max(1, seconds // 10))
            tasks.enqueue(kind, key, payload, min_interval=seconds, delay=delay)

    from backend.services.starter_catalog import managed_schools
    schools = managed_schools().filter(School.enabled.is_(True)).all()
    for school in schools:
        if school.subscriber_count > 0:
            due('scrape', school.id, {'school_id': school.id}, interval)
            due('health', school.id, {'school_id': school.id}, 86400)
        due('discover', school.id, {'school_id': school.id, 'refresh': True},
            (7 if school.subscriber_count > 0 else 30) * 86400)
    due('maintenance', 'daily', {}, 86400)
    due('backup', 'daily', {}, 86400)
    from backend.services.runtime_catalog import RuntimeCatalog
    registered = {(s.name, s.url) for s in School.query.all()}
    for site in RuntimeCatalog(__import__('flask').current_app.config['SOURCE_CATALOG_PATH']).all_sites():
        if (site['name'], site['root_url']) in registered:
            continue
        published = datetime.fromisoformat(site['updated_at']).replace(tzinfo=None)
        if published < now - timedelta(days=30):
            due('directory', site['site_key'], dict(site, refresh=True), 30 * 86400)
    AppConfig.set('worker_heartbeat', now.isoformat())
    db.session.remove()


def schedule_due(owner_id='local'):
    from backend.services.runtime_leases import acquire, release, worker_heartbeat
    lease = acquire('scheduler', owner_id, seconds=90)
    if not lease:
        return False
    try:
        from backend.services.scrape_logs import recover_interrupted_logs
        recover_interrupted_logs()
        from backend.ai.runtime import recover_uncertain_executions
        recover_uncertain_executions()
        _schedule_due(lease)
        worker_heartbeat(owner_id + ':scheduler', ['scheduler'])
        return True
    except tasks.LeaseLost:
        db.session.rollback()
        return False
    finally:
        release(lease)


def dispatch(kind, payload):
    from flask import current_app
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services.source_inventory import site_key
    if kind == 'content':
        from backend.services.content_cache import fetch_content
        return fetch_content(payload['announcement_id'])
    if kind == 'maintenance':
        from backend.services.content_cache import prune_content
        from backend.services.backups import expire_rollback
        from backend.services.discovery_cache import DiscoveryCache
        from filelock import FileLock
        from backend.database.models import RateBucket
        from backend.services.scrape_logs import prune_scrape_logs
        result = prune_content()
        RateBucket.query.filter(RateBucket.window_start < datetime.utcnow() - timedelta(days=2)).delete()
        db.session.commit()
        result['scrape_logs_deleted'] = prune_scrape_logs()
        scratch = current_app.config['DISCOVERY_CACHE_PATH']
        with FileLock(str(scratch) + '.worker.lock', timeout=0):
            DiscoveryCache(scratch).trim()
        result['rollback_expired'] = expire_rollback()
        from backend.services.runtime_leases import prune_expired
        from backend.services.task_fetch import prune_fetch_evidence
        prune_expired()
        result['fetch_evidence'] = prune_fetch_evidence()
        from backend.services.source_governance import prune_governance_evidence
        from backend.services.storage_policy import policy
        result['governance_evidence'] = prune_governance_evidence(policy()['discovery_cache_days'])
        from backend.services.runtime_catalog import prune_catalog_generations
        result['catalog_generations'] = prune_catalog_generations()
        return result
    if kind == 'backup':
        from backend.services.backups import create_backup
        return create_backup()
    if kind == 'source_baseline':
        from backend.services.source_governance import register_school_baselines
        from backend.services.discovery_cache import DiscoveryCache
        from filelock import FileLock
        path = current_app.config['DISCOVERY_CACHE_PATH']
        with FileLock(str(path) + '.worker.lock', timeout=0):
            return register_school_baselines(payload['school_id'], payload['baselines'],
                DiscoveryCache(path), payload['actor_id'], complete_roster=payload.get('complete_roster') is True)
    if kind == 'source_review':
        from backend.services.source_governance import process_source_review
        return process_source_review(payload)
    if kind == 'directory':
        from backend.services.discovery_cache import adapt_site
        checkpoint = (tasks.current_execution() or {}).get('checkpoint') or {}
        result = adapt_site(payload['name'], payload['root_url'],
                            monthly=bool(payload.get('refresh') and not checkpoint.get('directory_refresh_started')))
        from backend.services.directory_options import sync_directory_options
        catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
        for school in School.query.filter_by(url=payload['root_url'], name=payload['name']).all():
            sync_directory_options(school, catalog)
            if result.get('activated_ids') and school.subscriber_count:
                tasks.enqueue('scrape', school.id, {'school_id': school.id})
        if result.get('continuation_required'):
            checkpoint = dict((tasks.current_execution() or {}).get('checkpoint') or {})
            checkpoint.update(directory_refresh_started=True, pending_pages=result.get('pending_pages', 0))
            tasks.defer(capability='directory', phase='directory_slice', checkpoint=checkpoint, delay=1,
                        reason='本轮检查已保存，继续处理剩余部门与栏目')
        return result
    if kind == 'summary':
        from backend.services.summaries import generate_summary
        return generate_summary(payload['summary_id'])
    if kind == 'summarize':
        from backend.services.summaries import enqueue_batch
        return enqueue_batch(payload.get('ids'), requested_by=payload.get('requested_by'))
    if kind == 'selectors':
        from backend.scraper.engine import _fetch_html
        from backend.scraper.detectors.list_detector import test_selectors
        html = _fetch_html(payload['url'])
        return test_selectors(html, payload['url'], payload['list_selector'], payload.get('title_selector', 'a'),
                              payload.get('link_selector', 'a'), payload.get('date_selector', 'span'))
    school = db.session.get(School, payload['school_id'])
    if not school or not school.enabled:
        return {'skipped': True}
    if kind == 'discover':
        from backend.services.onboarding_progress import ai_available
        if payload.get('require_ai') and not ai_available():
            tasks.defer(capability='directory', phase='ai_setup', state='waiting',
                        reason='请先配置目录识别 AI', error_code='ai_not_configured')
        from backend.services.discovery_cache import adapt_site
        name, url = school.name, school.url
        db.session.commit()
        checkpoint = (tasks.current_execution() or {}).get('checkpoint') or {}
        result = adapt_site(name, url, monthly=bool(payload.get('refresh') and not checkpoint.get('directory_refresh_started')))
        school = db.session.get(School, payload['school_id'])
        if school:
            from backend.services.directory_options import sync_directory_options
            sync_directory_options(school, RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH']))
        if school and school.subscriber_count and result.get('activated_ids'):
            tasks.enqueue('scrape', school.id, {'school_id': school.id})
        if result.get('continuation_required'):
            checkpoint = dict((tasks.current_execution() or {}).get('checkpoint') or {})
            checkpoint.update(directory_refresh_started=True, pending_pages=result.get('pending_pages', 0))
            tasks.defer(capability='directory', phase='directory_slice', checkpoint=checkpoint, delay=1,
                        reason='本轮检查已保存，继续处理剩余部门与栏目')
        return result
    if not school.subscriber_count:
        return {'skipped': True, 'message': '已无订阅，暂停同步'}
    if kind in ('collect', 'source_health', 'health'):
        from backend.services.inbox_refresh import subscribed_sources
        subs = Subscription.query.filter_by(school_id=school.id).all()
        if not subs:
            return {'skipped': True, 'message': '已无订阅，暂停同步'}
        wanted = None if any(s.department_ids is None for s in subs) else {i for s in subs for i in s.department_ids}
        depts = subscribed_sources(school, wanted)
        if kind == 'health':
            jobs = [tasks.enqueue('source_health', d.id, {'school_id': school.id, 'department_id': d.id},
                                  min_interval=86400).id for d in depts]
            return {'queued': len(jobs), 'task_ids': jobs, 'message': '已安排逐个栏目检查'}
        dept = next((d for d in depts if d.id == payload['department_id']), None)
        if dept is None:
            return {'skipped': True, 'message': '该来源已不在订阅范围内'}
    if kind == 'collect':
        from backend.services.source_collection import collect_source
        from backend.database.models import ScrapeLog
        handle = tasks.current_execution() or {}
        log = ScrapeLog(school_id=school.id, source_name=dept.name, status='running',
                        task_id=handle.get('id'), task_generation=handle.get('generation'), task_token=handle.get('token'))
        db.session.add(log); db.session.commit()
        try:
            result = collect_source(dept)
            log.new_count, log.total_count = result['new_count'], result['checked']
            log.status = 'partial' if result.get('partial') else 'success'
            if result.get('partial'):
                log.error_message = result.get('message')
            return result
        except tasks.TaskDeferred as exc:
            db.session.rollback()
            log.status, log.error_message = 'waiting', str(exc)
            raise
        except tasks.LeaseLost:
            db.session.rollback()
            raise
        except Exception as exc:
            db.session.rollback()
            log.status, log.error_message = 'failed', f'{dept.name}：{exc}'
            raise
        finally:
            log.finished_at = datetime.utcnow()
            db.session.commit()
    if kind == 'source_health':
        from backend.scraper.engine import _fetch_html
        from backend.scraper.selector_monitor import evaluate_and_repair, mark_needs_review
        checked = 0
        needs_adaptation = False
        # Successful collection already checks selectors against its fetched DOM.
        # Avoid requesting the same column again when the daily audit overlaps it.
        if dept.last_scraped_at and dept.last_scraped_at >= datetime.utcnow() - timedelta(minutes=15):
            return {'checked': 1, 'adaptation_queued': False, 'reused_recent_collection': True}
        # Check every subscribed column daily; global HTTP slots bound concurrency.
        for dept in [dept]:
            url = dept.list_url
            db.session.commit()
            html = _fetch_html(url, raise_fetch_errors=True, purpose='list', source_id=str(dept.id)) if url else ''
            if html:
                result = evaluate_and_repair(dept, html)
                needs_adaptation |= result['action'] in ('needs_review', 'skip')
                checked += 1
            else:
                mark_needs_review(dept, '官网栏目暂时无法读取，已安排受限适配')
                needs_adaptation = True
        if needs_adaptation:
            tasks.enqueue('discover', school.id, {'school_id': school.id}, replace_finished=False)
        return {'checked': checked, 'adaptation_queued': needs_adaptation}
    if kind == 'scrape':
        from backend.services.source_catalog import apply_source_configs
        from backend.services.directory_options import sync_directory_options
        from backend.services.inbox_refresh import subscribed_sources, queue_sources
        catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
        sync_directory_options(school, catalog)
        configured = any(d.list_selector for d in school.departments)
        if not configured:
            tasks.enqueue('discover', school.id, {'school_id': school.id,
                'name': school.name, 'root_url': school.url}, replace_finished=False)
        subs = Subscription.query.filter_by(school_id=school.id).all()
        wanted = None if any(s.department_ids is None for s in subs) else {i for s in subs for i in s.department_ids}
        jobs = queue_sources(subscribed_sources(school, wanted), manual=payload.get('manual') is True)
        return {'new_count': 0, 'queued': len(jobs), 'task_ids': jobs, 'message': f'已安排 {len(jobs)} 个来源增量更新'}
    raise ValueError('Unknown task kind')


def execute(app, handle, pause=None):
    from backend.services.desktop_maintenance import before_fetch
    stop = threading.Event()
    handle['cancelled'] = threading.Event()
    def renew():
        while not stop.wait(20):
            with app.app_context():
                try:
                    if not tasks.heartbeat(handle):
                        handle['cancelled'].set()
                        return
                except Exception:
                    # Fail closed: continued ownership is required for any write.
                    handle['cancelled'].set()
                    logger.exception('Task lease renewal failed for %s', handle['id'])
                    return
                finally:
                    db.session.remove()
    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    with app.app_context():
        try:
            from backend.scraper.acquisition import execution_context, profile_fingerprint, FetchFailure
            from backend.services.task_fetch import browser_dispatch, cache_lookup, cache_store
            fingerprint = profile_fingerprint()
            handle['policy_validator'] = lambda: profile_fingerprint() == fingerprint
            with tasks.execution_scope(handle), execution_context(browser_dispatch=browser_dispatch,
                    cache_lookup=cache_lookup, cache_store=cache_store, before_fetch=lambda: before_fetch(pause)):
                before_fetch(pause)
                try:
                    result = dispatch(handle['kind'], dict(handle['payload']))
                except FetchFailure as exc:
                    if exc.outcome != 'needs_manual':
                        raise
                    request = handle.get('_fetch_request')
                    if request is None:
                        raise
                    from urllib.parse import urlsplit
                    parsed = urlsplit(request.url)
                    source_id = request.source_id or str(handle['payload'].get('department_id') or handle['identity'])
                    tasks.require_verification(source_id, request.url, f'{parsed.scheme}://{parsed.netloc}',
                                               str(exc), request_payload=request.to_dict())
                tasks.finish(handle, result=result)
        except tasks.TaskDeferred as deferred:
            db.session.rollback()
            tasks.handoff(handle, deferred)
        except tasks.PolicyChanged:
            db.session.rollback()
            tasks.restart_configuration(handle)
        except tasks.LeaseLost:
            db.session.rollback()
            logger.info('Discarded stale task execution %s', handle['id'])
        except Exception as exc:
            db.session.rollback()
            logger.exception('Task %s failed', handle['id'])
            tasks.finish(handle, error=exc, retryable=getattr(exc, 'retryable', True))
        finally:
            stop.set()
            db.session.remove()
            heartbeat.join()


def run(app, *, once=False, roles=None, concurrency=None, worker_id=None):
    from filelock import FileLock
    from pathlib import Path
    from backend.services.runtime_leases import worker_heartbeat
    roles = tuple(roles or ('http', 'browser', 'directory', 'scheduler'))
    if not set(roles).issubset((*tasks.CAPABILITIES, 'scheduler')):
        raise ValueError('Unknown worker role')
    worker_id = worker_id or f'{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}'
    slots = max(1, min(16, int(concurrency or app.config.get('WORKER_CONCURRENCY', 2))))
    lanes = tuple(role for role in roles if role in tasks.CAPABILITIES)
    with app.app_context():
        sqlite = db.engine.dialect.name == 'sqlite'
    lock_path = Path(app.config.get('WORKER_LOCK_PATH', str(Path(app.config['SOURCE_CATALOG_PATH']).parent / 'worker.lock')))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=0) if sqlite else nullcontext()
    with lock, ThreadPoolExecutor(max_workers=slots) as pool:
        from backend.services.desktop_maintenance import WorkerMaintenance
        maintenance = WorkerMaintenance(app)
        active = {}
        last_schedule = 0
        last_heartbeat = 0
        while True:
            for future in [future for future in active if future.done()]:
                future.result()
                del active[future]
            if maintenance.poll(len(active)):
                with app.app_context():
                    worker_heartbeat(worker_id, roles, stopped=True)
                return
            if not maintenance.pause.is_set() and 'scheduler' in roles and time.monotonic() - last_schedule > 30:
                with app.app_context():
                    schedule_due(worker_id)
                last_schedule = time.monotonic()
            if time.monotonic() - last_heartbeat > 20:
                with app.app_context():
                    worker_heartbeat(worker_id, roles)
                last_heartbeat = time.monotonic()
            while not maintenance.pause.is_set() and lanes and len(active) < slots:
                maintenance.poll(len(active))
                if maintenance.pause.is_set():
                    break
                with app.app_context():
                    handle = tasks.claim(capabilities=lanes, worker_id=worker_id)
                    db.session.remove()
                if not handle:
                    break
                active[pool.submit(execute, app, handle, maintenance.pause)] = handle['capability']
            if once:
                for future in active:
                    future.result()
                with app.app_context():
                    worker_heartbeat(worker_id, roles, stopped=True)
                return
            time.sleep(1)
