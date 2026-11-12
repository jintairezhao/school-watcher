"""Read the same execution stream across hot rows and checked immutable files.

The SQL index is a locator, not authority for a cold event's contents. Every
materialized cold row must match its file, stream, original identity and hash.
"""
from collections import OrderedDict
import hashlib
from pathlib import Path

from .discovery_history import EVENT_COLUMNS, _json, archive_root, read_segment


def has_archive_index(connection):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='exploration_event_archive_index'").fetchone())


def _verify_index_row(index, record):
    if (any(index[field] != record[field] for field in EVENT_COLUMNS if field != 'payload_json')
            or index['record_sha256'] != hashlib.sha256(_json(record)).hexdigest()):
        raise ValueError('Exploration archive index does not match its original record')


class EventReader:
    def __init__(self, connection):
        self.connection = connection
        # Pin schema and rows together. In particular, a reader that saw no
        # cold index must keep seeing the hot rows during the first rotation.
        if not connection.in_transaction:
            connection.execute('BEGIN')
        self.has_cold = has_archive_index(connection)
        self.segments = OrderedDict()
        self.validated_sites = set()
        if not self.has_cold:
            manifest_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='discovery_archive_segments'").fetchone()
            if manifest_exists and connection.execute("SELECT 1 FROM discovery_archive_segments "
                    "WHERE source_table='exploration_events' LIMIT 1").fetchone():
                raise ValueError('Exploration archive query index is missing')

    def _validate_site(self, key):
        if not self.has_cold or key in self.validated_sites:
            return
        declared, indexed = self.connection.execute('SELECT (SELECT coalesce(sum(record_count),0) FROM discovery_archive_segments '
            "WHERE source_table='exploration_events' AND site_key=?),"
            '(SELECT count(*) FROM exploration_event_archive_index WHERE site_key=?)', (key, key)).fetchone()
        if declared != indexed:
            raise ValueError('Exploration archive query index has missing records or invalid references')
        self.validated_sites.add(key)

    def _record(self, header):
        if header['archive_id'] is None:
            return {name: header[name] for name in EVENT_COLUMNS}
        archive_id = header['archive_id']
        if archive_id not in self.segments:
            state = self.connection.execute('SELECT stream_id FROM discovery_archive_state WHERE id=1').fetchone()
            manifest = self.connection.execute('SELECT * FROM discovery_archive_segments WHERE archive_id=?', (archive_id,)).fetchone()
            if not state or not manifest or manifest['source_table'] != 'exploration_events':
                raise ValueError('Exploration archive reference is missing')
            database = next((row[2] for row in self.connection.execute('PRAGMA database_list') if row[1] == 'main'), '')
            if not database:
                raise ValueError('Exploration archive has no working database location')
            records = read_segment(archive_root(Path(database)), manifest, state[0])
            self.segments[archive_id] = records
            if len(self.segments) > 3:
                self.segments.popitem(last=False)
        self.segments.move_to_end(archive_id)
        records = self.segments[archive_id]
        offset = header['row_offset']
        if type(offset) is not int or not 0 <= offset < len(records):
            raise ValueError('Exploration archive row location is invalid')
        record = records[offset]
        _verify_index_row(header, record)
        return dict(record)

    def query(self, key, *, event_id=None, url=None, types=None, parent_id=None, limit=None, descending=False):
        self._validate_site(key)
        hot = ('SELECT '+','.join(EVENT_COLUMNS)+',NULL AS archive_id,NULL AS row_offset,NULL AS record_sha256 FROM exploration_events')
        if self.has_cold:
            cold = ('SELECT '+','.join('NULL AS payload_json' if name == 'payload_json' else name for name in EVENT_COLUMNS)
                +',archive_id,row_offset,record_sha256 FROM exploration_event_archive_index')
            hot += ' UNION ALL '+cold
        filters, values = ['site_key=?'], [key]
        for field, value in (('event_id', event_id), ('url', url), ('parent_event_id', parent_id)):
            if value is not None:
                filters.append(field+'=?'); values.append(value)
        if types is not None:
            if not types:
                return []
            filters.append('event_type IN ('+','.join('?' for _ in types)+')'); values.extend(types)
        statement = 'SELECT * FROM ('+hot+') WHERE '+' AND '.join(filters)+' ORDER BY sequence'+(' DESC' if descending else '')
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise ValueError('Invalid exploration read limit')
            statement += ' LIMIT ?'; values.append(limit)
        return [self._record(dict(row)) for row in self.connection.execute(statement, values)]

    def get(self, key, event_id, **filters):
        if not isinstance(event_id, str) or not event_id:
            return None
        rows = self.query(key, event_id=event_id, limit=2, **filters)
        if len(rows) > 1:
            raise ValueError('Duplicate exploration event identity across storage tiers')
        return rows[0] if rows else None

    def total(self, key):
        self._validate_site(key)
        statement = 'SELECT (SELECT count(*) FROM exploration_events WHERE site_key=?)'
        values = [key]
        if self.has_cold:
            statement += '+(SELECT count(*) FROM exploration_event_archive_index WHERE site_key=?)'
            values.append(key)
        return self.connection.execute(statement, values).fetchone()[0]


def verify_archive_index(connection, root, stream_id, manifests):
    """A full backup must retain a bijection between cold rows and file records."""
    events = [item for item in manifests if item['source_table'] == 'exploration_events']
    exists = has_archive_index(connection)
    if events and not exists:
        raise ValueError('Exploration archive query index is missing')
    if not exists:
        return
    orphan = connection.execute('SELECT 1 FROM exploration_event_archive_index i LEFT JOIN discovery_archive_segments m '
        "ON m.archive_id=i.archive_id WHERE m.archive_id IS NULL OR m.source_table!='exploration_events' LIMIT 1").fetchone()
    duplicate = connection.execute('SELECT 1 FROM exploration_events h JOIN exploration_event_archive_index c '
        'ON h.event_id=c.event_id OR h.sequence=c.sequence LIMIT 1').fetchone()
    if orphan or duplicate:
        raise ValueError('Exploration archive query index has an invalid reference or duplicate')
    for manifest in events:
        records = read_segment(root, manifest, stream_id)
        indexes = connection.execute('SELECT * FROM exploration_event_archive_index WHERE archive_id=? ORDER BY row_offset',
            (manifest['archive_id'],)).fetchall()
        if len(indexes) != len(records):
            raise ValueError('Exploration archive query index has missing records')
        for offset, (index, record) in enumerate(zip(indexes, records)):
            if index['row_offset'] != offset:
                raise ValueError('Exploration archive query index has invalid row order')
            _verify_index_row(index, record)
