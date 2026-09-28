"""Portable collected-data backups and additive, transactionally merged imports."""
from contextlib import closing
from datetime import datetime, timezone
import io
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile
import shutil

from sqlalchemy import null, text

from backend.database.db import db
from backend.database.models import (School, Department, Announcement, AnnouncementSource,
                                     DepartmentDirectoryEntry)
from backend.scraper.change_detector import compute_hash
from backend.scraper.sanitizer import sanitize_html
from backend.services.source_inventory import canonical_url

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ROWS = 200000
FORMAT = 'school-watcher-collected-data'
TABLES = {
    'schools': (School, ('id', 'name', 'url', 'enabled', 'created_at')),
    'departments': (Department, ('id', 'school_id', 'name', 'list_url', 'group_name',
        'list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')),
    'announcements': (Announcement, ('id', 'school_id', 'department_id', 'title', 'url',
        'content_html', 'content_text', 'content_hash', 'summary', 'published_at', 'created_at', 'is_updated')),
    'announcement_sources': (AnnouncementSource, ('announcement_id', 'department_id',
        'list_url', 'article_url', 'first_seen_at', 'last_seen_at')),
    'department_directory_entries': (DepartmentDirectoryEntry, ('parent_id', 'department_id', 'position')),
}


def _report(progress, phase, done=0, total=0, processed_bytes=None):
    if progress:
        progress(phase, done, total, processed_bytes)


def _rows_progress(rows, progress, phase):
    total = len(rows)
    _report(progress, phase, 0, total)
    for index, row in enumerate(rows, 1):
        yield row
        _report(progress, phase, index, total)


def export_data(progress=None):
    """Stream rows into a temporary ZIP; no accounts, secrets or personal state."""
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        db.session.rollback()
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN'))
        elif db.engine.dialect.name == 'postgresql':
            db.session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        total = sum(db.session.query(model).count() for model, _ in TABLES.values())
        done = processed = 0
        _report(progress, 'compressing', done, total, processed)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            with archive.open('data.json', 'w') as raw, io.TextIOWrapper(raw, encoding='utf-8') as writer:
                writer.write('{"format":' + json.dumps(FORMAT) + ',"version":1')
                for name, (model, columns) in TABLES.items():
                    writer.write(',' + json.dumps(name) + ':[')
                    first = True
                    for row in db.session.query(*(getattr(model, c) for c in columns)).yield_per(500):
                        if not first:
                            writer.write(',')
                        values = {c: v.isoformat() if isinstance(v, datetime) else v for c, v in zip(columns, row)}
                        if name == 'announcements':
                            from backend.services.summaries import export_summary
                            ann = db.session.get(Announcement, values['id'])
                            version = export_summary(ann)
                            values['summary_history'] = values.get('summary') or ''
                            values['summary'] = version['summary'] if version else ''
                            values['summary_version'] = version
                        encoded = json.dumps(values, ensure_ascii=False)
                        writer.write(encoded)
                        done += 1
                        processed += len(encoded.encode('utf-8'))
                        _report(progress, 'compressing', done, total, processed)
                        first = False
                    writer.write(']')
                writer.write('}')
        _report(progress, 'finalizing')
        db.session.commit()
        output.seek(0)
        return output
    except Exception:
        db.session.rollback()
        output.close()
        raise


def _legacy_data(archive, infos, progress=None, disk_backed=False, max_rows=MAX_ROWS):
    candidates = []
    try:
        return _legacy_rows(archive, infos, candidates, progress, disk_backed, max_rows)
    except Exception:
        for data in candidates:
            if hasattr(data, 'close'):
                data.close()
        raise


