"""Back up the configured database with its native provider before upgrading."""
from contextlib import closing
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from flask_migrate import upgrade
from alembic.script import ScriptDirectory

load_dotenv(Path(os.environ.get('WATCHER_ENV_FILE', str(ROOT / '.env'))))
from backend import create_app
from backend.core.config import get_database_uri
from backend.core.config import DATA_DIR
from backend.database.db import db
from backend.database.provider_backup import dump_postgres


def migrate():
    uri = get_database_uri()
    parsed = make_url(uri)
    if parsed.get_backend_name() == 'postgresql':
        from sqlalchemy import inspect, text
        from filelock import FileLock
        app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': uri})
        with FileLock(str(DATA_DIR / 'schema-upgrade.lock'), timeout=60), app.app_context():
            tables = inspect(db.engine).get_table_names()
            latest = ScriptDirectory(str(ROOT / 'migrations')).get_current_head()
            with db.engine.connect() as connection:
                revision = connection.execute(text('SELECT version_num FROM alembic_version')).scalar() if 'alembic_version' in tables else None
            if revision == latest:
                print('Database is already up to date.')
                return
            if tables:
                folder = DATA_DIR / 'migration-backups'
                folder.mkdir(parents=True, exist_ok=True)
                backup = folder / ('before-upgrade-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.dump')
                dump_postgres(db.engine, backup)
                print('Database backup:', backup)
            upgrade(directory=str(ROOT / 'migrations'))
        print('Database upgrade complete.')
        return
    if parsed.drivername != 'sqlite' or not parsed.database or parsed.database == ':memory:':
        raise SystemExit('Use a file-based SQLite database or PostgreSQL.')
    path = Path(parsed.database).resolve()
    if path.exists():
        with closing(sqlite3.connect(path)) as source:
            has_version = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()
            row = source.execute('SELECT version_num FROM alembic_version').fetchone() if has_version else None
            latest = ScriptDirectory(str(ROOT / 'migrations')).get_current_head()
            if row and row[0] == latest:
                print('Database is already up to date.')
                return
            backup = path.with_name(path.name + '.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            print('Database backup:', backup)
    # TESTING prevents background work and seed writes while migrations run.
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': uri})
    with app.app_context():
        upgrade(directory=str(ROOT / 'migrations'))
    print('Database upgrade complete.')


if __name__ == '__main__':
    migrate()
