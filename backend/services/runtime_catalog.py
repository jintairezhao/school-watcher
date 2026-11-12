"""Small published catalogue. Discovery scratch databases are never read by web requests."""
from contextlib import contextmanager, closing, nullcontext
from functools import wraps
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import weakref
import zlib

from flask import current_app, has_app_context
from backend.core.config import DATA_DIR
from backend.services.source_inventory import canonical_url, now

HISTORY_VERSIONS = 2
HISTORY_BYTES = 256 * 1024 * 1024
RETIREMENT_GRACE_SECONDS = 10 * 60
_GENERATION_NAME = re.compile(r'(?P<key>[A-Za-z0-9_-]{1,128})-(?P<generation>[0-9a-f]{32})\.sqlite3\Z')
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def _link(path):
    return path.is_symlink() or path.is_junction()


@contextmanager
def catalog_publication_lock(path=None, *, timeout=60):
    """One cached, reentrant lock for readers, publishers, backups and pruning."""
    from filelock import FileLock
    root = Path(path or current_app.config['SOURCE_CATALOG_PATH']).resolve()
    name = str(root) + '.publish.lock'
    with _LOCKS_GUARD:
        lock = _LOCKS.get(name)
        if lock is None:
            lock = FileLock(name, timeout=60)
            _LOCKS[name] = lock
    with lock.acquire(timeout=timeout):
        yield root


def _catalog_read(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        root = self._read_lock_path()
        with catalog_publication_lock(root) if root else nullcontext():
            return method(self, *args, **kwargs)
    return guarded


def _generation_directory(root, *, create=False):
    directory = root.parent / 'catalog-generations'
    if _link(directory):
        raise RuntimeError('目录版本文件夹不能是符号链接或联接')
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if directory.exists() and (not directory.is_dir() or directory.resolve().parent != root.parent):
        raise RuntimeError('目录版本文件夹位置无效')
    return directory


def _generation_path(root, relative, *, key=None, generation=None):
    value = Path(relative)
    if value.is_absolute() or len(value.parts) != 2 or value.parts[0] != 'catalog-generations':
        raise RuntimeError('已发布目录版本路径无效')
    match = _GENERATION_NAME.fullmatch(value.name)
    if not match or (key is not None and match['key'] != key) or (generation is not None and match['generation'] != generation):
        raise RuntimeError('已发布目录版本标识无效')
    directory = _generation_directory(root)
    target = directory / value.name
    if _link(target) or not target.is_file() or target.resolve().parent != directory:
        raise RuntimeError('已发布目录版本缺失，请从完整备份恢复')
    return target


def _active_publications(root, *, key=None):
    """Fresh committed pointers, independent of an ORM identity map/snapshot."""
    from sqlalchemy import select
    from backend.database.db import db
    from backend.database.models import CatalogPublication
    statement = select(CatalogPublication.__table__)
    if key is not None:
        statement = statement.where(CatalogPublication.site_key == key)
    with db.engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    for row in rows:
        _generation_path(root, row['path'], key=row['site_key'], generation=row['generation'])
    return rows


def _ledger_path(root):
    path = root.with_name(root.name + '.retention.json')
    if _link(path) or (path.exists() and not path.is_file()):
        raise RuntimeError('目录历史保留记录位置无效')
    return path


def _read_retirements(root):
    path = _ledger_path(root)
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('retired'), dict):
        raise RuntimeError('目录历史保留记录无效')
    entries = value['retired']
    for name, entry in entries.items():
        if (not _GENERATION_NAME.fullmatch(name) or not isinstance(entry, dict)
                or type(entry.get('at')) not in (int, float) or not 0 <= entry['at'] < float('inf')
                or type(entry.get('legacy')) is not bool):
            raise RuntimeError('目录历史保留记录无效')
    return entries


