"""Provider-native snapshots, rotating backups, and checked restoration."""
from datetime import datetime
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
import hashlib
import gzip
import re

from flask import current_app
from backend.database.db import db
from backend.core.config import DATA_DIR


def create_backup():
    from backend.database.provider_backup import sqlite_snapshot, dump_postgres
    from filelock import FileLock
    folder = Path(current_app.config.get('BACKUP_DIR', DATA_DIR / 'backups')).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    catalog = Path(current_app.config['SOURCE_CATALOG_PATH']).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    target = folder / ('watcher-' + datetime.utcnow().strftime('%Y%m%dT%H%M%S%f') + '.zip')
    pending = target.with_suffix('.tmp')
    with FileLock(str(catalog) + '.publish.lock', timeout=60), tempfile.TemporaryDirectory(dir=folder) as scratch:
        scratch = Path(scratch)
        manifest, checksums, versions = {}, {}, []
        provider = db.engine.dialect.name
        if provider == 'sqlite':
            source = Path(db.engine.url.database).resolve()
            main = scratch / source.name
            sqlite_snapshot(source, main)
            with closing(sqlite3.connect(main)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_publications'").fetchone():
                    versions = [r[0] for r in connection.execute('SELECT path FROM catalog_publications')]
        elif provider == 'postgresql':
            main = scratch / 'database.dump'
            versions = dump_postgres(db.engine, main)
        else:
            raise RuntimeError('不支持此数据库的完整备份')
        sources = [(main, main.name, False)]
        if catalog.exists():
            sources.append((catalog, catalog.name, True))
        for relative in set(versions):
            source = (catalog.parent / relative).resolve()
            if not source.is_relative_to(catalog.parent) or not source.is_file():
                raise ValueError('当前发布目录版本缺失或路径无效，备份已停止')
            sources.append((source, str(source.relative_to(catalog.parent)).replace('\\', '/'), True))
        from backend.services.source_governance import _evidence_root
        evidence_root = _evidence_root().resolve()
        if evidence_root.is_dir():
            for evidence_file in sorted(evidence_root.glob('*.html.gz')):
                if evidence_file.is_symlink() or not re.fullmatch(r'[0-9a-f]{64}\.html\.gz', evidence_file.name):
                    raise ValueError('网页证据文件路径无效，备份已停止')
                if evidence_file.resolve().parent != evidence_root:
                    raise ValueError('网页证据文件超出配置目录')
                verify_evidence_file(evidence_file)
                relative = 'source-governance-evidence/' + evidence_file.name
                snapshot = scratch / relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(evidence_file, snapshot)
                sources.append((snapshot, relative, False))
        try:
            with zipfile.ZipFile(pending, 'w', zipfile.ZIP_DEFLATED) as archive:
                for source, name, snapshot_required in sources:
                    snapshot = scratch / name
                    if snapshot_required:
                        snapshot.parent.mkdir(parents=True, exist_ok=True)
                        sqlite_snapshot(source, snapshot)
                    archive.write(snapshot, name)
                    manifest[name] = snapshot.stat().st_size
                    with snapshot.open('rb') as reader:
                        checksums[name] = hashlib.file_digest(reader, 'sha256').hexdigest()
                archive.writestr('manifest.json', json.dumps(manifest))
                archive.writestr('backup-info.json', json.dumps(dict(version=2, provider=provider,
                    database_file=main.name, catalog_file=catalog.name, checksums=checksums)))
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)
    replica = current_app.config.get('BACKUP_COPY_DIR')
    if replica:
        destination = Path(replica).resolve()
        if destination == folder:
            raise ValueError('BACKUP_COPY_DIR must differ from BACKUP_DIR')
        destination.mkdir(parents=True, exist_ok=True)
        copy = destination / (target.name + '.tmp')
        shutil.copyfile(target, copy)
        copy.replace(destination / target.name)
        rotate(destination)
    rotate(folder)
    return {'backup': target.name, 'bytes': target.stat().st_size, 'replicated': bool(replica)}


def rotate(folder, *, clear=False):
    from backend.services.storage_policy import policy
    deleted = 0
    # Only this application's generated backups; never recurse or remove arbitrary files.
    keep = 0 if clear else policy()['backup_keep_count']
    if keep == 0 and not clear:
        return 0
    for old in sorted(folder.glob('watcher-*.zip'), reverse=True)[keep:]:
        if old.resolve().parent != folder.resolve():
            raise ValueError('Backup path escaped its directory')
        old.unlink()
        deleted += 1
    return deleted


