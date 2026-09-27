"""Replay reviewed source snapshots into memberships, without rewriting articles.

Requires a source-audits JSON containing reviewed configs, content hashes and
verified_article_count for each listing. Dry-run by default; no network requests.
"""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
from sqlalchemy import text
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, Department, School
from backend.scraper.discovery.publication_lists import select_node
from backend.services.announcement_sources import record_source
from backend.services.source_inventory import Inventory, DEFAULT_PATH, site_key


def original_digests():
    result = {}
    for table in ('announcements', 'departments', 'schools', 'subscriptions',
                  'user_reads', 'user_announcement_states'):
        digest, count = hashlib.sha256(), 0
        for row in db.session.execute(text('SELECT * FROM ' + table + ' ORDER BY rowid')):
            digest.update(repr(tuple(row)).encode('utf-8'))
            count += 1
        result[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def reconcile(school_id, evidence_path, inventory, apply=False):
    school = db.session.get(School, school_id)
    if not school:
        raise ValueError('School not found')
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    report = inventory.report(site_key(school.url))
    if not report:
        raise ValueError('No saved official evidence for this school')
    pages = {p['url']: p for p in report['pages'] if p['state'] == 'fetched'}
    plans, summary = [], []
    for change in evidence['changes']:
        config = change['next']
        department = Department.query.filter_by(school_id=school_id,
            list_url=config['list_url'], list_selector=config['list_selector']).one()
        if any(getattr(department, field) != value for field, value in config.items()):
            raise ValueError('Source config changed since review: ' + department.name)
        page = pages.get(department.list_url)
        html = inventory.snapshot(report['site']['site_key'], department.list_url)
        if not page or not html or hashlib.sha256(html.encode()).hexdigest() != change['source_hash']:
            raise ValueError('Missing or changed official snapshot: ' + department.name)
        article_urls = set()
        for item in BeautifulSoup(html, 'lxml').select(department.list_selector):
            link = select_node(item, department.link_selector or 'a[href]')
            href = (link.get('href', '') if link else '').strip()
            if href and not href.lower().startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                article_urls.add(urljoin(page['final_url'] or page['url'], href))
        if len(article_urls) != change['verified_article_count']:
            raise ValueError('Reviewed article scope no longer matches: ' + department.name)
        matches = Announcement.query.filter(Announcement.url.in_(article_urls)).all()
        existing = {a.announcement_id for a in AnnouncementSource.query.filter_by(department_id=department.id)}
        by_url = defaultdict(list)
        for article in matches:
            by_url[article.url].append(article)
        found, duplicates = [], []
        for url, candidates in by_url.items():
            if len(candidates) == 1:
                found.extend(candidates)
                continue
            # Older crawls may have duplicate rows. Do not spread duplicates to new
            # columns or merge IDs that already carry personal reading history.
            already_linked = [a for a in candidates if a.id in existing or a.department_id == department.id]
            if len(already_linked) == 1:
                found.extend(already_linked)
            duplicates.append({'url': url, 'article_ids': [a.id for a in candidates],
                'retained_existing_id': already_linked[0].id if len(already_linked) == 1 else None})
        plans.extend((a, department, datetime.fromisoformat(page['checked_at'])) for a in found)
        summary.append({'department_id': department.id, 'name': department.name,
            'list_url': department.list_url, 'snapshot_sha256': change['source_hash'],
            'observed_at': page['checked_at'], 'matched_articles': len(found),
            'new_memberships': sum(a.id not in existing for a in found),
            'missing_article_urls': sorted(article_urls - {a.url for a in matches}),
            'legacy_duplicates_needing_review': duplicates})
    result = {'applied': apply, 'evidence': str(evidence_path.resolve()), 'sources': summary}
    if not apply:
        return result
    database = Path(db.engine.url.database)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = database.with_name(database.name + '.backup-memberships-' + stamp)
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    # Commit no changes to original tables, even if an unexpected code path mutates one.
    before = original_digests()
    try:
        for article, department, observed_at in plans:
            record_source(article, department, observed_at=observed_at)
        after = original_digests()
        if before != after:
            raise RuntimeError('Original rows changed; membership replay rolled back')
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    result.update(backup=str(backup), preserved=after,
                  applied_at=datetime.now(timezone.utc).isoformat())
    audit = ROOT / 'data' / 'source-audits' / ('memberships-' + stamp + '.json')
    audit.parent.mkdir(exist_ok=True)
    audit.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    result['audit'] = str(audit)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--school-id', type=int, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with create_app().app_context():
        print(json.dumps(reconcile(args.school_id, args.evidence,
                         Inventory(args.inventory), args.apply), ensure_ascii=True))
