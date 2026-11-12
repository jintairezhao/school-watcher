"""Explicit local SQLite compaction, separate from logical notice deletion."""
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sqlite3
from urllib.parse import unquote, urlsplit

from filelock import FileLock, Timeout

from backend.database.db import db

BUSY_TIMEOUT_MS = 250
BUSY_MESSAGE = '数据库正在更新或有页面仍在读取，请稍后重试空间回收。'
UNSUPPORTED_MESSAGE = '当前数据库不支持手动回收本地文件空间。'


class DatabaseSpaceError(ValueError):
    def __init__(self, message, status_code=400):
        self.status_code = status_code
        super().__init__(message)


def _database_error(error):
    original = getattr(error, 'orig', error)
    code = getattr(original, 'sqlite_errorcode', 0) & 255
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or any(
            word in str(original).lower() for word in ('locked', 'busy')):
        return DatabaseSpaceError(BUSY_MESSAGE, 409)
    if code == sqlite3.SQLITE_FULL:
        return DatabaseSpaceError('磁盘空间不足，数据库空间回收未完成，请腾出空间后重试。')
    return DatabaseSpaceError('无法完成数据库空间回收，请稍后重试。')


@contextmanager
def _connection(*, writable=False):
    # This app's global SQLAlchemy connect hook changes journal mode and allows
    # thirty seconds of busy waiting. Maintenance uses its own bounded SQLite
    # connection to this existing file, bypassing that unrelated initialization.
    # Refuse a missing configured file before SQLite can silently create it.
    # The actual main path is still verified from PRAGMA database_list below.
    filename = db.engine.url.database or ''
    if filename.startswith('file:'):
        filename = unquote(urlsplit(filename).path)
        if os.name == 'nt' and filename.startswith('/') and len(filename) > 2 and filename[2] == ':':
            filename = filename[1:]
    if not filename or not Path(filename).is_file():
        raise DatabaseSpaceError('当前数据库文件不可用，请重新打开应用后重试。')
    if writable and (db.engine.url.query.get('mode') == 'ro' or db.engine.url.query.get('immutable') == '1'):
        raise DatabaseSpaceError('当前数据库为只读模式，无法回收文件空间。')
    uri = Path(filename).resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro')
    connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


def _main_path(connection):
    for _, name, filename in connection.execute('PRAGMA database_list').fetchall():
        if name == 'main':
            if not filename or filename == ':memory:':
                return None
            path = Path(filename).resolve()
            if not path.is_file():
                raise DatabaseSpaceError('当前数据库文件不可用，请重新打开应用后重试。')
            return path
    return None


def _memory_database():
    filename = db.engine.url.database or ''
    return filename in ('', ':memory:') or filename.startswith('file::memory:') or db.engine.url.query.get('mode') == 'memory'


@contextmanager
def storage_maintenance_lock():
    """One immediate lock shared by notice cleanup and file compaction.

    PostgreSQL cleanup uses row locks instead. Never interpret its database name
    or an in-memory SQLite name as a filesystem path.
    """
    if db.engine.dialect.name != 'sqlite' or _memory_database():
        yield None
        return
    try:
        with _connection() as connection:
            path = _main_path(connection)
    except sqlite3.Error as error:
        raise _database_error(error) from error
    if path is None:
        yield None
        return
    lock_path = str(path) + '.storage-maintenance.lock'
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except Timeout as error:
        if str(error.lock_file) != lock_path:
            raise
        raise DatabaseSpaceError('通知清理或空间回收正在进行，请稍后重试。', 409) from error
    except OSError as error:
        raise DatabaseSpaceError('无法访问数据库维护锁，请检查数据目录权限后重试。') from error
    try:
        yield path
    finally:
        lock.release()


def _unsupported():
    return {'supported': False, 'database_bytes': 0, 'wal_bytes': 0,
        'total_bytes': 0, 'reclaimable_bytes': 0, 'free_pages': 0,
        'page_count': 0, 'page_size': 0, 'can_compact': False,
        'message': UNSUPPORTED_MESSAGE}


