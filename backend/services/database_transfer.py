"""Offline whole-database migration. Portable notice imports are a separate API."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import shutil

from alembic.script import ScriptDirectory
from filelock import FileLock
from flask_migrate import upgrade
import sqlalchemy as sa

from backend import create_app
from backend.core.config import ROOT_DIR
from backend.database.db import db
from backend.database.provider_backup import sqlite_snapshot


def sqlite_to_postgres(source, target_url, destination, *, catalog=None, stopped=False):
    """Copy a consistent, upgraded snapshot into an EMPTY PostgreSQL database.

    Source files are never changed. All target schema/data changes share one
    PostgreSQL transaction; a rejected or failed copy leaves no partial tables.
    The operator must stop web and workers until cutover or abandon the target.
    """
    if not stopped:
        raise ValueError('完整搬迁需要先停止网站和所有采集进程，避免快照之后出现新数据')
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('SQLite 源文件不存在')
    url = sa.engine.make_url(target_url)
    if url.get_backend_name() != 'postgresql':
        raise ValueError('目标必须是空的 PostgreSQL 数据库')
    url = url.set(drivername='postgresql+psycopg')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('搬迁文件的目标目录必须为空')
    catalog = Path(catalog).resolve() if catalog else source.parent / 'source_catalog.sqlite3'
    catalog.parent.mkdir(parents=True, exist_ok=True)
    target = sa.create_engine(url, connect_args={'options': '-c timezone=UTC'})
    created_files, counts = [], {}
    try:
        if sa.inspect(target).get_table_names():
            raise ValueError('目标数据库必须为空，不会覆盖已有账号或通知')
        with FileLock(str(catalog) + '.publish.lock', timeout=60), tempfile.TemporaryDirectory() as scratch:
            snapshot = Path(scratch) / 'source.sqlite3'
            sqlite_snapshot(source, snapshot)
            app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(snapshot),
                              'SOURCE_CATALOG_PATH': str(catalog)})
            with app.app_context():
                try:
                    upgrade(directory=str(ROOT_DIR / 'migrations'))
                    with closing(sqlite3.connect(snapshot)) as check:
                        if check.execute('PRAGMA foreign_key_check').fetchone():
                            raise ValueError('源数据库存在失效关联，请先修复再搬迁')
                    unknown = set(sa.inspect(db.engine).get_table_names()) - set(db.metadata.tables) - {'alembic_version'}
                    if unknown:
                        raise ValueError('源数据库含未识别的数据表，搬迁已停止：' + ', '.join(sorted(unknown)))
                    with db.engine.connect() as reader, target.begin() as writer:
                        writer.execute(sa.text('SELECT pg_advisory_xact_lock(1937202602)'))
                        if sa.inspect(writer).get_table_names():
                            raise ValueError('目标数据库已被其他进程初始化')
                        db.metadata.create_all(writer)
                        for table in db.metadata.sorted_tables:
                            count = 0
                            result = reader.execute(sa.select(table)).mappings()
                            while batch := result.fetchmany(500):
                                writer.execute(table.insert(), [dict(row) for row in batch])
                                count += len(batch)
                            actual = writer.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                            if actual != count:
                                raise RuntimeError('搬迁行数校验失败：' + table.name)
                            counts[table.name] = count
                        publications = db.metadata.tables['catalog_publications']
                        files = [(catalog, catalog.name)] if catalog.exists() else []
                        for relative in reader.execute(sa.select(publications.c.path)).scalars():
                            path = (catalog.parent / relative).resolve()
                            if not path.is_relative_to(catalog.parent) or not path.is_file():
                                raise ValueError('已发布目录版本缺失或路径无效')
                            files.append((path, path.relative_to(catalog.parent).as_posix()))
                        for path, relative in dict(files).items():
                            output = destination / relative
                            output.parent.mkdir(parents=True, exist_ok=True)
                            created_files.append(output)
                            sqlite_snapshot(path, output)
                        from backend.services.backups import verify_evidence_file
                        evidence_root = source.parent / 'source-governance-evidence'
                        if evidence_root.is_dir():
                            for evidence_file in evidence_root.glob('*.html.gz'):
                                if evidence_file.is_symlink():
                                    raise ValueError('网页证据路径不能是符号链接')
                                verify_evidence_file(evidence_file)
                                output = destination / 'source-governance-evidence' / evidence_file.name
                                output.parent.mkdir(parents=True, exist_ok=True)
                                created_files.append(output)
                                shutil.copyfile(evidence_file, output)
                        # In-flight execution and authentication channels cannot migrate.
                        # Business rows, passwords, subscriptions and personal states do.
                        for name in ('runtime_leases', 'worker_heartbeats', 'origin_permits', 'verification_waiters'):
                            writer.execute(db.metadata.tables[name].delete())
                        tasks = db.metadata.tables['background_tasks']
                        active = tasks.c.state.in_(['running', 'pending', 'waiting', 'needs_manual', 'deferred'])
                        writer.execute(tasks.update().where(active).values(state='pending',
                            capability=sa.case((tasks.c.kind.in_(['directory', 'discover']), 'directory'), else_='http'), phase='fetch',
                            checkpoint={}, token=None, worker_id=None, lease_until=None, deadline_at=None,
                            generation=tasks.c.generation + 1, available_at=datetime.utcnow(), error='', error_code=''))
                        sessions = db.metadata.tables['verification_sessions']
                        writer.execute(sessions.update().where(sessions.c.status.in_(['required', 'pending', 'opening', 'active', 'verifying']))
                            .values(status='expired', generation=None, runtime_id=None,
                                    error_message='数据库已搬迁，请重新发起访问验证'))
                        from backend.database.provider_backup import fence_restored_ai
                        fence_restored_ai(writer)
                        for table in db.metadata.sorted_tables:
                            for column in table.primary_key:
                                if not isinstance(column.type, sa.Integer):
                                    continue
                                sequence = writer.execute(sa.text('SELECT pg_get_serial_sequence(:table, :column)'),
                                    {'table': table.name, 'column': column.name}).scalar()
                                if sequence:
                                    maximum = writer.execute(sa.select(sa.func.max(column))).scalar()
                                    writer.execute(sa.text('SELECT setval(CAST(:seq AS regclass), :value, :called)'),
                                        {'seq': sequence, 'value': maximum or 1, 'called': maximum is not None})
                        version = ScriptDirectory(str(ROOT_DIR / 'migrations')).get_current_head()
                        writer.execute(sa.text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
                        writer.execute(sa.text('INSERT INTO alembic_version VALUES (:version)'), {'version': version})
                finally:
                    db.session.remove()
                    db.engine.dispose()
        return {'tables': counts, 'revision': version, 'catalog_files': len(created_files),
                'runtime_reset': True, 'source_unchanged': True}
    except Exception:
        for path in reversed(created_files):
            if path.resolve().is_relative_to(destination):
                path.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()
