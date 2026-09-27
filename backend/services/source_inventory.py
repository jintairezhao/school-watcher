"""Persistent evidence and crawl frontier, separate from the reading database."""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'data' / 'source_inventory.sqlite3'
KINDS = {'root': 0, 'directory': 1, 'unit': 2, 'major': 2, 'channel': 3, 'navigation': 4}
MATCH_THRESHOLD = 90


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def has_fragment_route(url):
    return urlsplit(url).fragment.startswith(('/', '!/'))


def resolve_page_link(base_url, href):
    href = (href or '').strip()
    if (not href or href.lower().startswith(('javascript:', 'mailto:', 'tel:'))
            or href in ('{栏目URL}', '{栏目url}')
            or (href.startswith('#') and not has_fragment_route(href))):
        return ''
    return canonical_url(urljoin(base_url, href))


def canonical_url(url):
    p = urlsplit(url.strip())
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        return ''
    host = p.hostname.lower().encode('idna').decode()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    if port and port != (443 if p.scheme.lower() == 'https' else 80):
        host += ':' + str(port)
    # Preserve case-sensitive paths and functional query parameters.
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    # Client-side routes contain article/unit identities. Keep their case,
    # encoding and parameter order; ordinary document anchors still collapse.
    fragment = p.fragment if has_fragment_route(url) else ''
    return urlunsplit((p.scheme.lower(), host, p.path or '/', urlencode(sorted(query)), fragment))


def site_key(url):
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:20]