def _bytes(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _stats(connection, path):
    page_size = int(connection.execute('PRAGMA main.page_size').fetchone()[0])
    page_count = int(connection.execute('PRAGMA main.page_count').fetchone()[0])
    free_pages = int(connection.execute('PRAGMA main.freelist_count').fetchone()[0])
    database_bytes, wal_bytes = _bytes(path), _bytes(Path(str(path) + '-wal'))
    return {'supported': True, 'database_bytes': database_bytes, 'wal_bytes': wal_bytes,
        'total_bytes': database_bytes + wal_bytes, 'reclaimable_bytes': free_pages * page_size,
        'free_pages': free_pages, 'page_count': page_count, 'page_size': page_size,
        'can_compact': bool(free_pages or wal_bytes)}


def database_stats():
    """Read page accounting only: visiting storage settings never runs VACUUM."""
    if db.engine.dialect.name != 'sqlite' or _memory_database():
        return _unsupported()
    try:
        with _connection() as connection:
            path = _main_path(connection)
            return _stats(connection, path) if path else _unsupported()
    except sqlite3.Error as error:
        raise _database_error(error) from error
    except OSError as error:
        raise DatabaseSpaceError('无法读取数据库文件大小，请稍后重试。') from error


def _release_read_session():
    session = db.session()
    if session.new or session.dirty or session.deleted:
        raise DatabaseSpaceError('当前还有未保存的操作，请完成后再回收空间。', 409)
    db.session.remove()  # Roll back read-only auth/settings transactions first.


def _checkpoint(connection):
    busy, log_pages, written_pages = connection.execute(
        'PRAGMA main.wal_checkpoint(TRUNCATE)').fetchone()
    if busy or (log_pages >= 0 and written_pages >= 0 and written_pages < log_pages):
        raise DatabaseSpaceError(BUSY_MESSAGE, 409)


def compact_database():
    """Compact only the active main file, without changing any retained rows."""
    if db.engine.dialect.name != 'sqlite' or _memory_database():
        raise DatabaseSpaceError(UNSUPPORTED_MESSAGE)
    _release_read_session()
    try:
        with storage_maintenance_lock() as path:
            if path is None:
                raise DatabaseSpaceError(UNSUPPORTED_MESSAGE)
            with _connection(writable=True) as connection:
                if _main_path(connection) != path:
                    raise DatabaseSpaceError('当前数据库位置已变化，请重新打开存储管理后重试。', 409)
                before = _stats(connection, path)
                if before['free_pages']:
                    # SQLite documents up to two original-file sizes for the
                    # temporary database and transactional overwrite/WAL.
                    required = 2 * max(before['database_bytes'], before['page_count'] * before['page_size'])
                    if shutil.disk_usage(path.parent).free < required:
                        raise DatabaseSpaceError('可用磁盘空间不足，无法安全回收数据库空间，请先腾出空间。')
                vacuumed = False
                if before['can_compact']:
                    _checkpoint(connection)
                    if before['free_pages']:
                        connection.execute('VACUUM main')
                        vacuumed = True
                    _checkpoint(connection)
                after = _stats(connection, path)
                reclaimed = max(0, before['total_bytes'] - after['total_bytes'])
                return {'supported': True, 'reclaimed_bytes': reclaimed,
                    'before_bytes': before['total_bytes'], 'after_bytes': after['total_bytes'],
                    'database_bytes': after['database_bytes'], 'wal_bytes': after['wal_bytes'],
                    'reclaimable_bytes': after['reclaimable_bytes'], 'free_pages': after['free_pages'],
                    'vacuumed': vacuumed, 'state': 'reclaimed' if reclaimed else 'unchanged',
                    'message': '数据库空间已回收。' if reclaimed else '当前没有需要回收的数据库空间。'}
    except sqlite3.Error as error:
        raise _database_error(error) from error
    except OSError as error:
        raise DatabaseSpaceError('无法访问数据库文件，请检查磁盘空间和数据目录权限后重试。') from error