def _write_retirements(root, entries):
    path = _ledger_path(root)
    descriptor, temporary = tempfile.mkstemp(prefix='.catalog-retention-', suffix='.tmp', dir=root.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as writer:
            json.dump({'version': 1, 'retired': entries}, writer, ensure_ascii=False, separators=(',', ':'))
            writer.flush()
            os.fsync(writer.fileno())
        _ledger_path(root)  # Refuse a replaced ledger link before publication.
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _semantic_report(report):
    """Ignore publication bookkeeping, retaining timestamps of actual evidence."""
    result = dict(report)
    result.pop('catalogue_published_at', None)
    result.pop('updated_at', None)
    if isinstance(result.get('site'), dict):
        result['site'] = dict(result['site'])
        result['site'].pop('updated_at', None)
    return result


def _catalogue_digest(catalog, key):
    digest = hashlib.sha256()
    def add(value):
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).encode('utf8'))
        digest.update(b'\n')
    if not catalog.path.exists():
        return None
    with catalog.connect() as reader:
        site = reader.execute('SELECT * FROM catalog_sites WHERE site_key=?', (key,)).fetchone()
        if not site:
            return None
        add([site['site_key'], site['name'], site['root_url'],
             _semantic_report(unpack(site['report_gzip'])), unpack(site['paths_gzip']), unpack(site['candidates_gzip'])])
        for row in reader.execute('SELECT url,page_gzip,nodes_gzip,evidence_gzip FROM catalog_pages '
                                  'WHERE site_id=? ORDER BY url', (site['id'],)):
            add([row['url'], unpack(row['page_gzip']), unpack(row['nodes_gzip'])])
            digest.update(gzip.decompress(row['evidence_gzip']) if row['evidence_gzip'] else b'')
            digest.update(b'\n')
    return digest.hexdigest()


def _remove_unpublished(root, target):
    """Only our never-activated, fully closed staging file and its sidecars."""
    try:
        directory = _generation_directory(root)
        if target.parent != directory or not _GENERATION_NAME.fullmatch(target.name) or _link(target):
            return
        for suffix in ('-wal', '-shm', '-journal', ''):
            path = target.with_name(target.name + suffix)
            if path.is_file() and not _link(path) and path.resolve().parent == directory:
                path.unlink()
    except Exception:
        # A leftover stage is managed as an orphan by the normal grace policy.
        current_app.logger.warning('Unpublished catalogue stage cleanup deferred')