def _legacy_rows(archive, infos, candidates, progress, disk_backed, max_rows):
    """Read only known tables from historical watcher-*.zip SQLite snapshots."""
    if 'manifest.json' not in infos or infos['manifest.json'].file_size > 65536:
        raise ValueError('无法识别备份格式，请选择本项目导出的 ZIP 备份')
    manifest = json.loads(archive.read('manifest.json'))
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('备份清单无效')
    selected = None
    checksums = {}
    if 'backup-info.json' in infos:
        if infos['backup-info.json'].file_size > 2 * 1024 * 1024:
            raise ValueError('备份元数据过大')
        info = json.loads(archive.read('backup-info.json'))
        if not isinstance(info, dict):
            raise ValueError('备份元数据无效')
        if info.get('provider') == 'postgresql':
            raise ValueError('这是 PostgreSQL 完整恢复包；请先恢复到隔离数据库，再从存储管理导出通知数据用于合并')
        if info.get('version') != 2 or info.get('provider') != 'sqlite' or info.get('database_file') not in manifest:
            raise ValueError('备份元数据无效')
        selected = info['database_file']
        checksums = info.get('checksums', {})
        if not isinstance(checksums, dict):
            raise ValueError('备份校验清单无效')
    from backend.services.backup_stream import BackupData, DiskRows
    with tempfile.TemporaryDirectory(prefix='watcher-import-') as scratch:
        for name, size in manifest.items():
            if name not in infos or type(size) is not int or size != infos[name].file_size:
                raise ValueError('备份文件大小与清单不一致')
            if selected and name != selected:
                continue
            target = Path(scratch) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            # Names and expanded sizes have already been checked; never extractall.
            with archive.open(name) as source, target.open('wb') as destination:
                remaining = size
                _report(progress, 'reading', 0, size, 0)
                while chunk := source.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError('备份解压大小超出限制')
                    destination.write(chunk)
                    _report(progress, 'reading', size - remaining, size, size - remaining)
            if selected:
                with target.open('rb') as reader:
                    if hashlib.file_digest(reader, 'sha256').hexdigest() != checksums.get(name):
                        raise ValueError('备份内容与校验摘要不一致')
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as connection:
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('PRAGMA query_only=ON')
                connection.set_progress_handler(lambda: 1, 20000000)
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'schools', 'departments', 'announcements'} <= tables:
                    continue
                if candidates:
                    raise ValueError('备份中需要且只能包含一份通知数据库')
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('备份数据库已损坏')
                _report(progress, 'checking_database')
                data = BackupData(format=FORMAT, version=1) if disk_backed else {'format': FORMAT, 'version': 1}
                candidates.append(data)
                for table, (_, allowed) in TABLES.items():
                    data[table] = DiskRows() if disk_backed else []
                    if table not in tables:
                        continue
                    available = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
                    columns = [c for c in allowed if c in available]
                    if not columns:
                        raise ValueError('备份缺少必要的数据字段')
                    column_sql = ','.join('"' + c + '"' for c in columns)
                    rows = connection.execute(f'SELECT {column_sql} FROM "{table}" LIMIT ?', (max_rows + 1,))
                    for index, row in enumerate(rows, 1):
                        data[table].append(dict(zip(columns, row)))
                        _report(progress, 'checking_database', index)
    if len(candidates) != 1:
        raise ValueError('备份中需要且只能包含一份通知数据库')
    return candidates[0]