class Inventory:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sites (
                    site_key TEXT PRIMARY KEY, name TEXT NOT NULL, root_url TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL,
                    review_state TEXT NOT NULL DEFAULT 'unverified'
                );
                CREATE TABLE IF NOT EXISTS pages (
                    site_key TEXT NOT NULL, url TEXT NOT NULL, label TEXT NOT NULL,
                    kind TEXT NOT NULL, priority INTEGER NOT NULL, depth INTEGER NOT NULL,
                    path_json TEXT NOT NULL DEFAULT '[]', authority TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER, final_url TEXT, title TEXT, content_hash TEXT,
                    checked_at TEXT, error TEXT, health TEXT NOT NULL DEFAULT 'unverified',
                    latest_publication TEXT, feed_json TEXT, notes_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(site_key,url)
                );
                CREATE INDEX IF NOT EXISTS frontier ON pages(site_key,state,priority,depth);
                CREATE TABLE IF NOT EXISTS edges (
                    site_key TEXT NOT NULL, parent_url TEXT NOT NULL, target_url TEXT NOT NULL,
                    label TEXT NOT NULL, kind TEXT NOT NULL, path_json TEXT NOT NULL,
                    locator TEXT NOT NULL, decision TEXT NOT NULL, content_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(site_key,parent_url,target_url,label,path_json,locator)
                );
                CREATE INDEX IF NOT EXISTS source_edges ON edges(site_key,target_url);
                CREATE TABLE IF NOT EXISTS edge_history (
                    id INTEGER PRIMARY KEY, site_key TEXT NOT NULL, parent_url TEXT NOT NULL,
                    archived_at TEXT NOT NULL, records_gzip BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS structure (
                    site_key TEXT NOT NULL, node_key TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, url TEXT NOT NULL, parent_key TEXT NOT NULL,
                    reference_url TEXT NOT NULL, locator TEXT NOT NULL, content_hash TEXT NOT NULL,
                    relation TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(site_key,node_key,parent_key,reference_url,locator)
                );
                CREATE INDEX IF NOT EXISTS structure_parents ON structure(site_key,parent_key);
                CREATE INDEX IF NOT EXISTS structure_references ON structure(site_key,reference_url);
                CREATE TABLE IF NOT EXISTS structure_history (
                    id INTEGER PRIMARY KEY, site_key TEXT NOT NULL, reference_url TEXT NOT NULL,
                    archived_at TEXT NOT NULL, records_gzip BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    site_key TEXT NOT NULL, url TEXT NOT NULL, content_hash TEXT NOT NULL,
                    body_gzip BLOB NOT NULL, checked_at TEXT NOT NULL,
                    PRIMARY KEY(site_key,url)
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    site_key TEXT NOT NULL, category TEXT NOT NULL, reference_url TEXT NOT NULL,
                    label TEXT NOT NULL, expected_url TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT 'pending',
                    evidence TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
                    PRIMARY KEY(site_key,category,reference_url,label,expected_url)
                );
                CREATE TABLE IF NOT EXISTS reference_checks (
                    site_key TEXT NOT NULL, baseline_id TEXT NOT NULL, baseline_json TEXT NOT NULL,
                    result_json TEXT NOT NULL, checked_at TEXT NOT NULL,
                    PRIMARY KEY(site_key,baseline_id)
                );
            """)
            # A short write transaction serializes upgrades with other launchers.
            c.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in c.execute('PRAGMA table_info(pages)')}
            if 'parser_revision' not in columns:
                c.execute('ALTER TABLE pages ADD COLUMN parser_revision TEXT')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def ensure_site(self, name, root_url):
        root_url = canonical_url(root_url)
        key = site_key(root_url)
        with self.connect() as c:
            c.execute("INSERT INTO sites(site_key,name,root_url,updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(site_key) DO UPDATE SET name=excluded.name",
                      (key, name, root_url, now()))
        self.enqueue(key, root_url, name, 'root', 0, [], 'school_root')
        return key

    def enqueue(self, key, url, label, kind, depth, path, authority):
        url = canonical_url(url)
        if not url:
            return
        with self.connect() as c:
            c.execute("""INSERT INTO pages(site_key,url,label,kind,priority,depth,path_json,authority)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(site_key,url) DO UPDATE SET
                kind=CASE WHEN pages.state='reference_only' OR excluded.priority<pages.priority THEN excluded.kind ELSE pages.kind END,
                label=CASE WHEN pages.state='reference_only' OR excluded.priority<pages.priority THEN excluded.label ELSE pages.label END,
                path_json=CASE WHEN pages.state='reference_only' THEN excluded.path_json ELSE pages.path_json END,
                state=CASE WHEN pages.state='reference_only' THEN 'pending' ELSE pages.state END,
                priority=min(pages.priority,excluded.priority),depth=min(pages.depth,excluded.depth)""",
                (key, url, label, kind, KINDS.get(kind, 5), depth, json.dumps(path, ensure_ascii=False), authority))

    def record_edges(self, key, parent_url, links, content_hash):
        with self.connect() as c:
            previous = [dict(r) for r in c.execute('SELECT * FROM edges WHERE site_key=? AND parent_url=?',
                                                  (key, parent_url))]
            old_signature = {(r['target_url'], r['label'], r['kind'], r['path_json'], r['locator'],
                              r['decision'], r['content_hash']) for r in previous}
            new_signature = {(l.get('url', ''), l['label'], l['kind'],
                              json.dumps(l.get('path', []), ensure_ascii=False), l.get('locator', ''),
                              l['decision'], content_hash) for l in links}
            if previous and old_signature != new_signature:
                c.execute('INSERT INTO edge_history(site_key,parent_url,archived_at,records_gzip) VALUES(?,?,?,?)',
                          (key, parent_url, now(), gzip.compress(json.dumps(previous, ensure_ascii=False).encode())))
            # A parser correction can remove links even when the HTML hash stays
            # unchanged. Only this complete parse is current; keep prior evidence separately.
            c.execute('DELETE FROM edges WHERE site_key=? AND parent_url=?', (key, parent_url))
            for link in links:
                c.execute("""INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(site_key,parent_url,target_url,label,path_json,locator)
                    DO UPDATE SET decision=excluded.decision,content_hash=excluded.content_hash,
                    observed_at=excluded.observed_at,kind=excluded.kind""",
                    (key, parent_url, link.get('url', ''), link['label'], link['kind'],
                     json.dumps(link.get('path', []), ensure_ascii=False), link.get('locator', ''),
                     link['decision'], content_hash, now()))

    def record_structure(self, key, reference_url, nodes, content_hash):
        # The database keeps one final interpretation per placement. Generic
        # navigation parsing can emit the same heading first as an entry and
        # later as an ancestor. Compare that persisted result, not intermediate
        # duplicates, or every identical reparse invalidates an existing review.
        nodes = list({(n['key'], n.get('parent', ''), n['locator']): n for n in nodes}.values())
        with self.connect() as c:
            previous = [dict(r) for r in c.execute('SELECT * FROM structure WHERE site_key=? AND reference_url=?',
                                                  (key, reference_url))]
            old_signature = {(r['node_key'], r['name'], r['kind'], r['url'], r['parent_key'], r['locator'], r['relation']) for r in previous}
            new_signature = {(n['key'], n['name'], n['kind'], n.get('url', ''), n.get('parent', ''), n['locator'], n['relation']) for n in nodes}
            if previous and old_signature != new_signature:
                c.execute('INSERT INTO structure_history(site_key,reference_url,archived_at,records_gzip) VALUES(?,?,?,?)',
                          (key, reference_url, now(), gzip.compress(json.dumps(previous, ensure_ascii=False).encode())))
                for row in c.execute('SELECT baseline_id,result_json FROM reference_checks WHERE site_key=?', (key,)).fetchall():
                    check = json.loads(row['result_json'])
                    if check['reference_url'] == reference_url:
                        check.update(scope_passed=False, reference_current=False, status='structure_changed')
                        c.execute('UPDATE reference_checks SET result_json=? WHERE site_key=? AND baseline_id=?',
                                  (json.dumps(check, ensure_ascii=False), key, row['baseline_id']))
            # Current placements follow current evidence; prior placements remain in history.
            c.execute('DELETE FROM structure WHERE site_key=? AND reference_url=?', (key, reference_url))
            for n in nodes:
                c.execute("""INSERT INTO structure VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(site_key,node_key,parent_key,reference_url,locator)
                    DO UPDATE SET content_hash=excluded.content_hash,observed_at=excluded.observed_at,
                    relation=excluded.relation""",
                    (key, n['key'], n['name'], n['kind'], n.get('url', ''), n.get('parent', ''),
                     reference_url, n['locator'], content_hash, n['relation'], now()))

    def structure(self, key):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM structure WHERE site_key=? ORDER BY rowid', (key,))]

    def claim(self, key, focus='all'):
        if focus not in ('all', 'student'):
            raise ValueError('Unknown discovery focus')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            condition, order = '', 'depth+priority,depth,priority,url'
            from .student_sources import student_priority
            school = c.execute('SELECT name FROM sites WHERE site_key=?', (key,)).fetchone()
            school_name = school['name'] if school else ''
            c.create_function('student_priority', 3, lambda kind, label, path:
                              student_priority(kind, label, path, school_name))
            # All-information discovery retains every frontier item while
            # prioritising student routes. A bounded slice does not drop the rest.
            if focus == 'all':
                order = 'coalesce(student_priority(kind,label,path_json),20),depth+priority,depth,url'
            if focus == 'student':
                condition = 'AND student_priority(kind,label,path_json) IS NOT NULL '
                order = 'student_priority(kind,label,path_json),depth,priority,url'
            while True:
                row = c.execute("SELECT * FROM pages WHERE site_key=? AND state='pending' "
                                + condition + 'ORDER BY ' + order + ' LIMIT 1', (key,)).fetchone()
                if row is None:
                    return None
                decisions = {r[0] for r in c.execute(
                    "SELECT DISTINCT e.decision FROM edges e JOIN pages parent ON "
                    "parent.site_key=e.site_key AND parent.url=e.parent_url "
                    "WHERE e.site_key=? AND e.target_url=? AND parent.state='fetched' "
                    "AND parent.content_hash=e.content_hash", (key, row['url']))}
                if row['kind'] != 'root' and decisions == {'article_reference'}:
                    c.execute("UPDATE pages SET state='reference_only' WHERE site_key=? AND url=?", (key, row['url']))
                    continue
                break
            c.execute("UPDATE pages SET state='running',attempts=attempts+1 WHERE site_key=? AND url=?",
                      (key, row['url']))
            c.execute("UPDATE sites SET state='running',updated_at=? WHERE site_key=?", (now(), key))
            return dict(row)

    def reconcile_article_references(self, key):
        """Preserve misclassified article evidence; never erase or treat it as a unit.

        The caller owns the school's crawl lock. Every current incoming edge must
        agree, and a later genuine roster entrance can enqueue the URL again.
        """
        with self.connect() as c:
            result = c.execute("""UPDATE pages SET state='reference_only'
                WHERE site_key=? AND kind<>'root' AND state IN ('pending','fetched','failed','blocked')
                AND url IN (SELECT e.target_url FROM edges e JOIN pages parent ON
                    parent.site_key=e.site_key AND parent.url=e.parent_url
                    WHERE e.site_key=? AND parent.state='fetched' AND parent.content_hash=e.content_hash
                    GROUP BY e.target_url HAVING min(e.decision='article_reference')=1)""", (key, key))
            return result.rowcount

    def recover(self, key):
        # Caller must own this site's process lock; a status record alone is not proof a worker died.
        with self.connect() as c:
            c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND state='running'", (key,))

    def retry(self, key):
        with self.connect() as c:
            c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND state IN ('failed','blocked')", (key,))

    def finish(self, key, url, html='', fetched_at=None, **values):
        allowed = {'state', 'status_code', 'final_url', 'title', 'error', 'health',
                   'latest_publication', 'feed_json', 'notes_json', 'parser_revision'}
        if set(values) - allowed:
            raise ValueError('Unknown page fields')
        values['checked_at'] = fetched_at or now()
        if html:
            values['content_hash'] = hashlib.sha256(html.encode()).hexdigest()
        with self.connect() as c:
            fields = ','.join(k + '=?' for k in values)
            c.execute(f'UPDATE pages SET {fields} WHERE site_key=? AND url=?',
                      [*values.values(), key, url])
            if html:
                c.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",
                          (key, url, values['content_hash'], gzip.compress(html.encode()), values['checked_at']))
            c.execute('UPDATE sites SET updated_at=? WHERE site_key=?', (now(), key))

    def settle(self, key):
        with self.connect() as c:
            pending = c.execute("SELECT count(*) FROM pages WHERE site_key=? AND state IN ('pending','running')",
                                (key,)).fetchone()[0]
            # Even an exhausted frontier still requires independent completeness review.
            c.execute('UPDATE sites SET state=?,updated_at=? WHERE site_key=?',
                      ('pending' if pending else 'needs_review', now(), key))

    def snapshot(self, key, url):
        with self.connect() as c:
            row = c.execute('SELECT body_gzip FROM snapshots WHERE site_key=? AND url=?', (key, url)).fetchone()
            return gzip.decompress(row[0]).decode() if row else None

    def get_page(self, key, url):
        with self.connect() as c:
            row = c.execute('SELECT * FROM pages WHERE site_key=? AND url=?', (key, url)).fetchone()
            return dict(row) if row else None

    def report(self, key):
        with self.connect() as c:
            site = c.execute('SELECT * FROM sites WHERE site_key=?', (key,)).fetchone()
            if site is None:
                return None
            pages = [dict(r) for r in c.execute('SELECT * FROM pages WHERE site_key=? ORDER BY priority,label', (key,))]
            rows = c.execute('SELECT category,verdict,count(*) n FROM baselines WHERE site_key=? '
                             'GROUP BY category,verdict', (key,)).fetchall()
            metrics = {}
            for category in ('units', 'channels', 'accuracy'):
                total = sum(r['n'] for r in rows if r['category'] == category)
                matched = sum(r['n'] for r in rows if r['category'] == category and r['verdict'] == 'matched')
                metrics[category] = {'total': total, 'matched': matched,
                                     'percent': round(100 * matched / total, 2) if total else None}
            states = {}
            for p in pages:
                states[p['state']] = states.get(p['state'], 0) + 1
            # Legacy numerator tables are diagnostic only. Whole-school readiness
            # now belongs to SchoolOnboarding and its independent roster/scope
            # review, not a second 90%-confidence approval path.
            accepted = False
            reference_checks = [json.loads(r[0]) for r in c.execute(
                'SELECT result_json FROM reference_checks WHERE site_key=? ORDER BY baseline_id', (key,))]
            current_hashes = {p['url']: p['content_hash'] for p in pages}
            by_url = {p['url']: p for p in pages}
            for check in reference_checks:
                if current_hashes.get(check['reference_url']) != check['reference_hash']:
                    check.update(reference_current=False, scope_passed=False, status='reference_changed')
                elif by_url.get(check['reference_url'], {}).get('state') != 'fetched':
                    check.update(reference_current=False, scope_passed=False, status='reference_unavailable')
                elif check.get('category') == 'publication_columns':
                    from .source_baselines import publication_hash
                    page = by_url[check['reference_url']]
                    if check.get('feed_hash') != publication_hash(page.get('feed_json')):
                        check.update(reference_current=False, scope_passed=False, status='publication_changed')
            return {'site': dict(site), 'pages': pages, 'states': states, 'metrics': metrics,
                    'reference_checks': reference_checks, 'match_threshold': MATCH_THRESHOLD,
                    'accepted': accepted, 'acceptance_source': 'school_onboarding',
                    'edge_count': c.execute('SELECT count(*) FROM edges WHERE site_key=?', (key,)).fetchone()[0]}

    def progress_snapshot(self, key):
        """Small live counters; progress is not a completeness or accuracy verdict."""
        with self.connect() as c:
            site = c.execute('SELECT * FROM sites WHERE site_key=?', (key,)).fetchone()
            if site is None:
                return None
            states = dict(c.execute('SELECT state,count(*) FROM pages WHERE site_key=? GROUP BY state', (key,)))
            return {'site': dict(site), 'states': states}

    def all_sites(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM sites ORDER BY name')]
