"""Provider-native, consistent backup operations; credentials never enter argv."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess

from sqlalchemy.engine import make_url


def sqlite_snapshot(source, target):
    source, target = Path(source).resolve(), Path(target)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader, closing(sqlite3.connect(target)) as writer:
        reader.backup(writer)
        if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库备份完整性检查失败')


def pg_environment(uri):
    url = make_url(uri)
    if url.get_backend_name() != 'postgresql':
        raise ValueError('需要 PostgreSQL 数据库连接')
    env = os.environ.copy()
    for key in ('PGHOST', 'PGPORT', 'PGUSER', 'PGPASSWORD', 'PGDATABASE', 'PGSERVICE', 'PGSERVICEFILE'):
        env.pop(key, None)
    env.update(PGHOST=url.host or 'localhost', PGPORT=str(url.port or 5432),
               PGUSER=url.username or '', PGPASSWORD=url.password or '', PGDATABASE=url.database or '')
    for key in ('sslmode', 'sslcert', 'sslkey', 'sslrootcert', 'connect_timeout', 'options'):
        if key in url.query:
            env['PG' + key.upper()] = str(url.query[key])
    env.setdefault('PGCONNECT_TIMEOUT', '15')
    return env


def pg_tool(name, args, *, uri, timeout=600):
    binary_dir = os.environ.get('WATCHER_POSTGRES_BIN', '')
    executable = str(Path(binary_dir) / (name + ('.exe' if os.name == 'nt' else ''))) if binary_dir else name
    result = subprocess.run([executable, *args], env=pg_environment(uri), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        # Database errors may contain SQL literals. Do not put those in web logs.
        raise RuntimeError(f'{name} 未完成（退出码 {result.returncode}），请检查目标数据库和工具版本')
    return result.stdout


def dump_postgres(engine, target):
    """Hold one exported snapshot for both pg_dump and catalogue pointers."""
    from sqlalchemy import text, inspect
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection, connection.begin():
        snapshot = connection.execute(text('SELECT pg_export_snapshot()')).scalar_one()
        paths = []
        if inspect(connection).has_table('catalog_publications'):
            paths = list(connection.execute(text('SELECT path FROM catalog_publications')).scalars())
        pg_tool('pg_dump', ['--format=custom', '--no-owner', '--no-privileges',
                            '--snapshot=' + snapshot, '--file=' + str(target)], uri=engine.url)
        return paths


def restore_postgres(dump, engine):
    from sqlalchemy import inspect
    if inspect(engine).get_table_names():
        raise ValueError('完整恢复需要空的 PostgreSQL 数据库，不能覆盖运行中的数据')
    pg_tool('pg_restore', ['--exit-on-error', '--single-transaction', '--no-owner', '--no-privileges',
                           '--dbname=' + (engine.url.database or ''), str(dump)], uri=engine.url)


def fence_restored_ai(connection):
    """Never replay in-flight paid calls after restoring or moving a database."""
    from datetime import datetime
    import sqlalchemy as sa
    names = set(sa.inspect(connection).get_table_names())
    if 'ai_executions' not in names:
        return
    metadata = sa.MetaData()
    def table(name):
        return sa.Table(name, metadata, autoload_with=connection)
    executions = table('ai_executions')
    connection.execute(executions.update().where(executions.c.status.in_(['reserved', 'sending']))
        .values(status='uncertain', error_code='restored_result_unknown', retryable=False,
                active_released=True, finished_at=datetime.utcnow()))
    if 'ai_budgets' in names:
        budgets = table('ai_budgets')
        connection.execute(budgets.update().values(active_count=0))
    if 'ai_profiles' in names:
        profiles = table('ai_profiles')
        connection.execute(profiles.update().values(enabled=False, last_test_code='restored_requires_review'))
    if 'app_config' in names:
        config = table('app_config')
        query = config.c.key == 'ai_restore_review_required'
        if connection.execute(sa.select(config.c.id).where(query)).first():
            connection.execute(config.update().where(query).values(value='1'))
        else:
            connection.execute(config.insert().values(key='ai_restore_review_required', value='1'))
    if 'announcement_summaries' in names:
        summaries = table('announcement_summaries')
        connection.execute(summaries.update().where(summaries.c.state.in_(['pending','running','generating']))
            .values(state='uncertain', error_code='restored_result_unknown',
                    error='备份恢复后需管理员核对原调用，不会自动重发'))
    if 'background_tasks' in names:
        tasks = table('background_tasks')
        connection.execute(tasks.update().where(tasks.c.kind.in_(['summary','summarize']),
            tasks.c.state.in_(['pending','running','waiting','deferred']))
            .values(state='failed', error_code='restored_result_unknown',
                    error='备份恢复后需核对付费调用', token=None, worker_id=None,
                    lease_until=None, generation=tasks.c.generation + 1))