def read_backup(stream, progress=None, expanded_limit=None, disk_backed=False):
    limit = MAX_EXPANDED_BYTES if expanded_limit is None else expanded_limit
    max_rows = max(MAX_ROWS, limit // 1024)
    data = None
    try:
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            versioned = 'backup-info.json' in archive.namelist()
            expanded = sum(i.file_size for i in entries)
            if len(entries) > (10000 if versioned else 10):
                raise ValueError('备份包含过多文件')
            if expanded > limit:
                raise ValueError(f'备份展开后约 {expanded / 1048576:.1f} MB，超过本次 {limit / 1048576:g} MB 额度；请在导入选项中调整')
            if disk_backed and expanded * 3 > shutil.disk_usage(tempfile.gettempdir()).free:
                raise ValueError('临时目录可用空间不足，请释放空间后重试')
            infos = {}
            for info in entries:
                nested_catalog = (versioned and info.filename.startswith('catalog-generations/') and
                    info.filename.count('/') == 1 and info.filename.endswith('.sqlite3'))
                nested_evidence = bool(versioned and __import__('re').fullmatch(
                    r'source-governance-evidence/[0-9a-f]{64}\.html\.gz', info.filename))
                if (info.filename in infos or info.is_dir() or ('/' in info.filename and not (nested_catalog or nested_evidence)) or '\\' in info.filename
                        or ':' in info.filename or info.filename in ('.', '..') or info.flag_bits & 1):
                    raise ValueError('备份包含重复、加密或不安全的文件名')
                infos[info.filename] = info
            if 'data.json' in infos:
                if disk_backed:
                    from backend.services.backup_stream import read_json
                    with archive.open('data.json') as source:
                        data = read_json(source, infos['data.json'].file_size, progress, max_rows)
                else:
                    data = json.loads(archive.read('data.json'))
            else:
                data = _legacy_data(archive, infos, progress, disk_backed, max_rows)
        return validate_data(data, progress, max_rows)
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError,
            KeyError, TypeError, OverflowError, RecursionError, NotImplementedError) as exc:
        if hasattr(data, 'close'):
            data.close()
        raise ValueError('备份文件损坏或格式不支持，请重新选择完整备份') from exc
    except Exception:
        if hasattr(data, 'close'):
            data.close()
        raise


