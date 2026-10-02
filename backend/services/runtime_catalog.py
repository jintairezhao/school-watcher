"""Small published catalogue. Discovery scratch databases are never read by web requests."""
from contextlib import contextmanager, closing
import gzip
import json
from pathlib import Path
import sqlite3

from flask import current_app, has_app_context
from backend.core.config import DATA_DIR
from backend.services.source_inventory import canonical_url, now


def pack(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)


def unpack(value):
    return json.loads(gzip.decompress(value))


class RuntimeCatalog:
    def __init__(self, path=None, *, versioned=True):
        self.path = Path(path or DATA_DIR / 'source_catalog.sqlite3')
        self.versioned = versioned

    def _published(self, key):
        if not self.versioned or not has_app_context():
            return None
        from backend.database.db import db
        from backend.database.models import CatalogPublication
        record = db.session.get(CatalogPublication, key)
        if record is None:
            return None
        target = (self.path.parent / record.path).resolve()
        if not target.is_relative_to(self.path.parent.resolve()) or not target.is_file():
            raise RuntimeError('已发布目录版本缺失，请从完整备份恢复')
        return RuntimeCatalog(target, versioned=False)

    @contextmanager
    def connect(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, timeout=30)
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS catalog_sites (
                    id INTEGER PRIMARY KEY, site_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, root_url TEXT NOT NULL, updated_at TEXT NOT NULL,
                    report_gzip BLOB NOT NULL, paths_gzip BLOB NOT NULL, candidates_gzip BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_pages (
                    id INTEGER PRIMARY KEY, site_id INTEGER NOT NULL REFERENCES catalog_sites(id),
                    url TEXT NOT NULL, page_gzip BLOB NOT NULL, nodes_gzip BLOB NOT NULL,
                    evidence_gzip BLOB, UNIQUE(site_id,url));
            ''')
        else:
            c = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            if write:
                c.commit()
        finally:
            c.close()

    def report(self, key):
        published = self._published(key)
        if published:
            return published.report(key)
        if not self.path.exists():
            return None
        with self.connect() as c:
            site = c.execute('SELECT id,report_gzip FROM catalog_sites WHERE site_key=?', (key,)).fetchone()
            if not site:
                return None
            report = unpack(site['report_gzip'])
            report['pages'] = [unpack(r[0]) for r in c.execute(
                'SELECT page_gzip FROM catalog_pages WHERE site_id=? ORDER BY id', (site['id'],))]
            return report

    def structure(self, key):
        published = self._published(key)
        if published:
            return published.structure(key)
        if not self.path.exists():
            return []
        with self.connect() as c:
            return [node for row in c.execute('SELECT p.nodes_gzip FROM catalog_pages p '
                'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=?', (key,)) for node in unpack(row[0])]

    def get_page(self, key, url):
        published = self._published(key)
        if published:
            return published.get_page(key, url)
        if not self.path.exists():
            return None
        with self.connect() as c:
            row = c.execute('SELECT p.page_gzip FROM catalog_pages p JOIN catalog_sites s ON s.id=p.site_id '
                            'WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
            return unpack(row[0]) if row else None

    def directory_entries(self, key, urls):
        """Read only requested roster pages, without inflating the entire investigation."""
        published = self._published(key)
        if published:
            return published.directory_entries(key, urls)
        if not self.path.exists():
            return {}
        result = {}
        with self.connect() as c:
            for url in dict.fromkeys(canonical_url(u) for u in urls if u):
                row = c.execute('SELECT p.page_gzip,p.nodes_gzip FROM catalog_pages p '
                    'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
                if not row:
                    continue
                page = unpack(row['page_gzip'])
                if page.get('kind') != 'directory' or page.get('state') != 'fetched':
                    continue
                entries = [n for n in unpack(row['nodes_gzip']) if n.get('kind') == 'unit'
                           and n.get('relation') == 'directory_entry'
                           and canonical_url(n.get('reference_url', '')) == url]
                if entries:
                    result[url] = entries
        return result

    def snapshot(self, key, url):
        published = self._published(key)
        if published:
            return published.snapshot(key, url)
        with self.connect() as c:
            row = c.execute('SELECT p.evidence_gzip FROM catalog_pages p JOIN catalog_sites s ON s.id=p.site_id '
                            'WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
            return gzip.decompress(row[0]).decode() if row and row[0] else ''

    def _site_value(self, key, column, default):
        published = self._published(key)
        if published:
            return published._site_value(key, column, default)
        if not self.path.exists():
            return default
        with self.connect() as c:
            row = c.execute(f'SELECT {column} FROM catalog_sites WHERE site_key=?', (key,)).fetchone()
            return unpack(row[0]) if row else default

    def paths(self, key):
        return self._site_value(key, 'paths_gzip', {})

    def candidates(self, key):
        return self._site_value(key, 'candidates_gzip', [])

    def all_sites(self):
        sites = {}
        if self.path.exists():
            with self.connect() as c:
                sites.update({r['site_key']: dict(r) for r in c.execute(
                    'SELECT site_key,name,root_url,updated_at FROM catalog_sites ORDER BY name')})
        if self.versioned and has_app_context():
            from backend.database.models import CatalogPublication
            for record in CatalogPublication.query.all():
                published = self._published(record.site_key)
                if published:
                    sites.update({s['site_key']: s for s in published.all_sites()})
        return sorted(sites.values(), key=lambda s: s['name'])

    def publish(self, inventory, key, *, merge=False):
        if self.versioned and has_app_context():
            return self._publish_version(inventory, key, merge=merge)
        return self._publish_mutable(inventory, key, merge=merge)

    def _publish_version(self, inventory, key, *, merge=False):
        """Write an immutable SQLite generation, then activate with the task fence.

        A crashed or superseded publisher can leave an orphan file; it can never
        change what readers see without a valid main-database transaction.
        """
        import re
        import uuid
        from datetime import datetime
        from filelock import FileLock
        from backend.database.db import db
        from backend.database.dialect import insert
        from backend.database.models import CatalogPublication
        from backend.services import tasks
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
            raise ValueError('Invalid catalogue site identity')
        generation = uuid.uuid4().hex
        relative = Path('catalog-generations') / f'{key}-{generation}.sqlite3'
        target = self.path.parent / relative
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + '.publish.lock', timeout=60):
            previous = self._published(key) or RuntimeCatalog(self.path, versioned=False)
            staged = RuntimeCatalog(target, versioned=False)
            # Copy only this site's published records, never the whole catalogue.
            with staged.connect(write=True) as writer:
                if merge and previous.path.exists():
                    with previous.connect() as reader:
                        site = reader.execute('SELECT * FROM catalog_sites WHERE site_key=?', (key,)).fetchone()
                        if site:
                            columns = list(site.keys())
                            writer.execute('INSERT INTO catalog_sites (' + ','.join(columns) + ') VALUES (' +
                                           ','.join('?' for _ in columns) + ')', tuple(site))
                            for page in reader.execute('SELECT * FROM catalog_pages WHERE site_id=?', (site['id'],)):
                                columns = list(page.keys())
                                writer.execute('INSERT INTO catalog_pages (' + ','.join(columns) + ') VALUES (' +
                                               ','.join('?' for _ in columns) + ')', tuple(page))
            staged._publish_mutable(inventory, key, merge=merge)
            with closing(sqlite3.connect(target)) as connection:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                connection.execute('PRAGMA journal_mode=DELETE')
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('新目录版本完整性检查失败')
            tasks.assert_owned()
            statement = insert(CatalogPublication).values(site_key=key, generation=generation,
                path=relative.as_posix(), activated_at=datetime.utcnow(), policy_version='1')
            db.session.execute(statement.on_conflict_do_update(index_elements=['site_key'], set_={
                field: getattr(statement.excluded, field) for field in ('generation', 'path', 'activated_at', 'policy_version')}))
            db.session.commit()
        return generation

    def _publish_mutable(self, inventory, key, *, merge=False):
        from collections import defaultdict
        from backend.services.source_relationships import SourceRelationships, ROSTER_RELATIONS, PATH_RELATIONS
        from backend.services.source_catalog import publication_candidates
        report = inventory.report(key)
        if not report:
            return
        records = inventory.structure(key)
        if merge:
            previous = self.report(key)
            if previous:
                # A failed revisit contains no replacement evidence. Preserve
                # the last valid page and its ownership graph while reporting
                # this attempt separately, rather than erasing working feeds.
                touched = {p['url'] for p in report['pages'] if p.get('checked_at') and p.get('state') == 'fetched'}
                pages_by_url = {p['url']: p for p in previous['pages']}
                for attempt in report['pages']:
                    if attempt['url'] in pages_by_url and attempt.get('checked_at') and attempt.get('state') in ('failed', 'blocked'):
                        pages_by_url[attempt['url']] = dict(pages_by_url[attempt['url']], latest_attempt={
                            k: attempt.get(k) for k in ('state', 'checked_at', 'error', 'status_code')})
                pages_by_url.update({p['url']: p for p in report['pages'] if p['url'] in touched or p['url'] not in pages_by_url})
                records = [n for n in self.structure(key) if n['reference_url'] not in touched] + [n for n in records if n['reference_url'] in touched]
                report['pages'] = list(pages_by_url.values())
                checks = {c['id']: c for c in previous.get('reference_checks', [])}
                checks.update({c['id']: c for c in report.get('reference_checks', [])})
                from backend.services.source_baselines import publication_hash
                for check in checks.values():
                    page = pages_by_url.get(check['reference_url'], {})
                    changed = page.get('content_hash') != check.get('reference_hash') or page.get('state') != 'fetched'
                    if check.get('category') == 'publication_columns':
                        changed |= check.get('feed_hash') != publication_hash(page.get('feed_json'))
                    if changed:
                        check.update(scope_passed=False, reference_current=False, status='reference_changed')
                report['reference_checks'] = list(checks.values())
                report['accepted'] = False
        relationships = SourceRelationships(report, records, directory_navigation=True)
        # Discovery spends its budget on student priorities; publishing retains
        # all observed columns so news/research/administration remain selectable.
        from backend.services.student_sources import display_priority
        candidates = sorted(publication_candidates(report, records, focus='all'),
                            key=lambda source: display_priority(source['name']))
        reviewed = {c['reference_url'] for c in report.get('reference_checks', [])}
        relations = ROSTER_RELATIONS | PATH_RELATIONS | {
            'major_directory_entry', 'programme_college', 'programme_joint_group',
            'publication_column', 'unit_channel', 'hidden_directory_entry',
            'directory_label_pending', 'unit_profile_entry', 'table_reference'}
        by_ref = defaultdict(list)
        for n in records:
            by_ref[n['reference_url']].append(n)
        selected = {}
        for ref, nodes in by_ref.items():
            if ref in reviewed:
                selected[ref] = nodes
                continue
            wanted = {n['node_key'] for n in nodes if n['relation'] in relations}
            # Preserve ancestors within the same observed document only.
            while True:
                parents = {n['parent_key'] for n in nodes if n['node_key'] in wanted and n['parent_key']}
                if parents <= wanted:
                    break
                wanted.update(parents)
            kept = [n for n in nodes if n['node_key'] in wanted]
            if kept:
                selected[ref] = kept
        pages = [p for p in report['pages'] if p['url'] in selected or p.get('feed_json') or
                 p['kind'] in ('root', 'directory', 'unit', 'major', 'gateway', 'channel') or p['url'] in reviewed]
        targets = {p['url'] for p in pages} | {n['url'] for ns in selected.values() for n in ns if n['url']}
        for cfg in candidates:
            targets.update((cfg['list_url'], cfg.get('column_url', '')))
        paths = {url: relationships.paths_for(url) for url in targets if url}
        paths = {url: value for url, value in paths.items() if value}
        site = report['site']
        summary = {k: v for k, v in report.items() if k != 'pages'}
        summary['catalogue_published_at'] = now()
        summary['stored_page_count'] = len(pages)
        summary['discovery_counters_are_historical'] = True
        with self.connect(write=True) as c:
            c.execute('INSERT INTO catalog_sites(site_key,name,root_url,updated_at,report_gzip,paths_gzip,candidates_gzip) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(site_key) DO UPDATE SET name=excluded.name,root_url=excluded.root_url,'
                'updated_at=excluded.updated_at,report_gzip=excluded.report_gzip,paths_gzip=excluded.paths_gzip,'
                'candidates_gzip=excluded.candidates_gzip',
                (key, site['name'], site['root_url'], now(), pack(summary), pack(paths), pack(candidates)))
            ident = c.execute('SELECT id FROM catalog_sites WHERE site_key=?', (key,)).fetchone()[0]
            if not merge:
                c.execute('DELETE FROM catalog_pages WHERE site_id=?', (ident,))
            for page in pages:
                evidence = inventory.snapshot(key, page['url']) if page['url'] in reviewed else ''
                blob = gzip.compress(evidence.encode(), mtime=0) if evidence else None
                c.execute('INSERT INTO catalog_pages(site_id,url,page_gzip,nodes_gzip,evidence_gzip) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(site_id,url) DO UPDATE SET page_gzip=excluded.page_gzip,nodes_gzip=excluded.nodes_gzip,'
                    'evidence_gzip=coalesce(excluded.evidence_gzip,catalog_pages.evidence_gzip)',
                    (ident, page['url'], pack(page), pack(selected.get(page['url'], [])), blob))


def runtime_catalog():
    # Explicit isolated legacy fixtures remain usable during the migration test suite.
    if current_app.config.get('TESTING') and current_app.config.get('SOURCE_INVENTORY_PATH'):
        from backend.services.source_inventory import Inventory
        return Inventory(current_app.config['SOURCE_INVENTORY_PATH'])
    return RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])


def prune_catalog_generations(days=7):
    """Retain every active publication and a rollback window for orphan versions."""
    import time
    from filelock import FileLock
    from backend.database.models import CatalogPublication
    root = Path(current_app.config['SOURCE_CATALOG_PATH']).resolve()
    directory = root.parent / 'catalog-generations'
    if not directory.exists():
        return {'files': 0, 'bytes': 0}
    cutoff = time.time() - max(7, days) * 86400
    removed = {'files': 0, 'bytes': 0}
    with FileLock(str(root) + '.publish.lock', timeout=60):
        protected = {(root.parent / row.path).resolve() for row in CatalogPublication.query.all()}
        for candidate in directory.glob('*.sqlite3'):
            path = candidate.resolve()
            if path.parent != directory.resolve() or path in protected or path.is_symlink():
                continue
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            path.unlink()
            removed['files'] += 1
            removed['bytes'] += stat.st_size
    return removed


def relationships_for(catalog, key):
    if isinstance(catalog, RuntimeCatalog):
        class Paths:
            def __init__(self):
                self.entries = catalog.paths(key)
            def paths_for(self, url):
                return self.entries.get(url, self.entries.get(canonical_url(url), []))
        return Paths()
    from backend.services.source_relationships import SourceRelationships
    return SourceRelationships(catalog.report(key), catalog.structure(key))