def _prune_generations(root, *, dry_run=False, offline_legacy=False):
    from filelock import Timeout
    removed = {'files': 0, 'bytes': 0}
    try:
        with catalog_publication_lock(root, timeout=0):
            directory = _generation_directory(root)
            if not directory.exists():
                return removed
            active = _active_publications(root)
            protected = {_generation_path(root, row['path'], key=row['site_key'],
                generation=row['generation']) for row in active}
            original = _read_retirements(root)
            timestamp = time.time()
            history, skipped_sidecars = [], 0
            for candidate in directory.iterdir():
                match = _GENERATION_NAME.fullmatch(candidate.name)
                if not match or _link(candidate) or not candidate.is_file() or candidate.resolve().parent != directory:
                    continue
                if candidate in protected:
                    continue
                # Sidecars are evidence of an unfinished/unknown external writer.
                # Never remove just its main file or guess that the writer stopped.
                if any(candidate.with_name(candidate.name + suffix).exists() or _link(
                        candidate.with_name(candidate.name + suffix)) for suffix in ('-wal', '-shm', '-journal')):
                    skipped_sidecars += 1
                    continue
                stat = candidate.stat()
                retirement = original.get(candidate.name, {'at': timestamp, 'legacy': True})
                grace = timestamp - retirement['at'] < RETIREMENT_GRACE_SECONDS
                if offline_legacy and retirement['legacy']:
                    grace = False  # A caller proved all pre-upgrade processes stopped.
                history.append({'path': candidate, 'key': match['key'], 'size': stat.st_size,
                    'mtime': stat.st_mtime, 'retirement': retirement, 'grace': grace})
            # A single bounded ledger; stale records disappear on the next pass.
            retirements = {row['path'].name: row['retirement'] for row in history}
            ordered = sorted(history, key=lambda row: (
                not row['retirement']['legacy'],
                row['mtime'] if row['retirement']['legacy'] else row['retirement']['at'],
                row['mtime'], row['path'].name), reverse=True)
            grace_names = {row['path'].name for row in history if row['grace']}
            grace_bytes = sum(row['size'] for row in history if row['grace'])
            retained, per_site = set(grace_names), {}
            remaining = max(0, HISTORY_BYTES - grace_bytes)
            for row in ordered:
                count = per_site.get(row['key'], 0)
                per_site[row['key']] = count + 1
                if count >= HISTORY_VERSIONS or row['grace']:
                    continue
                if row['size'] <= remaining:
                    retained.add(row['path'].name)
                    remaining -= row['size']
            planned = [row for row in reversed(ordered) if row['path'].name not in retained]
            kept_bytes = sum(row['size'] for row in history if row['path'].name in retained)
            summary = {'active_files': len(protected), 'active_bytes': sum(path.stat().st_size for path in protected),
                'history_files': len(retained), 'history_bytes': kept_bytes,
                'grace_files': len(grace_names), 'grace_bytes': grace_bytes,
                'over_budget_bytes': max(0, kept_bytes - HISTORY_BYTES),
                'skipped_sidecars': skipped_sidecars}
            if dry_run:
                return {'files': len(planned), 'bytes': sum(row['size'] for row in planned), 'dry_run': True, **summary}
            # Metadata must be durable before deleting anything. Failure leaves
            # every snapshot intact; dry-run never creates/updates this ledger.
            if planned or retirements != original:
                _write_retirements(root, retirements)
            for row in planned:
                path = row['path']
                try:
                    checked = _generation_path(root, Path('catalog-generations') / path.name)
                    if checked in protected or any(checked.with_name(checked.name + suffix).exists()
                            for suffix in ('-wal', '-shm', '-journal')):
                        continue
                    size = checked.stat().st_size
                    checked.unlink()
                    removed['files'] += 1
                    removed['bytes'] += size
                except (OSError, RuntimeError):
                    # Windows may still hold a pre-upgrade file open. Retry on
                    # another pass, without failing the successful publication.
                    continue
            # A file held open by a legacy Windows reader may remain after a
            # planned deletion. Report the actual residual history, not the plan.
            residual = [row for row in history if row['path'].is_file() and not _link(row['path'])]
            summary['history_files'] = len(residual)
            summary['history_bytes'] = sum(row['path'].stat().st_size for row in residual)
            summary['over_budget_bytes'] = max(0, summary['history_bytes'] - HISTORY_BYTES)
            summary['skipped_files'] = len(planned) - removed['files']
            return {**removed, **summary}
    except Timeout:
        return {**removed, 'skipped': 'catalogue_busy'}
    except Exception:
        current_app.logger.warning('Catalogue history cleanup skipped: state could not be verified')
        return {**removed, 'skipped': 'unverified_catalogue_state'}


def pack(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)


def unpack(value):
    return json.loads(gzip.decompress(value))