def _stamp(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('备份中的时间格式无效')
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result
    except ValueError as exc:
        raise ValueError('备份中的时间格式无效') from exc


def _ident(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('备份中的记录编号无效')
    return value


def validate_data(data, progress=None, max_rows=MAX_ROWS):
    from backend.services.backup_stream import DiskRows
    if not isinstance(data, dict) or data.get('format') != FORMAT or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('不支持此备份版本，请使用兼容版本导出')
    for table, (_, columns) in TABLES.items():
        rows = data.get(table, [] if table in ('announcement_sources', 'department_directory_entries') else None)
        if not isinstance(rows, (list, DiskRows)) or len(rows) > max_rows:
            raise ValueError('备份缺少数据表或记录数超过限制')
        data[table] = rows
        ids = set()
        for row in _rows_progress(rows, progress, 'validating_' + table):
            if not isinstance(row, dict):
                raise ValueError('备份记录格式无效')
            if 'id' in columns:
                ident = _ident(row.get('id'))
                if ident in ids:
                    raise ValueError('备份中存在重复的记录编号')
                ids.add(ident)
            for field in columns:
                value = row.get(field)
                if field.endswith('_id'):
                    _ident(value)
                elif field.endswith('_at'):
                    row[field] = _stamp(value)
                elif field in ('enabled', 'is_updated'):
                    if value is not None and value not in (True, False, 0, 1):
                        raise ValueError('备份中的开关值无效')
                elif field == 'position':
                    if type(value) is not int or not 0 <= value < 2**31:
                        raise ValueError('备份中的目录顺序无效')
                elif field != 'id':
                    if value is not None and not isinstance(value, str):
                        raise ValueError('备份中的文字字段无效')
                    limit = getattr(TABLES[table][0].__table__.columns[field].type, 'length', None) or 2 * 1024 * 1024
                    if value and len(value) > limit:
                        raise ValueError('备份中的字段内容过长')
                    if field in ('url', 'list_url', 'article_url') and value:
                        try:
                            valid = canonical_url(value)
                        except ValueError:
                            valid = ''
                        if not valid:
                            raise ValueError('备份中的原文或来源地址无效')
            if 'name' in columns and not (row.get('name') or '').strip():
                raise ValueError('备份中的学校或部门名称为空')
            if table == 'announcements' and not (row.get('title') or '').strip():
                raise ValueError('备份中的通知标题为空')
            if table == 'announcements':
                history = row.get('summary_history')
                version = row.get('summary_version')
                if history is not None and (not isinstance(history, str) or len(history) > 2 * 1024 * 1024):
                    raise ValueError('备份中的历史摘要无效')
                if version is not None and (not isinstance(version, dict) or
                        len(json.dumps(version, ensure_ascii=False)) > 512000):
                    raise ValueError('备份中的摘要版本无效')
        if isinstance(rows, DiskRows):
            rows.normalized = True
    _report(progress, 'checking_relations')
    schools = {r['id'] for r in data['schools']}
    departments = {r['id']: {'id': r['id'], 'school_id': r['school_id']} for r in data['departments']}
    announcements = set()
    for row in departments.values():
        if row['school_id'] not in schools:
            raise ValueError('备份中的部门缺少所属学校')
    for row in data['announcements']:
        announcements.add(row['id'])
        dept = departments.get(row['department_id'])
        if not dept or dept['school_id'] != row['school_id']:
            raise ValueError('备份中的通知与所属学校、部门不一致')
    for row in data['announcement_sources']:
        if row['announcement_id'] not in announcements or row['department_id'] not in departments:
            raise ValueError('备份中的通知来源不完整')
    for row in data['department_directory_entries']:
        parent, child = departments.get(row['parent_id']), departments.get(row['department_id'])
        if not parent or not child or parent['id'] == child['id'] or parent['school_id'] != child['school_id']:
            raise ValueError('备份中的部门目录关系无效')
    return data


def _url(value):
    try:
        return canonical_url(value) if value else ''
    except ValueError:
        return value or ''


def _history_key(school_id, dept_id, title, published_at, created_at, digest):
    stamp = published_at or created_at
    # Undated, unlinked history needs content evidence. Keep its stored hash even
    # when a later cache cleanup removes the body; never invent a date for it.
    return school_id, dept_id, title, stamp, digest if stamp is None else None


def _history_digest(title, body_text, body_html):
    content = json.dumps([title, body_text or '', body_html or ''], ensure_ascii=False)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def merge_data(data, progress=None):
    """Keep current values and identities; add missing records and source memberships."""
    result = dict(added=0, duplicates=0, bodies_restored=0, schools_added=0, departments_added=0,
                  sources_pending_review=0)
    now = datetime.utcnow()
    # Auth queries may have opened a transaction. Serialize SQLite writers before
    # looking up identities, so two uploads cannot insert the same missing notice.
    db.session.rollback()
    try:
        _report(progress, 'waiting_database')
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN IMMEDIATE'))
        elif db.engine.dialect.name == 'postgresql':
            # Transaction-scoped: serializes imports, and blocks collectors only
            # while the additive merge writes the shared article tables.
            db.session.execute(text('SELECT pg_advisory_xact_lock(1937202601)'))
            db.session.execute(text('LOCK TABLE school_registry_entries, schools, departments, announcements, announcement_sources, '
                                    'department_directory_entries IN SHARE ROW EXCLUSIVE MODE'))
        from backend.services.catalog import normalize_name
        from backend.services.school_registry import registry_key
        from backend.database.school_registry_models import SchoolRegistryEntry
        school_names = {}
        for school in School.query.all():
            school_names.setdefault(normalize_name(school.name), []).append(school)
        registrations = SchoolRegistryEntry.query.all()
        registry = {entry.registry_key: entry for entry in registrations}
        aliases = {}
        for entry in registrations:
            for name in [entry.canonical_name, *(entry.aliases or [])]:
                aliases.setdefault(normalize_name(name), {})[entry.registry_key] = entry
        school_map = {}
        for row in _rows_progress(data['schools'], progress, 'merging_schools'):
            name = normalize_name(row['name'])
            matches = school_names.get(name, [])
            if len(matches) > 1:
                raise ValueError('现有学校身份存在重复，请管理员先核对；导入尚未写入')
            entry = registry.get(registry_key(name))
            alias_matches = list(aliases.get(name, {}).values())
            if len(alias_matches) > 1:
                raise ValueError('学校别名对应多个身份，请管理员先核对')
            entry = entry or (alias_matches[0] if alias_matches else None)
            school = db.session.get(School, entry.school_id) if entry and entry.school_id else None
            if school and matches and matches[0].id != school.id:
                raise ValueError('学校登记身份与历史记录冲突，请管理员先核对')
            school = school or (matches[0] if matches else None)
            if school is None:
                school = School(name=row['name'], url=row.get('url') or '', enabled=bool(row.get('enabled', True)),
                                created_at=row.get('created_at') or now, subscriber_count=0)
                db.session.add(school); db.session.flush()
                school_names.setdefault(name, []).append(school)
                result['schools_added'] += 1
            if entry is None:
                entry = next((item for item in registrations if item.school_id == school.id), None)
            if entry is None:
                entry = SchoolRegistryEntry(registry_key=registry_key(name), canonical_name=name,
                    root_url=school.url or '', aliases=[row['name']], origin='import', school_id=school.id)
                db.session.add(entry); db.session.flush()
                registrations.append(entry); registry[entry.registry_key] = entry
                aliases.setdefault(name, {})[entry.registry_key] = entry
            elif entry.school_id is None:
                entry.school_id = school.id
            school_map[row['id']] = school
        dept_index = {(d.school_id, _url(d.list_url), d.name.strip()): d for d in Department.query.order_by(Department.id.desc()).all()}
        dept_map = {}
        source_reviews = []
        for row in _rows_progress(data['departments'], progress, 'merging_departments'):
            key = (school_map[row['school_id']].id, _url(row.get('list_url')), row['name'].strip())
            dept = dept_index.get(key)
            if dept is None:
                values = {c: row.get(c) for c in TABLES['departments'][1] if c not in ('id', 'school_id')}
                # Ordinary data packages are evidence of history, not authority to
                # install scraping rules. Complete DB restores use a separate path.
                for field in ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector'):
                    values[field] = ''
                dept = Department(school_id=key[0], **values)
                db.session.add(dept); db.session.flush()
                dept_index[key] = dept
                result['departments_added'] += 1
            dept_map[row['id']] = dept
            if row.get('list_selector'):
                from backend.services.source_governance import source_config, FIELDS
                candidate = {field: row.get(field) or '' for field in FIELDS}
                existing = source_config(dept)
                if any((candidate.get(field) or '') != (existing.get(field) or '') for field in FIELDS):
                    source_reviews.append((dept, candidate, existing))
        url_index = {}
        fallback = {}
        count = Announcement.query.count()
        _report(progress, 'indexing', 0, count)
        for index, ann in enumerate(Announcement.query.order_by(Announcement.id).yield_per(500), 1):
            if ann.url:
                url_index.setdefault((ann.school_id, _url(ann.url)), ann.id)
            else:
                key = _history_key(ann.school_id, ann.department_id, ann.title, ann.published_at,
                                   ann.created_at, ann.content_hash or _history_digest(ann.title, ann.content_text, ann.content_html))
                fallback.setdefault(key, ann.id)
            _report(progress, 'indexing', index, count)
        ann_map = {}
        for row in _rows_progress(data['announcements'], progress, 'merging_announcements'):
            school_id, dept_id = school_map[row['school_id']].id, dept_map[row['department_id']].id
            url = _url(row.get('url'))
            undated_history = not url and not row.get('published_at') and not row.get('created_at')
            digest = ((row.get('content_hash') or _history_digest(row['title'], row.get('content_text'), row.get('content_html')))
                      if undated_history else compute_hash(row['title'], row.get('content_text') or ''))
            key = _history_key(school_id, dept_id, row['title'], row.get('published_at'), row.get('created_at'), digest)
            ident = url_index.get((school_id, url)) if url else fallback.get(key)
            ann = db.session.get(Announcement, ident) if ident else None
            legacy_summary = row.get('summary_history') or (row.get('summary') if not row.get('summary_version') else None)
            if ann is None:
                if url:
                    from backend.services.announcement_identity import upsert_listing
                    ann, created = upsert_listing(dept_map[row['department_id']], row['title'], row['url'],
                        row.get('published_at'), created_at=row.get('created_at') or null(),
                        summary=legacy_summary, content_hash=digest, is_updated=bool(row.get('is_updated', False)))
                    url_index[(school_id, url)] = ann.id
                    result['added' if created else 'duplicates'] += 1
                else:
                    ann = Announcement(school_id=school_id, department_id=dept_id, title=row['title'],
                        url='', created_at=row.get('created_at') or null(),
                        published_at=row.get('published_at'), summary=legacy_summary,
                        content_hash=digest, is_updated=bool(row.get('is_updated', False)))
                    db.session.add(ann); db.session.flush()
                    fallback[key] = ann.id
                    result['added'] += 1
            else:
                result['duplicates'] += 1
                if not ann.summary:
                    ann.summary = legacy_summary
                if not ann.published_at:
                    ann.published_at = row.get('published_at')
            # A backup must not replace newer live content. Missing bodies can be recovered.
            if not (ann.content_html or ann.content_text) and (row.get('content_html') or row.get('content_text')):
                ann.content_html = sanitize_html(row.get('content_html') or '')
                ann.content_text = row.get('content_text') or ''
                ann.content_bytes = len(ann.content_html.encode()) + len(ann.content_text.encode())
                ann.content_cached_at = ann.content_accessed_at = now
                ann.content_error = ''
                ann.content_hash = digest if undated_history else compute_hash(ann.title, ann.content_text)
                result['bodies_restored'] += 1
            if row.get('summary_version'):
                from backend.services.summaries import import_summary
                import_summary(ann, row['summary_version'])
            ann_map[row['id']] = ann.id
        for row in _rows_progress(data['announcements'], progress, 'merging_memberships'):
            _merge_source(ann_map[row['id']], dept_map[row['department_id']].id,
                          dept_map[row['department_id']].list_url, row.get('url'), row.get('created_at'), row.get('created_at'))
        for row in _rows_progress(data['announcement_sources'], progress, 'merging_sources'):
            _merge_source(ann_map[row['announcement_id']], dept_map[row['department_id']].id,
                          row.get('list_url'), row.get('article_url'), row.get('first_seen_at'), row.get('last_seen_at'))
        for row in _rows_progress(data['department_directory_entries'], progress, 'merging_directories'):
            key = (dept_map[row['parent_id']].id, dept_map[row['department_id']].id)
            if key[0] != key[1] and db.session.get(DepartmentDirectoryEntry, key) is None:
                db.session.add(DepartmentDirectoryEntry(parent_id=key[0], department_id=key[1], position=row['position']))
        from backend.services.source_governance import propose_source
        from backend.services.tasks import enqueue
        for department, candidate, expected in _rows_progress(source_reviews, progress, 'reviewing_sources'):
            proposal = propose_source(department.school_id, candidate, department_id=department.id,
                expected_config=expected, origin='import', commit=False)
            enqueue('source_review', proposal.id, {'proposal_id': proposal.id},
                    capability='directory', replace_finished=False, commit=False)
            result['sources_pending_review'] += 1
        _report(progress, 'committing')
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


def _merge_source(ann_id, dept_id, list_url, article_url, first_seen, last_seen):
    row = db.session.get(AnnouncementSource, (ann_id, dept_id))
    first_seen = first_seen or datetime.utcnow()
    last_seen = last_seen or first_seen
    if row is None:
        db.session.add(AnnouncementSource(announcement_id=ann_id, department_id=dept_id,
            list_url=list_url or '', article_url=article_url or '', first_seen_at=first_seen, last_seen_at=last_seen))
    else:
        row.first_seen_at = min(row.first_seen_at, first_seen)
        row.last_seen_at = max(row.last_seen_at, last_seen)
        if not row.list_url:
            row.list_url = list_url or ''
        if not row.article_url:
            row.article_url = article_url or ''