def restore_backup(archive_path, destination, *, postgres_url=None):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Restore requires an empty destination directory')
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        info = json.loads(archive.read('backup-info.json')) if 'backup-info.json' in archive.namelist() else {'provider': 'sqlite'}
        if not manifest or not isinstance(manifest, dict) or len(manifest) > 10000:
            raise ValueError('Invalid backup manifest')
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('Duplicate backup entries')
        if not isinstance(info, dict) or info.get('provider') not in ('sqlite', 'postgresql'):
            raise ValueError('Unsupported backup provider')
        if ('database_file' in info and info['database_file'] not in manifest) or (info['provider'] == 'postgresql' and info.get('version') != 2):
            raise ValueError('Invalid database backup member')
        if 'version' in info and (info['version'] != 2 or info.get('database_file') not in manifest or
                                  set(info.get('checksums', {})) != set(manifest)):
            raise ValueError('Invalid backup metadata')
        if info['provider'] == 'postgresql' and not postgres_url:
            raise ValueError('PostgreSQL 完整备份需要指定空的目标数据库')
        for name, size in manifest.items():
            target = (destination / name).resolve()
            if ('\\' in name or ':' in name or Path(name).is_absolute() or '..' in Path(name).parts or
                    not target.is_relative_to(destination) or target == destination or
                    type(size) is not int or size < 0 or archive.getinfo(name).file_size != size):
                raise ValueError('Invalid restore path')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as reader, target.open('wb') as writer:
                shutil.copyfileobj(reader, writer)
            if target.stat().st_size != size:
                raise ValueError('Backup size mismatch')
            if info.get('checksums', {}).get(name):
                with target.open('rb') as reader:
                    if hashlib.file_digest(reader, 'sha256').hexdigest() != info['checksums'][name]:
                        raise ValueError('Backup checksum mismatch')
            if name.startswith('source-governance-evidence/'):
                if len(Path(name).parts) != 2:
                    raise ValueError('Invalid evidence path')
                verify_evidence_file(target)
            elif name != info.get('database_file') or info['provider'] == 'sqlite':
                with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as connection:
                    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Restored database failed integrity check')
        if info['provider'] == 'postgresql':
            from sqlalchemy import create_engine
            from backend.database.provider_backup import restore_postgres
            engine = create_engine(postgres_url)
            try:
                restore_postgres(destination / info['database_file'], engine)
                from backend.database.provider_backup import fence_restored_ai
                with engine.begin() as connection:
                    fence_restored_ai(connection)
            finally:
                engine.dispose()
    if info['provider'] == 'sqlite':
        from sqlalchemy import create_engine
        from backend.database.provider_backup import fence_restored_ai
        database_name = info.get('database_file')
        if not database_name:
            database_name = next((name for name in manifest if name.endswith('.db')), None)
        if database_name:
            engine = create_engine('sqlite:///' + str(destination / database_name))
            try:
                with engine.begin() as connection:
                    fence_restored_ai(connection)
            finally:
                engine.dispose()
    return list(manifest)


def expire_rollback():
    """Remove only the verified migration archive after its documented seven-day window."""
    from datetime import timezone
    folder = DATA_DIR / 'rollback'
    manifest_path = folder / 'manifest.json'
    audit_path = DATA_DIR / 'source-audits/lightweight-cutover.json'
    if not manifest_path.exists() or not audit_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    if not audit.get('business_rows_unchanged') or not audit.get('baselines_passed'):
        return False
    if datetime.now(timezone.utc) < datetime.fromisoformat(manifest['expires_at']):
        return False
    target = folder / 'source_inventory.sqlite3.gz'
    if manifest.get('file') != target.name or target.resolve().parent != folder.resolve():
        raise ValueError('Invalid rollback archive target')
    if target.exists():
        target.unlink()
    manifest_path.unlink()
    return True


def verify_evidence_file(path):
    """Bound decompression and check immutable evidence identity before accepting it."""
    if not re.fullmatch(r'[0-9a-f]{64}\.html\.gz', path.name):
        raise ValueError('Invalid evidence filename')
    with gzip.open(path, 'rb') as reader:
        body = reader.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != path.name[:64]:
        raise ValueError('Evidence content hash mismatch')