@contextmanager
def retained_evidence_references(path=None):
    """Pin cache-cleanup roots until deletion finishes, including retained history.

    This reads page metadata only. A catalogue row is an existing reference to
    bytes, not new proof of a relationship or a replacement for those bytes.
    """
    configured = path or current_app.config.get('SOURCE_CATALOG_PATH')
    if not configured:
        yield set()
        return
    root = Path(configured).resolve()
    with catalog_publication_lock(root):
        # Validate every active pointer first. A missing active generation must
        # stop cleanup before it can mistake its unobservable references for orphans.
        active = _active_publications(root)
        paths = {root} if root.is_file() else set()
        paths.update(_generation_path(root, row['path'], key=row['site_key'],
            generation=row['generation']) for row in active)
        directory = _generation_directory(root)
        if directory.exists():
            for candidate in directory.iterdir():
                match = _GENERATION_NAME.fullmatch(candidate.name)
                if match:
                    # Retired versions may still support history/recovery until
                    # the existing generation-retention policy removes the file.
                    paths.add(_generation_path(root, 'catalog-generations/' + candidate.name,
                        key=match['key'], generation=match['generation']))
        protected = set()

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('hash', 'reference_hash', 'sha256', 'content_hash') and isinstance(item, str) and re.fullmatch('[0-9a-f]{64}', item):
                        protected.add(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for retained in sorted(paths):
            with closing(sqlite3.connect(retained.as_uri() + '?mode=ro', uri=True, timeout=30)) as reader:
                for (packed_page,) in reader.execute('SELECT page_gzip FROM catalog_pages'):
                    page = unpack(packed_page)
                    digest = page.get('content_hash')
                    if page.get('state') != 'fetched' or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                        continue
                    protected.add(digest)
                    # Only a reference for this exact page version may retain
                    # its script/PDF dependencies. Stale notes do not pin old files.
                    for note in json.loads(page.get('notes_json') or '[]'):
                        if isinstance(note, str) and note.startswith('full_snapshot:'):
                            reference = json.loads(note[len('full_snapshot:'):])
                            if isinstance(reference, dict) and reference.get('hash') == digest:
                                collect(reference)
        yield protected


def _retained_page(row):
    # Derive availability from this pinned catalogue, not an old parser flag or
    # a fetched status. This reads only BLOB length; it does not inflate HTML.
    return dict(unpack(row['page_gzip']), material_available=bool(row['material_retained']))


def direct_directory_members(nodes):
    """The compatibility roster reader may expose only direct page members.

    Nested cards retain their explicit parent in the canonical structure writer;
    flattening their leaves here would add a second, unsupported membership.
    """
    owners = {node.get('node_key') or node.get('key') for node in nodes
              if node.get('relation') == 'page_identity'}
    return [node for node in nodes if node.get('kind') == 'unit'
            and node.get('relation') == 'directory_entry'
            and (not (parent := node.get('parent_key') or node.get('parent')) or parent in owners)]


class RuntimeCatalog:
    def __init__(self, path=None, *, versioned=True):
        self.path = Path(path or DATA_DIR / 'source_catalog.sqlite3')
        self.versioned = versioned

    def _read_lock_path(self):
        if self.versioned and has_app_context():
            return self.path
        if getattr(self, '_publication_root', None):
            return self._publication_root
        if has_app_context() and self.path.parent.name == 'catalog-generations' and _GENERATION_NAME.fullmatch(self.path.name):
            root = Path(current_app.config['SOURCE_CATALOG_PATH']).resolve()
            if self.path.parent.resolve() == root.parent / 'catalog-generations':
                return root
        return None

    def _published(self, key):
        if not self.versioned or not has_app_context():
            return None
        root = self.path.resolve()
        record = next(iter(_active_publications(root, key=key)), None)
        if record is None:
            return None
        target = _generation_path(root, record['path'], key=key, generation=record['generation'])
        catalog = RuntimeCatalog(target, versioned=False)
        catalog._publication_root = root
        return catalog

    @_catalog_read
    def pin(self, key):
        """Resolve one immutable published file using the existing path guards.

        The returned reader does not look up another generation on later reads.
        Its publication root retains the same read/retirement lock discipline.
        """
        return self._published(key)

    @contextmanager
    def connect(self, write=False):
        root = self._read_lock_path()
        with catalog_publication_lock(root) if root else nullcontext():
            with self._connect(write=write) as connection:
                yield connection

    @contextmanager
    def _connect(self, write=False):
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

    @_catalog_read
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
            report['pages'] = [_retained_page(r) for r in c.execute(
                'SELECT page_gzip,length(evidence_gzip)>0 AS material_retained '
                'FROM catalog_pages WHERE site_id=? ORDER BY id', (site['id'],))]
            return report

    @_catalog_read
    def monitor_snapshot(self, key):
        """Published page and link metadata; no HTML or scratch database locks."""
        published = self._published(key)
        if published:
            return published.monitor_snapshot(key)
        if not self.path.exists():
            return None
        with self.connect() as c:
            site = c.execute('SELECT id,report_gzip FROM catalog_sites WHERE site_key=?', (key,)).fetchone()
            if not site:
                return None
            summary = unpack(site['report_gzip'])
            fields = ('url', 'final_url', 'label', 'title', 'kind', 'path_json', 'state', 'health',
                      'status_code', 'checked_at', 'content_hash', 'material_available')
            pages, structure, navigation = [], [], []
            node_fields = ('name', 'url', 'node_key', 'parent_key', 'relation', 'reference_url', 'locator', 'kind',
                           'content_hash', 'observed_at')
            for row in c.execute('SELECT url,page_gzip,nodes_gzip,length(evidence_gzip)>0 AS material_retained '
                                 'FROM catalog_pages WHERE site_id=? ORDER BY id', (site['id'],)):
                page = _retained_page(row)
                compact = {field: page.get(field) for field in fields}
                from backend.services.publisher_identity import saved_observation, PREFIX
                observation = saved_observation(page, key)
                profiles = [{field: claim[field] for field in ('source', 'entity_name')}
                    for claim in observation.get('profile_names', [])
                    if claim.get('source') == 'unit_profile_self_description' and claim.get('entity_name')]
                if profiles:
                    # The projection needs the current endpoint predicate, not
                    # every parser note, navigation route or retained document.
                    profile = {field: observation[field] for field in
                        ('site_key', 'reference_url', 'final_url', 'content_hash')}
                    compact['notes_json'] = json.dumps([PREFIX + json.dumps(
                        dict(profile, profile_names=profiles), ensure_ascii=False)], ensure_ascii=False)
                pages.append(compact)
                for edge in page.get('navigation_edges', []):
                    # A DOM menu's parent is not necessarily the page containing
                    # its link. Preserve the observed source document/direction.
                    if (edge.get('parent_url') == row['url'] and edge.get('target_url')
                            and edge.get('content_hash')
                            and edge['content_hash'] == page.get('content_hash')):
                        navigation.append(edge)
                for node in unpack(row['nodes_gzip']):
                    # Stale evidence from another revision must not establish a link.
                    if node.get('content_hash') and node.get('content_hash') != page.get('content_hash'):
                        continue
                    structure.append({**{field: node.get(field) for field in node_fields},
                                      'reference_url': row['url'], 'material_available': page['material_available']})
            return {'catalogue_published_at': summary.get('catalogue_published_at'),
                    'pages': pages, 'structure_nodes': structure, 'navigation_edges': navigation,
                    'exploration_events': summary.get('exploration_events', []),
                    'exploration_history': summary.get('exploration_history', {'history_before_recording': 'unknown'})}

    @_catalog_read
    def structure(self, key):
        published = self._published(key)
        if published:
            return published.structure(key)
        if not self.path.exists():
            return []
        with self.connect() as c:
            return [dict(node, material_available=bool(row['material_retained'])) for row in c.execute(
                'SELECT p.nodes_gzip,length(p.evidence_gzip)>0 AS material_retained FROM catalog_pages p '
                'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=?', (key,))
                for node in unpack(row['nodes_gzip'])]

    @_catalog_read
    def get_page(self, key, url):
        published = self._published(key)
        if published:
            return published.get_page(key, url)
        if not self.path.exists():
            return None
        with self.connect() as c:
            row = c.execute('SELECT p.page_gzip,length(p.evidence_gzip)>0 AS material_retained '
                            'FROM catalog_pages p JOIN catalog_sites s ON s.id=p.site_id '
                            'WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
            return _retained_page(row) if row else None

    @_catalog_read
    def page_metadata(self, key, urls, *, include_listing_data=False):
        """Batch only requested publication identities, without HTML or a full report."""
        addresses = tuple(dict.fromkeys(canonical_url(url) for url in urls if url))
        if not addresses:
            return {}
        published = self._published(key)
        if published:
            return published.page_metadata(key, addresses, include_listing_data=include_listing_data)
        if not self.path.exists():
            return {}
        fields = ('url', 'final_url', 'state', 'kind', 'health', 'title', 'content_hash', 'notes_json', 'material_available')
        result = {}
        with self.connect() as c:
            for offset in range(0, len(addresses), 200):
                batch = addresses[offset:offset + 200]
                rows = c.execute('SELECT p.url,p.page_gzip,length(p.evidence_gzip)>0 AS material_retained FROM catalog_pages p '
                    'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=? AND p.url IN (' +
                    ','.join('?' for _ in batch) + ')', (key, *batch))
                for row in rows:
                    page = _retained_page(row)
                    result[row['url']] = {field: page.get(field) for field in fields}
                    if include_listing_data:
                        # Only explicit alias replay needs these local parsing
                        # inputs. Default metadata readers still omit them and
                        # neither mode inflates HTML or scans unrelated pages.
                        result[row['url']].update(checked_at=page.get('checked_at'),
                            label=page.get('label'),feed_json=page.get('feed_json'))
                    try:
                        lists = json.loads(page.get('feed_json') or '{}').get('lists', [])
                    except (TypeError, ValueError, AttributeError):
                        lists = []
                    result[row['url']]['column_regions'] = [
                        {field: item.get(field) for field in ('name', 'column_url',
                            'heading_locator', 'container_locator')}
                        for item in lists if isinstance(item, dict)]
        return result

    @_catalog_read
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
                row = c.execute('SELECT p.page_gzip,p.nodes_gzip,length(p.evidence_gzip)>0 AS material_retained FROM catalog_pages p '
                    'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
                if not row:
                    continue
                page = _retained_page(row)
                if page.get('kind') != 'directory' or page.get('state') != 'fetched':
                    continue
                entries = [dict(n, material_available=page['material_available'])
                           for n in direct_directory_members(unpack(row['nodes_gzip']))
                           if canonical_url(n.get('reference_url', '')) == url
                           and n.get('content_hash') == page.get('content_hash')]
                if entries:
                    result[url] = entries
        return result

    @_catalog_read
    def snapshot(self, key, url):
        published = self._published(key)
        if published:
            return published.snapshot(key, url)
        with self.connect() as c:
            row = c.execute('SELECT p.evidence_gzip FROM catalog_pages p JOIN catalog_sites s ON s.id=p.site_id '
                            'WHERE s.site_key=? AND p.url=?', (key, url)).fetchone()
            return gzip.decompress(row[0]).decode() if row and row[0] else ''

    @_catalog_read
    def _matching_materials(self, key, expected):
        """Reuse only literal published bodies at the same site, URL and hash."""
        published = self._published(key)
        if published:
            return published._matching_materials(key, expected)
        if not expected or not self.path.exists():
            return {}
        result = {}
        with self.connect() as reader:
            rows = reader.execute('SELECT p.url,p.page_gzip,p.evidence_gzip FROM catalog_pages p '
                'JOIN catalog_sites s ON s.id=p.site_id WHERE s.site_key=? AND p.evidence_gzip IS NOT NULL', (key,))
            for row in rows:
                digest = expected.get(row['url'])
                if not digest or unpack(row['page_gzip']).get('content_hash') != digest:
                    continue
                try:
                    html = gzip.decompress(row['evidence_gzip']).decode('utf8')
                except (OSError, EOFError, UnicodeError, zlib.error) as error:
                    raise ValueError('已发布官网证据损坏，目录发布已停止') from error
                if hashlib.sha256(html.encode()).hexdigest() != digest:
                    raise ValueError('已发布官网证据与材料指纹不符，目录发布已停止')
                result[row['url']] = html
        return result

    @_catalog_read
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

    @_catalog_read
    def all_sites(self):
        sites = {}
        if self.path.exists():
            with self.connect() as c:
                sites.update({r['site_key']: dict(r) for r in c.execute(
                    'SELECT site_key,name,root_url,updated_at FROM catalog_sites ORDER BY name')})
        if self.versioned and has_app_context():
            for record in _active_publications(self.path.resolve()):
                published = self._published(record['site_key'])
                if published:
                    sites.update({s['site_key']: s for s in published.all_sites()})
        return sorted(sites.values(), key=lambda s: s['name'])

    def publish(self, inventory, key, *, merge=False):
        if self.versioned and has_app_context():
            return self._publish_version(inventory, key, merge=merge)
        return self._publish_mutable(inventory, key, merge=merge)

    def _publish_version(self, inventory, key, *, merge=False):
        from backend.database.models import School
        from backend.services.source_inventory import site_key
        from backend.services.school_structure import sync_official_structure
        with self.publication_writer(inventory, key, merge=merge) as (catalog, generation):
            for school in School.query.all():
                if site_key(school.url) == key:
                    sync_official_structure(school, catalog, commit=False)
        return generation

    @contextmanager
    def publication_writer(self, inventory, key, *, merge=False):
        """Stage material before DML; commit the caller's facts and pointer together.

        A crashed or superseded publisher can leave an orphan file; it can never
        change what readers see without a valid main-database transaction.
        """
        import uuid
        from datetime import datetime
        from backend.database.db import db
        from backend.database.dialect import insert
        from backend.database.models import CatalogPublication, School
        from backend.services import tasks
        from backend.services.source_inventory import site_key as school_site_key
        if not self.versioned or not has_app_context():
            raise RuntimeError('材料事务需要版本化目录及应用上下文')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
            raise ValueError('Invalid catalogue site identity')
        root = self.path.resolve()
        root.parent.mkdir(parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        relative = Path('catalog-generations') / f'{key}-{generation}.sqlite3'
        directory = _generation_directory(root, create=True)
        target = directory / relative.name
        with catalog_publication_lock(root):
            if target.exists() or _link(target):
                raise RuntimeError('目录版本文件已存在')
            previous = self._published(key) or RuntimeCatalog(self.path, versioned=False)
            staged = RuntimeCatalog(target, versioned=False)
            staged._publication_root = root
            activation_started = False
            try:
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
                staged._publish_mutable(inventory, key, merge=merge, material_catalog=previous)
                with closing(sqlite3.connect(target)) as connection:
                    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    connection.execute('PRAGMA journal_mode=DELETE')
                    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('新目录版本完整性检查失败')
                tasks.assert_owned()
                schools = [school for school in School.query.all() if school_site_key(school.url) == key]
                if len(schools) > 1:
                    raise RuntimeError('同一官网对应多个学校身份，目录关系尚不能自动归属')
                unchanged = False
                if getattr(previous, '_publication_root', None):
                    staged_digest = _catalogue_digest(staged, key)
                    try:
                        unchanged = staged_digest == _catalogue_digest(previous, key)
                    except (OSError, EOFError, UnicodeError, ValueError, zlib.error) as error:
                        # Fresh verified material may repair a damaged old copy.
                        # Never treat an unreadable comparison as an unchanged view.
                        current_app.logger.warning('Previous catalogue material comparison unavailable: %s', type(error).__name__)
                if unchanged:
                    generation = _GENERATION_NAME.fullmatch(previous.path.name)['generation']
                yield (previous if unchanged else staged), generation
                # The caller stages source rules, receipts and structural facts
                # against this exact reader. It must not commit inside the scope.
                tasks.assert_owned()
                if not unchanged:
                    activation_started = True
                    statement = insert(CatalogPublication).values(site_key=key, generation=generation,
                        path=relative.as_posix(), activated_at=datetime.utcnow(), policy_version='1')
                    db.session.execute(statement.on_conflict_do_update(index_elements=['site_key'], set_={
                        field: getattr(statement.excluded, field) for field in ('generation', 'path', 'activated_at', 'policy_version')}))
                    if schools:
                        from backend.services.structure_versions import mark_structure_changed
                        mark_structure_changed(schools[0].id, 'catalog', key, fields=('generation',))
                db.session.commit()
                if unchanged:
                    _remove_unpublished(root, target)
                else:
                    if getattr(previous, '_publication_root', None):
                        try:
                            retired = _read_retirements(root)
                            retired[previous.path.name] = {'at': time.time(), 'legacy': False}
                            _write_retirements(root, retired)
                        except Exception:
                            current_app.logger.exception('Catalogue retirement metadata deferred')
            except BaseException:
                db.session.rollback()
                if not activation_started:
                    _remove_unpublished(root, target)
                raise
            # Maintenance must never turn a committed publication into a failure.
            try:
                _prune_generations(root)
            except Exception:
                current_app.logger.exception('Catalogue history maintenance deferred')

    def _publish_mutable(self, inventory, key, *, merge=False, material_catalog=None):
        from collections import defaultdict
        from backend.services.source_relationships import SourceRelationships, ROSTER_RELATIONS, PATH_RELATIONS
        from backend.services.source_catalog import publication_candidates
        report = inventory.report(key)
        if not report:
            return
        records = inventory.structure(key)
        # Publish observed page links alongside each page's existing metadata.
        # These remain navigation facts, separate from roster/ownership paths.
        # Web requests read only this catalogue, never the discovery database.
        navigation = defaultdict(list)
        with inventory.connect() as reader:
            for row in reader.execute('SELECT parent_url,target_url,label,kind,path_json,locator,decision,'
                    'content_hash,observed_at FROM edges WHERE site_key=? '
                    'ORDER BY parent_url,target_url,label,path_json,locator', (key,)):
                edge = dict(row)
                if edge['target_url']:
                    navigation[edge['parent_url']].append(edge)
        for page in report['pages']:
            page['navigation_edges'] = [edge for edge in navigation[page['url']]
                if page.get('state') == 'fetched' and edge['content_hash']
                and edge['content_hash'] == page.get('content_hash')]
        if merge:
            previous = self.report(key)
            if previous:
                # A failed revisit contains no replacement evidence. Preserve
                # the last valid page and its ownership graph while reporting
                # this attempt separately, rather than erasing working feeds.
                pages_by_url = {p['url']: p for p in previous['pages']}
                from backend.scraper.discovery.parser_revision import PARSER_REVISION
                def stale_cached_parse(page):
                    retained = pages_by_url.get(page['url'])
                    # The scratch store may still contain a previous parser's
                    # result after retained material was reparsed and published.
                    # Re-publishing that same acquisition is not a fresh read or
                    # a current reparse, and must preserve the published result.
                    return bool(retained and page.get('content_hash')
                        and page.get('content_hash') == retained.get('content_hash')
                        and page.get('checked_at') == retained.get('checked_at')
                        and page.get('parser_revision') != retained.get('parser_revision')
                        and page.get('parser_revision') != PARSER_REVISION)
                touched = {p['url'] for p in report['pages'] if p.get('checked_at')
                    and p.get('state') == 'fetched' and not stale_cached_parse(p)}
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
            'website_publisher_declaration',
            'major_directory_entry', 'programme_college', 'programme_joint_group',
            'publication_column', 'unit_channel', 'hidden_directory_entry',
            'directory_label_pending', 'unit_profile_entry', 'table_reference',
            'navigation_entry'}
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
        ownership_materials = {reference['url'] for choices in paths.values() for path in choices
            if path.get('website_identity') for reference in path.get('references', [])}
        # Cache eviction is not evidence that an official relationship changed.
        # Validate fresh material first, then reuse exact already-published bytes.
        # Prepare before DELETE so mutable catalogues obey the same rule too.
        materials = {}
        for page in pages:
            needs_material = (page['url'] in reviewed or page['url'] in ownership_materials or
                any(edge.get('locator') for edge in page.get('navigation_edges', [])) or
                any(node.get('relation') in ('sub_unit', 'attached_unit') for node in selected.get(page['url'], [])))
            if not needs_material:
                continue
            html = inventory.snapshot(key, page['url'])
            if html:
                if not isinstance(html, str) or hashlib.sha256(html.encode()).hexdigest() != page.get('content_hash'):
                    raise ValueError('官网缓存证据与材料指纹不符，目录发布已停止')
                materials[page['url']] = html
        expected = {page['url']: page['content_hash'] for page in pages if page['url'] not in materials
            and isinstance(page.get('content_hash'), str) and re.fullmatch(r'[0-9a-f]{64}', page['content_hash'])}
        materials.update((material_catalog or self)._matching_materials(key, expected))
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
                evidence = materials.get(page['url']) or ''
                blob = gzip.compress(evidence.encode(), mtime=0) if evidence else None
                c.execute('INSERT INTO catalog_pages(site_id,url,page_gzip,nodes_gzip,evidence_gzip) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(site_id,url) DO UPDATE SET page_gzip=excluded.page_gzip,nodes_gzip=excluded.nodes_gzip,'
                    'evidence_gzip=excluded.evidence_gzip',
                    (ident, page['url'], pack(page), pack(selected.get(page['url'], [])), blob))


def published_catalog():
    """Typed readers always use the published generation, including in tests."""
    return RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])


def runtime_catalog():
    # Explicit isolated legacy fixtures remain usable during the migration test suite.
    if current_app.config.get('TESTING') and current_app.config.get('SOURCE_INVENTORY_PATH'):
        from backend.services.source_inventory import Inventory
        return Inventory(current_app.config['SOURCE_INVENTORY_PATH'])
    return RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])


def prune_catalog_generations(days=7, *, dry_run=False, offline_legacy=False):
    """Bound history by count/bytes, preserving active pointers and retirement grace.

    ``days`` remains accepted for existing callers; a seven-day minimum no longer
    overrides the bounded policy. ``offline_legacy`` is only for a caller that
    independently proved the old application and all workers have stopped.
    It never shortens the normal policy persisted for future generations.
    """
    root = Path(current_app.config['SOURCE_CATALOG_PATH']).resolve()
    return _prune_generations(root, dry_run=dry_run, offline_legacy=offline_legacy)


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
