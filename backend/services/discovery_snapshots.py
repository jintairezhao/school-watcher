"""Evictable HTML, stored separately from durable discovery progress."""
from contextlib import contextmanager
import gzip
from pathlib import Path
import sqlite3


class DiscoverySnapshots:
    def __init__(self, progress_path):
        self.path = Path(str(progress_path) + '.snapshots.sqlite3')
        with self.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                site_key TEXT NOT NULL, url TEXT NOT NULL, content_hash TEXT NOT NULL,
                body_gzip BLOB NOT NULL, checked_at TEXT NOT NULL,
                PRIMARY KEY(site_key,url))""")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def put(self, row):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)', row)

    def get(self, key, url, content_hash):
        with self.connect() as c:
            row = c.execute('SELECT body_gzip FROM snapshots WHERE site_key=? AND url=? AND content_hash=?',
                            (key, url, content_hash)).fetchone()
        return gzip.decompress(row[0]).decode() if row else None

    def migrate(self, inventory):
        # Commit the destination before removing legacy rows. An interrupted
        # upgrade may leave duplicates, never a missing source or lost progress.
        with inventory.connect() as source:
            source.execute('BEGIN IMMEDIATE')
            rows = source.execute('SELECT * FROM snapshots').fetchall()
            if not rows:
                return False
            with self.connect() as target:
                target.executemany("""INSERT INTO snapshots VALUES(?,?,?,?,?)
                    ON CONFLICT(site_key,url) DO UPDATE SET
                        content_hash=excluded.content_hash,body_gzip=excluded.body_gzip,
                        checked_at=excluded.checked_at
                    WHERE excluded.checked_at>=snapshots.checked_at""", [tuple(r) for r in rows])
            source.execute('DELETE FROM snapshots')
        return True

    def trim(self, *, cutoff=None, limit=0, all_cache=False):
        with self.connect() as c:
            before = c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            if all_cache:
                c.execute('DELETE FROM snapshots')
            elif cutoff:
                c.execute('DELETE FROM snapshots WHERE checked_at<?', (cutoff,))
            if limit:
                size = c.execute('SELECT coalesce(sum(length(body_gzip)),0) FROM snapshots').fetchone()[0]
                # Leave space for SQLite rows/indexes; remove oldest documents,
                # including a single document larger than the entire budget.
                for row in c.execute('SELECT site_key,url,length(body_gzip) size FROM snapshots ORDER BY checked_at').fetchall():
                    if size <= limit * 3 // 4:
                        break
                    c.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', (row['site_key'], row['url']))
                    size -= row['size']
            remaining = c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        if remaining < before or limit and self.path.stat().st_size > limit:
            with self.connect() as c:
                c.execute('VACUUM')
        # Long URL indexes also occupy space. Bound the actual cache file, not
        # only HTML payload, without ever touching the separate progress file.
        while limit and self.path.stat().st_size > limit and remaining:
            with self.connect() as c:
                c.execute('DELETE FROM snapshots WHERE rowid IN '
                          '(SELECT rowid FROM snapshots ORDER BY checked_at LIMIT ?)', (max(1, remaining // 2),))
                remaining = c.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            with self.connect() as c:
                c.execute('VACUUM')
        return before - remaining
