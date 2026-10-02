"""Bounded scratch space for resumable adaptation; published evidence lives elsewhere."""
from datetime import datetime, timedelta, timezone
import json

from backend.services.source_inventory import Inventory, canonical_url


class DiscoveryCache(Inventory):
    max_bytes = 100 * 1024 * 1024
    max_pages_per_site = None

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
        cutoff = retention_cutoff(days, datetime.now(timezone.utc)).isoformat(timespec='seconds')
        with self.connect() as c:
            before = c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            if all_cache:
                c.execute('DELETE FROM snapshots')
            elif days:
                c.execute('DELETE FROM snapshots WHERE checked_at<?', (cutoff,))
            c.execute('DELETE FROM structure_history')
            c.execute('DELETE FROM edge_history')
            # Older eviction removed pages but left their wide edge/index rows
            # behind. They are no longer evidence and can fill the cache forever.
            c.execute('DELETE FROM edges WHERE NOT EXISTS (SELECT 1 FROM pages p '
                      'WHERE p.site_key=edges.site_key AND p.url=edges.parent_url)')
            size = c.execute('SELECT coalesce(sum(length(body_gzip)),0) FROM snapshots').fetchone()[0]
            for row in c.execute('SELECT site_key,url,length(body_gzip) size FROM snapshots ORDER BY checked_at').fetchall():
                if not limit or size <= limit // 2:
                    break
                c.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', (row['site_key'], row['url']))
                size -= row['size']
            deleted = before - c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        # Check actual allocated space, not just payload bytes. Scratch records can
        # be rediscovered; retain pending tasks and published catalogue evidence.
        with self.connect() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if limit and self.path.stat().st_size > limit:
            active_keys = set()
            if has_app_context():
                from backend.database.models import BackgroundTask, School
                from backend.database.source_governance_models import SourceProposal
                from backend.services.source_inventory import site_key
                schools = {s.id: site_key(s.url) for s in School.query.all()}
                proposals = {p.id: p.school_id for p in SourceProposal.query.all()}
                for task in BackgroundTask.query.filter(BackgroundTask.state.in_(('pending', 'running', 'waiting')),
                        BackgroundTask.kind.in_(('discover', 'directory', 'navigation_review', 'source_review', 'source_grouping'))).all():
                    ident = task.payload.get('school_id') or proposals.get(task.payload.get('proposal_id'))
                    active_keys.add(schools.get(ident) or task.payload.get('site', {}).get('site_key') or task.payload.get('site_key'))
            with self.connect() as c:
                completed = c.execute("SELECT site_key,url FROM pages WHERE state NOT IN ('pending','running') ORDER BY checked_at").fetchall()
                # The visited set is part of an unfinished checklist. Removing
                # it would rediscover the same pages and silently reopen work.
                completed = [row for row in completed if row['site_key'] not in active_keys]
                for row in completed[:max(1, len(completed) // 2)]:
                    c.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', tuple(row))
                    c.execute('DELETE FROM structure WHERE site_key=? AND reference_url=?', tuple(row))
                    c.execute('DELETE FROM edges WHERE site_key=? AND parent_url=?', tuple(row))
                    c.execute('DELETE FROM pages WHERE site_key=? AND url=?', tuple(row))
            with self.connect() as c:
                c.execute('VACUUM')
            if self.path.stat().st_size > limit:
                raise RuntimeError('调查缓存已达到容量上限，已保存进度，请先清理后继续')
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
        prepare(inventory, key)
        catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
        from backend.database.models import School
        from backend.services import tasks
        from backend.services.source_onboarding import queue_columns
        school = next((s for s in School.query.filter_by(name=name).all()
                       if canonical_url(s.url) == canonical_url(root_url)), None)
        handle = tasks.current_execution()
        if monthly:
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
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, directory_refresh_started=True))
        # A slice prioritizes student entrances but must eventually visit other
        # official information too; student_priority is ordering, not exclusion.
        from backend.services.onboarding_progress import record_progress
        from backend.services.school_structure import sync_official_structure
        queued_columns = []
        def progress(processed, snapshot):
            states = snapshot['states']
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
        result = crawl_site(inventory, key, max_pages=3, workers=2,
                            focus='layered', retry_failed=monthly, progress=progress)
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
        entry = next((p for p in result['pages'] if p['kind'] == 'root' and p['state'] in ('failed', 'blocked')), None)
        entry_failure = ({'reason': entry.get('error') or '学校官网暂未读取成功',
                          'status_code': entry.get('status_code'), 'health': entry.get('health')}
                         if entry and not result['states'].get('fetched') else None)
        if handle:
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, first_slice_finished=True))
        from backend.services.discovery_changes import counts
        changes = counts(result)
        record_progress(phase='crawl' if pending or governance['remaining_candidates'] else 'complete',
                        entry_failure=entry_failure, changes=changes)
        return {'departments': catalog.candidates(key), 'pending_pages': pending,
                'changes': changes,
                'entry_failure': entry_failure,
                'continuation_required': bool(pending or governance['remaining_candidates']), **governance,
                'coverage_verified': False, 'message': '已找到的栏目正在接入，其他官网入口继续查找'}
