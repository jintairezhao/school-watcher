"""Durable discovery progress with an independently bounded HTML cache."""
from datetime import datetime, timezone

from backend.services.source_inventory import Inventory, canonical_url


class DiscoveryCache(Inventory):
    max_bytes = 100 * 1024 * 1024
    max_pages_per_site = None

    def __init__(self, path):
        super().__init__(path)
        from backend.services.discovery_snapshots import DiscoverySnapshots
        self.snapshots = DiscoverySnapshots(self.path)
        if self.snapshots.migrate(self):
            self._compact_progress()

    def _write_snapshot(self, connection, row):
        self.snapshots.put(row)

    def snapshot(self, key, url):
        page = self.get_page(key, url)
        return self.snapshots.get(key, url, page.get('content_hash')) if page else None

    def _compact_progress(self):
        with self.connect() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('VACUUM')

    def enqueue(self, key, url, label, kind, depth, path, authority):
        with self.connect() as c:
            count = c.execute('SELECT count(*) FROM pages WHERE site_key=?', (key,)).fetchone()[0]
            known = c.execute('SELECT 1 FROM pages WHERE site_key=? AND url=?', (key, url)).fetchone()
        if self.max_pages_per_site and count >= self.max_pages_per_site and not known:
            raise RuntimeError('本校待检查入口达到容量上限，已保留进度，不能认定检查完成')
        return super().enqueue(key, url, label, kind, depth, path, authority)

    def record_edges(self, key, parent_url, links, content_hash):
        # Intermediate navigation is evidence of a discovered route, including
        # undergraduate/admission pages beyond the fourth level.
        return super().record_edges(key, parent_url, links, content_hash)

    def record_structure(self, key, reference_url, nodes, content_hash):
        from backend.services.source_relationships import ROSTER_RELATIONS, PATH_RELATIONS
        relations = ROSTER_RELATIONS | PATH_RELATIONS | {
            'major_directory_entry', 'programme_college', 'programme_joint_group',
            'publication_column', 'unit_channel', 'navigation_entry', 'unit_profile_entry',
            'hidden_directory_entry', 'directory_label_pending'}
        nodes = [n for n in nodes if n['relation'] in relations]
        with self.connect() as c:
            c.execute('DELETE FROM structure WHERE site_key=? AND reference_url=?', (key, reference_url))
        super().record_structure(key, reference_url, nodes, content_hash)

    def trim(self, *, all_cache=False):
        from flask import has_app_context
        from backend.services.storage_policy import policy, retention_cutoff
        days = policy()['discovery_cache_days'] if has_app_context() else 7
        limit = policy()['discovery_cache_mb'] * 1048576 if has_app_context() else self.max_bytes
        cutoff = retention_cutoff(days, datetime.now(timezone.utc)).isoformat(timespec='seconds') if days else None
        migrated = self.snapshots.migrate(self)
        deleted = self.snapshots.trim(cutoff=cutoff, limit=limit, all_cache=all_cache)
        with self.connect() as c:
            c.execute('DELETE FROM structure_history')
            c.execute('DELETE FROM edge_history')
            c.execute('DELETE FROM edges WHERE NOT EXISTS (SELECT 1 FROM pages p '
                      'WHERE p.site_key=edges.site_key AND p.url=edges.parent_url)')
            page_count = c.execute('PRAGMA page_count').fetchone()[0]
            free_pages = c.execute('PRAGMA freelist_count').fetchone()[0]
        # Visited pages, pending work and current official routes are progress,
        # not disposable cache. They must survive cleanup and never consume the
        # HTML budget. Reclaim dead history without deleting this visited set.
        if migrated or free_pages > max(256, page_count // 5):
            self._compact_progress()
        return deleted


def adapt_site(name, root_url, *, monthly=False):
    from flask import current_app
    from filelock import FileLock
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.scraper.discovery.inventory_crawler import crawl_site
    path = current_app.config['DISCOVERY_CACHE_PATH']
    with FileLock(str(path) + '.worker.lock', timeout=0):
        inventory = DiscoveryCache(path)
        inventory.trim()
        key = inventory.ensure_site(name, root_url)
        from backend.scraper.discovery.layered import prepare
        upgraded = prepare(inventory, key, policy='valuable')
        catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
        from backend.database.models import School
        from backend.services import tasks
        from backend.services.source_onboarding import queue_columns
        school = next((s for s in School.query.filter_by(name=name).all()
                       if canonical_url(s.url) == canonical_url(root_url)), None)
        handle = tasks.current_execution()
        checkpoint = dict((handle or {}).get('checkpoint') or {})
        page_budget = max(1, int(current_app.config.get('DISCOVERY_PAGE_BUDGET', 40)))
        checked_this_round = checkpoint.get('student_scan_pages', 0)
        starting = not checkpoint.get('student_scan_started')
        pending_before = inventory.report(key)['states'].get('pending', 0)
        continuing = bool(pending_before) and not upgraded
        if starting and not pending_before:
            # Finish already observed navigation in a later bounded round.
            # Current decisions remain cached; only unseen links reach AI.
            with inventory.connect() as connection:
                connection.execute("UPDATE pages SET state='pending' WHERE site_key=? AND state='fetched' "
                    "AND url IN (SELECT parent_url FROM edges WHERE site_key=? AND decision='navigation_pending')", (key, key))
            continuing = bool(inventory.report(key)['states'].get('pending', 0))
        if monthly and not continuing:
            from backend.services.discovery_changes import seed_check
            refresh_key = f"{handle['id']}:{handle['generation']}" if handle else datetime.now(timezone.utc).isoformat()
            extras = []
            if school:
                from backend.database.models import BackgroundTask
                sources = [(d.list_url, d.name) for d in school.departments if d.list_selector and d.list_url]
                sources += [(j.payload['url'], j.payload.get('label', '')) for j in BackgroundTask.query.filter_by(kind='onboard')
                            if j.payload.get('school_id') == school.id and j.payload.get('url')]
                extras = [dict(url=url, label=label, kind='channel', depth=3, path_json='[]', authority='official_backlink')
                          for url, label in sources]
            seed_check(inventory, key, catalog, refresh_key, extras)
        if handle:
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, directory_refresh_started=True, student_scan_started=True))
        from backend.services.onboarding_progress import record_progress
        from backend.services.school_structure import sync_official_structure
        queued_columns = []
        def progress(processed, snapshot):
            states = snapshot['states']
            if handle:
                tasks.checkpoint(dict(handle.get('checkpoint') or {}, student_scan_pages=checked_this_round + processed))
            record_progress(phase='crawl', checked_pages=sum(v for k, v in states.items() if k not in ('pending', 'running')),
                pending_pages=states.get('pending', 0) + states.get('running', 0),
                failed_pages=states.get('failed', 0) + states.get('blocked', 0),
                unit_checked=snapshot.get('unit_checked', 0), unit_pending=snapshot.get('unit_pending', 0),
                unit_failed=snapshot.get('unit_failed', 0),
                current_label=snapshot.get('current_label', ''))
            if processed:
                catalog.publish(inventory, key, merge=True)
                if school:
                    sync_official_structure(school, catalog)
                    queued_columns.extend(queue_columns(school.id, inventory, key)['onboarding_ids'])
        background_retry = starting and (handle or {}).get('payload', {}).get('trigger') == 'background_discovery'
        result = crawl_site(inventory, key, max_pages=min(3, max(0, page_budget - checked_this_round)), workers=2,
                            focus='valuable', retry_failed=monthly or background_retry, progress=progress)
        checked_this_round += result.get('processed_this_run', 0)
        from backend.services.discovery_control import pause_if_requested
        pause_if_requested()
        from backend.services.source_governance import record_onboarding_slice
        from backend.services.source_relationships import ROSTER_RELATIONS
        governance = {'proposal_ids': [], 'activated_ids': [], 'remaining_candidates': 0}
        if school:
            governance = queue_columns(school.id, inventory, key)
            governance['onboarding_ids'] = list(dict.fromkeys(queued_columns + governance['onboarding_ids']))
            result['official_units'] = [n for n in inventory.structure(key) if n['kind'] == 'unit' and n['relation'] in ROSTER_RELATIONS]
            record_onboarding_slice(school.id, result)
        # Publish navigation separately; each page installs its first notices in
        # an independent onboarding task without waiting for the school crawl.
        catalog.publish(inventory, key, merge=True)
        if school:
            from backend.services.school_structure import sync_official_structure
            sync_official_structure(school, catalog)
        inventory.trim()
        pending = result['states'].get('pending', 0) + result['states'].get('running', 0)
        with inventory.connect() as connection:
            deferred_routes = connection.execute("SELECT count(*) FROM edges WHERE site_key=? AND decision='navigation_pending'", (key,)).fetchone()[0]
        page_limited = bool(pending and checked_this_round >= page_budget)
        limited = page_limited or bool((handle or {}).get('checkpoint', {}).get('navigation_budget_limited'))
        entry = next((p for p in result['pages'] if p['kind'] == 'root' and p['state'] in ('failed', 'blocked')), None)
        entry_failure = ({'reason': entry.get('error') or '学校官网暂未读取成功',
                          'status_code': entry.get('status_code'), 'health': entry.get('health')}
                         if entry and not result['states'].get('fetched') else None)
        if handle:
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, first_slice_finished=True))
        from backend.services.discovery_changes import counts
        changes = counts(result)
        record_progress(phase='complete' if page_limited else 'crawl' if pending or governance['remaining_candidates'] else 'complete',
                        entry_failure=entry_failure, changes=changes, exploration_limited=limited,
                        deferred_pages=pending if page_limited else 0)
        return {'departments': catalog.candidates(key), 'pending_pages': pending,
                'changes': changes,
                'entry_failure': entry_failure,
                'continuation_required': bool(pending or governance['remaining_candidates']) and not page_limited, **governance,
                'discovery_policy': 'student-routes-1', 'exploration_limited': limited,
                'deferred_pages': pending if page_limited else 0, 'deferred_routes': deferred_routes,
                'coverage_verified': False, 'message': '本轮优先信息已检查，其他线索已保留供后续自动补充' if limited else '已找到的信息正在接入，继续寻找对学生有用的内容'}
