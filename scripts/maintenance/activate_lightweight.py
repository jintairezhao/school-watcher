"""Offline checked cutover; preserve business rows and archive investigation evidence.

Stop the website and worker before running. No purchase, remote write, or deployment.
"""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from backend.core.config import DATA_DIR
from filelock import FileLock


def business_fingerprint(path, columns=None):
    with closing(sqlite3.connect(path)) as c:
        if columns is None:
            tables = ('schools', 'departments', 'announcements', 'announcement_sources', 'users',
                      'subscriptions', 'user_reads', 'user_announcement_states')
            columns = {table: [r[1] for r in c.execute(f'PRAGMA table_info({table})')] for table in tables}
        result = {}
        for table, names in columns.items():
            digest = hashlib.sha256()
            select = ','.join('"' + name + '"' for name in names)
            count = 0
            for row in c.execute(f'SELECT {select} FROM "{table}" ORDER BY 1,2'):
                digest.update(json.dumps(row, ensure_ascii=False, default=str).encode())
                count += 1
            result[table] = {'count': count, 'sha256': digest.hexdigest()}
        return columns, result


def archive_legacy(path):
    from backend.services.source_inventory import Inventory
    inventory = Inventory.__new__(Inventory); inventory.path = path
    archive_dir = DATA_DIR / 'rollback'
    archive_dir.mkdir(exist_ok=True)
    target = archive_dir / 'source_inventory.sqlite3.gz'
    if target.exists():
        raise ValueError('An existing rollback archive must be reviewed before replacing it')
    with ExitStack() as locks:
        for site in inventory.all_sites():
            locks.enter_context(FileLock(str(path.parent / ('source-crawl-' + site['site_key'] + '.lock')), timeout=0))
        with closing(sqlite3.connect(path)) as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Legacy database integrity check failed')
        original = hashlib.sha256()
        pending = target.with_suffix('.tmp')
        with path.open('rb') as source, gzip.open(pending, 'wb', compresslevel=6) as writer:
            while block := source.read(4 * 1024 * 1024):
                original.update(block); writer.write(block)
        recovered = hashlib.sha256()
        with gzip.open(pending, 'rb') as reader:
            while block := reader.read(4 * 1024 * 1024):
                recovered.update(block)
        if recovered.digest() != original.digest():
            raise RuntimeError('Rollback archive did not reproduce the original database')
        pending.replace(target)
        manifest = {'file': target.name, 'sha256': original.hexdigest(), 'original_bytes': path.stat().st_size,
                    'archive_bytes': target.stat().st_size,
                    'expires_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
        (archive_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        # Validate the exact absolute target immediately before removing this one file.
        if path.resolve() != (DATA_DIR / 'source_inventory.sqlite3').resolve():
            raise ValueError('Legacy removal target differs from the configured data directory')
        path.unlink()
        return manifest


def activate(archive=False):
    from backend import create_app
    from backend.database.db import db
    from backend.services.backups import create_backup
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services.source_baselines import BASELINE_DIRECTORY, check_baseline
    from flask_migrate import upgrade
    main = DATA_DIR / 'school_watcher.db'
    prepared = DATA_DIR / 'source_catalog.build.sqlite3'
    published = DATA_DIR / 'source_catalog.sqlite3'
    with FileLock(str(DATA_DIR / 'worker.lock'), timeout=0):
        if not prepared.exists():
            raise ValueError('Build and validate source_catalog.build.sqlite3 first')
        catalog = RuntimeCatalog(prepared)
        checks = [check_baseline(catalog, json.loads(p.read_text(encoding='utf-8')))
                  for p in BASELINE_DIRECTORY.glob('*.json')]
        if not checks or not all(c['scope_passed'] for c in checks):
            raise RuntimeError('Prepared source catalogue did not pass all baselines')
        columns, before = business_fingerprint(main)
        app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(main)})
        with app.app_context():
            backup = create_backup()
            upgrade(directory=str(ROOT / 'migrations'))
            db.session.remove(); db.engine.dispose()
        _, after = business_fingerprint(main, columns)
        if before != after:
            raise RuntimeError('Business rows changed; restore the pre-migration backup before restarting')
        with closing(sqlite3.connect(main)) as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('VACUUM')
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Business database integrity check failed')
        if prepared.resolve().parent != DATA_DIR.resolve() or published.resolve().parent != DATA_DIR.resolve():
            raise ValueError('Catalogue cutover path escaped the configured directory')
        prepared.replace(published)
        report = {'business_rows_unchanged': before == after, 'business_tables': before,
                  'baseline_count': len(checks), 'baselines_passed': True, 'backup': backup,
                  'business_bytes': main.stat().st_size, 'catalogue_bytes': published.stat().st_size}
        audit = DATA_DIR / 'source-audits/lightweight-cutover.json'
        audit.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if archive:
            report['legacy_archive'] = archive_legacy(DATA_DIR / 'source_inventory.sqlite3')
        report['runtime_database_bytes'] = main.stat().st_size + published.stat().st_size
        report['all_data_files_bytes'] = sum(p.stat().st_size for p in DATA_DIR.rglob('*') if p.is_file())
        audit = DATA_DIR / 'source-audits/lightweight-cutover.json'
        audit.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-legacy', action='store_true')
    args = parser.parse_args()
    activate(args.archive_legacy)
