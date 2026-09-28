"""Bounded scratch space for resumable adaptation; published evidence lives elsewhere."""
from datetime import datetime, timedelta, timezone
import json

from backend.services.source_inventory import Inventory, canonical_url


class DiscoveryCache(Inventory):
    max_bytes = 100 * 1024 * 1024
    max_pages_per_site = 1000

    def enqueue(self, key, url, label, kind, depth, path, authority):
        with self.connect() as c:
            count = c.execute('SELECT count(*) FROM pages WHERE site_key=?', (key,)).fetchone()[0]
            known = c.execute('SELECT 1 FROM pages WHERE site_key=? AND url=?', (key, url)).fetchone()
        if count >= self.max_pages_per_site and not known:
            raise RuntimeError('本校待检查入口达到容量上限，已保留进度，不能认定检查完成')
        return super().enqueue(key, url, label, kind, depth, path[-6:], authority)

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
        from backend.services.storage_policy import policy
        days = policy()['discovery_cache_days'] if has_app_context() else 7
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
        with self.connect() as c:
            before = c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            if all_cache:
                c.execute('DELETE FROM snapshots')
            elif days:
                c.execute('DELETE FROM snapshots WHERE checked_at<?', (cutoff,))
            c.execute('DELETE FROM structure_history')
            c.execute('DELETE FROM edge_history')
            size = c.execute('SELECT coalesce(sum(length(body_gzip)),0) FROM snapshots').fetchone()[0]
            for row in c.execute('SELECT site_key,url,length(body_gzip) size FROM snapshots ORDER BY checked_at').fetchall():
                if size <= self.max_bytes // 2:
                    break
                c.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', (row['site_key'], row['url']))
                size -= row['size']
            deleted = before - c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        # Check actual allocated space, not just payload bytes. Scratch records can
        # be rediscovered; retain pending tasks and published catalogue evidence.
        with self.connect() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if self.path.stat().st_size > self.max_bytes:
            with self.connect() as c:
                completed = c.execute("SELECT site_key,url FROM pages WHERE state NOT IN ('pending','running') ORDER BY checked_at").fetchall()
                for row in completed[:max(1, len(completed) // 2)]:
                    c.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', tuple(row))
                    c.execute('DELETE FROM structure WHERE site_key=? AND reference_url=?', tuple(row))
                    c.execute('DELETE FROM pages WHERE site_key=? AND url=?', tuple(row))
            with self.connect() as c:
                c.execute('VACUUM')
            if self.path.stat().st_size > self.max_bytes:
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
        catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
        report = catalog.report(key)
        if monthly and report:
            # Refresh the known entrances. Generic website navigation is not a crawl seed.
            for page in report['pages']:
                if page['kind'] in ('root', 'directory') and page['depth'] <= 2:
                    inventory.enqueue(key, page['url'], page['label'], page['kind'], page['depth'],
                                      json.loads(page['path_json']), page['authority'])
                    with inventory.connect() as c:
                        c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=? AND state!='running'", (key, page['url']))
        from backend.services import tasks
        handle = tasks.current_execution()
        if handle:
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, directory_refresh_started=True))
        # A slice prioritizes student entrances but must eventually visit other
        # official information too; student_priority is ordering, not exclusion.
        from backend.services.onboarding_progress import record_progress
        def progress(processed, snapshot):
            states = snapshot['states']
            record_progress(phase='crawl', checked_pages=sum(v for k, v in states.items() if k not in ('pending', 'running')),
                pending_pages=states.get('pending', 0) + states.get('running', 0),
                failed_pages=states.get('failed', 0) + states.get('blocked', 0),
                current_label=snapshot.get('current_label', ''))
            if processed:
                catalog.publish(inventory, key, merge=True)
        # Make the first results available without waiting for a twenty-page crawl.
        first_slice = bool(handle) and not handle.get('checkpoint', {}).get('first_slice_finished')
        result = crawl_site(inventory, key, max_pages=3 if first_slice else 20, workers=2,
                            focus='all', retry_failed=monthly, progress=progress)
        from backend.database.models import School
        from backend.services.source_governance import process_discovered_candidates, record_onboarding_slice
        from backend.services.source_relationships import ROSTER_RELATIONS
        school = next((s for s in School.query.filter_by(name=name).all()
                       if canonical_url(s.url) == canonical_url(root_url)), None)
        governance = {'proposal_ids': [], 'activated_ids': [], 'remaining_candidates': 0}
        if school:
            record_progress(phase='verify', current_label='')
            governance = process_discovered_candidates(school.id, inventory, key)
            result['official_units'] = [n for n in inventory.structure(key) if n['kind'] == 'unit' and n['relation'] in ROSTER_RELATIONS]
            record_onboarding_slice(school.id, result)
        # Publication carries observed structure and explicit candidate states;
        # installation in the reading database is exclusively the gated path.
        catalog.publish(inventory, key, merge=True)
        inventory.trim()
        pending = result['states'].get('pending', 0) + result['states'].get('running', 0)
        if handle:
            tasks.checkpoint(dict(handle.get('checkpoint') or {}, first_slice_finished=True))
        record_progress(phase='crawl' if pending or governance['remaining_candidates'] else 'complete')
        return {'departments': catalog.candidates(key), 'pending_pages': pending,
                'continuation_required': bool(pending or governance['remaining_candidates']), **governance,
                'coverage_verified': False, 'message': '本轮来源检查已保存；剩余入口将继续检查，待核实栏目不会直接生效'}
