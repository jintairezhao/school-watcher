"""Immutable archives of superseded parser observations, never current facts.

Commit a checked file before replacing its working rows with a manifest. A
failed transaction can leave an unreferenced file; it cannot lose observations.
Current pages and relations are outside this rotation. Execution event payloads
may move to archives, retaining their original identities and query index.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid

from filelock import FileLock

FORMAT = 'school-watcher-discovery-history'
TABLES = {'edge_history': 'parent_url', 'structure_history': 'reference_url'}
EVENT_COLUMNS = ('sequence', 'event_id', 'site_key', 'url', 'event_type', 'parent_event_id', 'observed_at', 'payload_json')
HOT_EVENT_COUNT = 4000
HOT_EVENT_DAYS = 30
MAX_ROWS = 128
MAX_SEGMENT_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
_RELATIVE = re.compile(r'\d{4}-\d{2}/\d{4}-\d{2}-\d{2}/[0-9a-f]{64}\.json\.gz')


def archive_root(progress_path):
    return Path(str(Path(progress_path).absolute()) + '.history')


def archive_lock(progress_path):
    return FileLock(str(Path(progress_path).absolute()) + '.history.lock', timeout=60)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_archive_segments'").fetchone()


def ensure_schema(connection):
    # Do not use executescript here: it would commit the caller's transaction.
    connection.execute('CREATE TABLE IF NOT EXISTS discovery_archive_state '
        '(id INTEGER PRIMARY KEY CHECK(id=1),stream_id TEXT NOT NULL,next_batch INTEGER NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS discovery_archive_segments '
        '(archive_id TEXT PRIMARY KEY,relative_path TEXT NOT NULL UNIQUE,source_table TEXT NOT NULL,'
        'site_key TEXT NOT NULL,observation_day TEXT NOT NULL,batch_number INTEGER NOT NULL UNIQUE,'
        'record_count INTEGER NOT NULL,compressed_bytes INTEGER NOT NULL,compressed_sha256 TEXT NOT NULL)')
    connection.execute('CREATE INDEX IF NOT EXISTS discovery_archive_site_table ON discovery_archive_segments(source_table,site_key)')
    connection.execute('INSERT OR IGNORE INTO discovery_archive_state VALUES(1,?,1)', (uuid.uuid4().hex,))


def _safe_path(root, relative, *, create=False):
    if not isinstance(relative, str) or not _RELATIVE.fullmatch(relative):
        raise ValueError('Invalid discovery archive path')
    day = relative.split('/')[1]
    datetime.strptime(day, '%Y-%m-%d')
    if relative.split('/')[0] != day[:7]:
        raise ValueError('Discovery archive month does not match its day')
    root = Path(root).absolute()
    # Reject links and Windows junctions before both reads and directory writes.
    target = root / relative
    for part in reversed((target, *target.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Discovery archive path contains a link')
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        return _safe_path(root, relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Discovery archive path escaped its directory')
    return target


def _unzip(value, maximum):
    with gzip.GzipFile(fileobj=io.BytesIO(value)) as reader:
        result = reader.read(maximum + 1)
    if len(result) > maximum:
        raise ValueError('Discovery archive material exceeds its size limit')
    return result


def _day(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Discovery history timestamp has no time zone')
    return stamp.astimezone(timezone.utc).date().isoformat()


def _encode_row(table, row):
    record = dict(row)
    if table == 'exploration_events':
        if (set(record) != set(EVENT_COLUMNS) or type(record['sequence']) is not int or record['sequence'] < 1
                or any(not isinstance(record[field], str) or not record[field] for field in
                    ('event_id', 'site_key', 'url', 'event_type', 'observed_at', 'payload_json'))
                or record['parent_event_id'] is not None and not isinstance(record['parent_event_id'], str)
                or len(record['payload_json'].encode('utf-8')) > MAX_RECORD_BYTES
                or not isinstance(json.loads(record['payload_json']), dict)):
            raise ValueError('Invalid exploration archive record')
        _day(record['observed_at'])
        return record
    # Check that existing compressed history is readable, without reinterpreting
    # it or substituting a newly inferred navigation/ownership relationship.
    old = json.loads(_unzip(record['records_gzip'], MAX_RECORD_BYTES))
    if not isinstance(old, list) or not all(isinstance(item, dict) for item in old):
        raise ValueError('Invalid discovery parser history')
    if any(item.get('site_key') != row['site_key'] or item.get(TABLES[table]) != row[TABLES[table]] for item in old):
        raise ValueError('Discovery parser history belongs to another page or site')
    record['records_gzip'] = base64.b64encode(record['records_gzip']).decode('ascii')
    return record


def read_segment(root, manifest, stream_id):
    """Validate both the compressed file and its original-record fingerprint."""
    record = dict(manifest)
    path = _safe_path(root, record['relative_path'])
    if path.stat().st_size != record['compressed_bytes'] or path.stat().st_size > MAX_SEGMENT_BYTES:
        raise ValueError('Discovery archive size mismatch')
    packed = path.read_bytes()
    if hashlib.sha256(packed).hexdigest() != record['compressed_sha256']:
        raise ValueError('Discovery archive checksum mismatch')
    raw = _unzip(packed, MAX_SEGMENT_BYTES)
    if hashlib.sha256(raw).hexdigest() != record['archive_id'] or path.name != record['archive_id']+'.json.gz':
        raise ValueError('Discovery archive identity mismatch')
    value = json.loads(raw)
    if (value.get('format') != FORMAT or value.get('version') != 1 or value.get('stream_id') != stream_id
            or value.get('source_table') not in {*TABLES, 'exploration_events'} or value.get('source_table') != record['source_table']
            or value.get('site_key') != record['site_key'] or value.get('observation_day') != record['observation_day']
            or value.get('batch_number') != record['batch_number'] or not isinstance(value.get('records'), list)
            or not 0 < len(value['records']) <= MAX_ROWS or len(value['records']) != record['record_count']
            or record['relative_path'].split('/')[1] != record['observation_day']):
        raise ValueError('Discovery archive metadata mismatch')
    rows = []
    for row in value['records']:
        if record['source_table'] == 'exploration_events':
            decoded = _encode_row('exploration_events', row)
            if decoded['site_key'] != record['site_key'] or _day(decoded['observed_at']) != record['observation_day']:
                raise ValueError('Exploration archive belongs to another day or site')
            rows.append(decoded)
            continue
        expected = {'id', 'site_key', TABLES[record['source_table']], 'archived_at', 'records_gzip'}
        if (not isinstance(row, dict) or set(row) != expected or type(row['id']) is not int or row['id'] < 1
                or row['site_key'] != record['site_key'] or _day(row['archived_at']) != record['observation_day']):
            raise ValueError('Invalid discovery archive record')
        decoded = dict(row, records_gzip=base64.b64decode(row['records_gzip'], validate=True))
        _encode_row(record['source_table'], decoded)
        rows.append(decoded)
    identity = 'event_id' if record['source_table'] == 'exploration_events' else 'id'
    if len({row[identity] for row in rows}) != len(rows):
        raise ValueError('Duplicate discovery archive record')
    return rows


def _write_segment(root, envelope):
    raw = _json(envelope)
    if len(raw) > MAX_SEGMENT_BYTES:
        raise ValueError('Discovery archive segment exceeds its size limit')
    digest = hashlib.sha256(raw).hexdigest()
    packed = gzip.compress(raw, mtime=0)
    relative = envelope['observation_day'][:7]+'/'+envelope['observation_day']+'/'+digest+'.json.gz'
    path = _safe_path(root, relative, create=True)
    manifest = dict(archive_id=digest, relative_path=relative, source_table=envelope['source_table'],
        site_key=envelope['site_key'], observation_day=envelope['observation_day'],
        batch_number=envelope['batch_number'], record_count=len(envelope['records']),
        compressed_bytes=len(packed), compressed_sha256=hashlib.sha256(packed).hexdigest())
    if not path.exists():
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='pending-', suffix='.tmp', delete=False) as writer:
                pending = Path(writer.name)
                writer.write(packed)
                writer.flush()
                os.fsync(writer.fileno())
            pending.replace(path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    read_segment(root, manifest, envelope['stream_id'])
    return manifest


def rotate_history(inventory):
    """Move superseded parses into checked UTC month/day segments.

    Each bounded batch holds a SQLite write transaction across file validation.
    Batch numbers survive row-id reuse; retries after rollback reuse the same
    immutable bytes. No current relation or execution event is removed.
    """
    result = dict(archived_records=0, archive_segments=0)
    with archive_lock(inventory.path):
        with inventory.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_schema(connection)
        for table in TABLES:
            while True:
                with inventory.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    rows = connection.execute('SELECT id,site_key,archived_at,length(records_gzip) AS packed_size FROM '
                        +table+' ORDER BY id LIMIT ?', (MAX_ROWS,)).fetchall()
                    if not rows:
                        break
                    first = rows[0]
                    day = _day(first['archived_at'])
                    rows = [row for row in rows if row['site_key'] == first['site_key'] and _day(row['archived_at']) == day]
                    encoded, originals, size = [], [], 0
                    for candidate in rows:
                        if encoded and size + candidate['packed_size'] * 4 // 3 > MAX_SEGMENT_BYTES // 2:
                            break
                        if candidate['packed_size'] > MAX_SEGMENT_BYTES // 2:
                            raise ValueError('Discovery history record exceeds its size limit')
                        row = connection.execute('SELECT * FROM '+table+' WHERE id=?', (candidate['id'],)).fetchone()
                        item = _encode_row(table, row)
                        length = len(_json(item))
                        if encoded and size + length > MAX_SEGMENT_BYTES // 2:
                            break
                        encoded.append(item); originals.append(row); size += length
                    stream_id, batch = connection.execute('SELECT stream_id,next_batch FROM discovery_archive_state WHERE id=1').fetchone()
                    envelope = dict(format=FORMAT, version=1, stream_id=stream_id, batch_number=batch,
                        source_table=table, site_key=first['site_key'], observation_day=day, records=encoded)
                    manifest = _write_segment(archive_root(inventory.path), envelope)
                    connection.execute('INSERT INTO discovery_archive_segments VALUES(?,?,?,?,?,?,?,?,?)', tuple(manifest.values()))
                    connection.executemany('DELETE FROM '+table+' WHERE id=? AND records_gzip=?',
                        [(row['id'], row['records_gzip']) for row in originals])
                    connection.execute('UPDATE discovery_archive_state SET next_batch=next_batch+1 WHERE id=1')
                result['archived_records'] += len(originals)
                result['archive_segments'] += 1
    return result


def ensure_event_archive_schema(connection):
    ensure_schema(connection)
    connection.execute('CREATE TABLE IF NOT EXISTS exploration_event_archive_index ('
        'sequence INTEGER NOT NULL UNIQUE,event_id TEXT PRIMARY KEY,site_key TEXT NOT NULL,url TEXT NOT NULL,'
        'event_type TEXT NOT NULL,parent_event_id TEXT,observed_at TEXT NOT NULL,archive_id TEXT NOT NULL,'
        'row_offset INTEGER NOT NULL,record_sha256 TEXT NOT NULL)')
    connection.execute('CREATE INDEX IF NOT EXISTS archived_event_site_sequence ON exploration_event_archive_index(site_key,sequence)')
    connection.execute('CREATE INDEX IF NOT EXISTS archived_event_page_origin ON exploration_event_archive_index(site_key,url,event_type,sequence)')
    connection.execute('CREATE INDEX IF NOT EXISTS archived_event_parent ON exploration_event_archive_index(site_key,parent_event_id,event_type,sequence)')
    connection.execute('CREATE TRIGGER IF NOT EXISTS reject_archived_event_identity BEFORE INSERT ON exploration_events '
        'WHEN EXISTS (SELECT 1 FROM exploration_event_archive_index WHERE event_id=NEW.event_id OR sequence=NEW.sequence) '
        "BEGIN SELECT RAISE(ABORT,'Exploration event identity is already archived'); END")


def rotate_events(inventory, *, keep_recent=None, older_than_days=None, now=None):
    """Bound hot payloads by count/age, preserving all indexed observations.

    An event is eligible when outside its site's recent count OR older than
    the age window. A zero age disables that criterion; zero count archives all.
    This controls hot payloads, not total historical/index bytes.
    """
    keep = HOT_EVENT_COUNT if keep_recent is None else keep_recent
    days = HOT_EVENT_DAYS if older_than_days is None else older_than_days
    if type(keep) is not int or keep < 0 or type(days) is not int or days < 0:
        raise ValueError('Invalid exploration hot storage policy')
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('Exploration rotation clock has no time zone')
    cutoff = (clock - timedelta(days=days)).isoformat() if days else None
    result = dict(archived_events=0, archive_segments=0)
    with archive_lock(inventory.path):
        with inventory.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_event_archive_schema(connection)
            keys = [row[0] for row in connection.execute('SELECT DISTINCT site_key FROM exploration_events ORDER BY site_key')]
        for key in keys:
            while True:
                with inventory.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    if keep:
                        row = connection.execute('SELECT sequence FROM exploration_events WHERE site_key=? '
                            'ORDER BY sequence DESC LIMIT 1 OFFSET ?', (key, keep-1)).fetchone()
                        boundary = row[0] if row else 0
                    else:
                        boundary = connection.execute('SELECT coalesce(max(sequence),0)+1 FROM exploration_events WHERE site_key=?', (key,)).fetchone()[0]
                    candidates = connection.execute('SELECT sequence,observed_at,length(CAST(payload_json AS BLOB)) AS packed_size '
                        'FROM exploration_events WHERE site_key=? AND (sequence<? OR (? IS NOT NULL '
                        'AND julianday(observed_at)<julianday(?))) ORDER BY sequence LIMIT ?',
                        (key, boundary, cutoff, cutoff, MAX_ROWS)).fetchall()
                    if not candidates:
                        break
                    day = _day(candidates[0]['observed_at'])
                    records, size = [], 0
                    for candidate in candidates:
                        if _day(candidate['observed_at']) != day:
                            continue
                        if candidate['packed_size'] > MAX_RECORD_BYTES:
                            raise ValueError('Exploration event payload exceeds its size limit')
                        if records and size + candidate['packed_size'] > MAX_SEGMENT_BYTES // 2:
                            break
                        record = dict(connection.execute('SELECT * FROM exploration_events WHERE sequence=?', (candidate['sequence'],)).fetchone())
                        encoded = _encode_row('exploration_events', record)
                        length = len(_json(encoded))
                        if records and size + length > MAX_SEGMENT_BYTES // 2:
                            break
                        records.append(encoded); size += length
                    stream_id, batch = connection.execute('SELECT stream_id,next_batch FROM discovery_archive_state WHERE id=1').fetchone()
                    envelope = dict(format=FORMAT, version=1, stream_id=stream_id, batch_number=batch,
                        source_table='exploration_events', site_key=key, observation_day=day, records=records)
                    manifest = _write_segment(archive_root(inventory.path), envelope)
                    connection.execute('INSERT INTO discovery_archive_segments VALUES(?,?,?,?,?,?,?,?,?)', tuple(manifest.values()))
                    for offset, record in enumerate(records):
                        connection.execute('INSERT INTO exploration_event_archive_index VALUES(?,?,?,?,?,?,?,?,?,?)',
                            tuple(record[name] for name in EVENT_COLUMNS if name != 'payload_json')+
                            (manifest['archive_id'], offset, hashlib.sha256(_json(record)).hexdigest()))
                    connection.executemany('DELETE FROM exploration_events WHERE event_id=? AND sequence=?',
                        [(row['event_id'], row['sequence']) for row in records])
                    connection.execute('UPDATE discovery_archive_state SET next_batch=next_batch+1 WHERE id=1')
                result['archived_events'] += len(records)
                result['archive_segments'] += 1
    return result


def archive_manifest(connection):
    if not _exists(connection):
        return None, []
    state = connection.execute('SELECT stream_id FROM discovery_archive_state WHERE id=1').fetchone()
    if not state:
        raise ValueError('Discovery archive stream identity is missing')
    return state[0], [dict(row) for row in connection.execute('SELECT * FROM discovery_archive_segments ORDER BY batch_number')]


def iter_history(inventory, table, *, site_key=None):
    """Read archived observations followed by remaining working rows.

    This deliberately is not an Inventory.structure/read-model fallback. Old
    observations must never reappear as current website facts after cleanup.
    """
    if table not in TABLES:
        raise ValueError('Unknown discovery history table')
    with inventory.connect() as connection:
        connection.execute('BEGIN')
        stream_id, manifests = archive_manifest(connection)
        for item in manifests:
            if item['source_table'] == table and (site_key is None or item['site_key'] == site_key):
                yield from read_segment(archive_root(inventory.path), item, stream_id)
        statement = 'SELECT * FROM '+table+(' WHERE site_key=?' if site_key is not None else '')+' ORDER BY id'
        for row in connection.execute(statement, (site_key,) if site_key is not None else ()):
            yield dict(row)


def backup_members(progress_path, snapshot_path):
    """Use the backup's SQLite manifest, not a directory scan or live SQL state."""
    with closing(sqlite3.connect(Path(snapshot_path).resolve().as_uri()+'?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        stream_id, manifests = archive_manifest(connection)
        from .exploration_event_store import verify_archive_index
        verify_archive_index(connection, archive_root(progress_path), stream_id, manifests)
    for item in manifests:
        read_segment(archive_root(progress_path), item, stream_id)
        yield _safe_path(archive_root(progress_path), item['relative_path']), item['relative_path']


def archive_files(progress_path):
    """Inventory managed physical files, including crash-orphaned segments.

    Enumerate fixed month/day levels and reject links before descending. This
    counts storage without interpreting old observations or decompressing them.
    """
    root = archive_root(progress_path)
    if not root.exists():
        return
    # Validate the root before globbing; the dummy filename is never created.
    _safe_path(root, '2000-01/2000-01-01/'+'0'*64+'.json.gz')
    for month in root.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9]'):
        _safe_path(root, month.name+'/'+month.name+'-01/'+'0'*64+'.json.gz')
        for day in month.glob(month.name+'-[0-9][0-9]'):
            _safe_path(root, month.name+'/'+day.name+'/'+'0'*64+'.json.gz')
            for path in day.glob('*.json.gz'):
                relative = path.relative_to(root).as_posix()
                if _RELATIVE.fullmatch(relative):
                    checked = _safe_path(root, relative)
                    if checked.is_file():
                        yield checked


def verify_restored(progress_path, manifest_names):
    expected = {Path(progress_path).name+'.history/'+relative for _, relative in backup_members(progress_path, progress_path)}
    actual = {name for name in manifest_names if name.startswith(Path(progress_path).name+'.history/')}
    if expected != actual:
        raise ValueError('Discovery backup archive membership mismatch')
