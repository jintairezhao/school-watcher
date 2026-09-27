"""Bounded, resumable source checks for subscribed schools."""
from datetime import datetime, timedelta, timezone

from filelock import Timeout
from flask import current_app

from backend.database.db import db
from backend.database.models import School
from backend.services.source_inventory import Inventory, DEFAULT_PATH


def refresh_subscribed_sources(max_pages=40, workers=2):
    """One school per turn, least-recently-checked first; no concurrent per-school owner."""
    from backend.scraper.discovery.inventory_crawler import crawl_site
    inventory = Inventory(current_app.config.get('SOURCE_INVENTORY_PATH', DEFAULT_PATH))
    schools = (School.query.filter(School.enabled.is_(True), School.subscriber_count > 0)
               .order_by(School.id).all())
    keys = {inventory.ensure_site(s.name, s.url): s for s in schools if s.url}
    if not keys:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
    # Persistent timestamps provide fairness across process restarts.
    with inventory.connect() as c:
        candidates = [dict(r) for r in c.execute("""
            SELECT s.site_key, coalesce(max(p.checked_at),'') AS checked
            FROM sites s JOIN pages p ON p.site_key=s.site_key
            GROUP BY s.site_key ORDER BY checked,s.site_key
        """) if r['site_key'] in keys]
    for candidate in candidates:
        key = candidate['site_key']
        report = inventory.report(key)
        from backend.services.student_sources import student_priority
        relevant = lambda p: student_priority(p['kind'], p['label'], p['path_json'], report['site']['name']) is not None
        if not any(p['state'] in ('pending', 'running') and relevant(p) for p in report['pages']):
            # Failed sources get a daily retry; old directories are revisited without deleting history.
            with inventory.connect() as c:
                targets = [p['url'] for p in report['pages'] if relevant(p)
                    and (not p['checked_at'] or p['checked_at'] < cutoff)
                    and (p['state'] in ('failed', 'blocked') or
                         (p['kind'] in ('root', 'directory') and p['state'] == 'fetched'))]
                c.executemany("UPDATE pages SET state='pending' WHERE site_key=? AND url=?",
                              [(key, url) for url in targets])
            report = inventory.report(key)
            if not any(p['state'] == 'pending' and relevant(p) for p in report['pages']):
                continue
        try:
            return crawl_site(inventory, key, max_pages=max_pages, workers=workers, focus='student')
        except Timeout:
            continue
        finally:
            db.session.remove()
    return None
