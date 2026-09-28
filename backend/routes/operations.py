"""Minimal health probes and authenticated job/storage inspection."""
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, current_app, render_template, request, send_file, g, abort
from filelock import Timeout
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge
from sqlalchemy import text
from backend.auth import admin_required
from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask, Announcement, UserAnnouncementState

bp = Blueprint('operations', __name__)


@bp.before_app_request
def limit_backup_upload():
    if request.path == '/api/storage/import':
        from backend.services.data_transfer import MAX_UPLOAD_BYTES
        request.max_content_length = MAX_UPLOAD_BYTES
        if request.args.get('progress') == '1':
            import shutil
            import tempfile
            try:
                upload_limit = _import_megabytes('upload_mb', 100)
                expanded_limit = _import_megabytes('expanded_mb', 1024)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            # The authenticated UI declares the selected file size; unpacking is
            # separately bounded by the user's explicit processing allowance.
            request.max_content_length = min(upload_limit, expanded_limit + 2 * 1048576,
                                             shutil.disk_usage(tempfile.gettempdir()).free // 2)


def _import_megabytes(key, default):
    raw = request.args.get(key, str(default))
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10 or not 1 <= int(raw) <= 2**31 - 1:
        raise ValueError('导入额度请输入正整数（MB）')
    return int(raw) * 1048576


@bp.app_errorhandler(RequestEntityTooLarge)
def upload_too_large(error):
    return jsonify(error='文件超过本次导入额度或临时目录可用空间，请调整导入选项后重试'), 413


@bp.get('/health/live')
def live():
    return jsonify(status='ok')


@bp.get('/health/ready')
def ready():
    try:
        db.session.execute(text('SELECT 1'))
        from backend.services.runtime_leases import runtime_status
        runtime = runtime_status()
        healthy = runtime['ready']
        return jsonify(status='ok' if healthy else 'worker_unavailable', runtime=runtime), 200 if healthy else 503
    except Exception:
        db.session.rollback()
        return jsonify(status='unavailable'), 503


@bp.get('/api/tasks/<int:task_id>')
@admin_required
def task(task_id):
    row = db.get_or_404(BackgroundTask, task_id)
    return jsonify(state=row.state, phase=row.phase, capability=row.capability, result=row.result,
                   error=row.error, error_code=row.error_code)


@bp.get('/api/admin/runtime/metrics')
@admin_required
def runtime_metrics():
    from sqlalchemy import select, func
    from backend.services.runtime_leases import runtime_status
    counts = db.session.execute(select(BackgroundTask.state, BackgroundTask.capability, func.count())
        .group_by(BackgroundTask.state, BackgroundTask.capability)).all()
    oldest = db.session.execute(select(func.min(BackgroundTask.queued_at)).where(
        BackgroundTask.state == 'pending', BackgroundTask.available_at <= datetime.utcnow())).scalar()
    return jsonify(database=db.engine.dialect.name, runtime=runtime_status(),
        tasks=[{'state': state, 'capability': lane, 'count': count} for state, lane, count in counts],
        oldest_pending_seconds=max(0, (datetime.utcnow() - oldest).total_seconds()) if oldest else 0)


@bp.get('/api/storage')
@admin_required
def storage():
    from backend.core.config import DATA_DIR
    root = Path(current_app.config.get('STORAGE_ROOT', DATA_DIR))
    from backend.services.source_governance import _evidence_root
    governance_root = _evidence_root().resolve()
    files = [{'name': str(p.relative_to(root)), 'bytes': p.stat().st_size,
              'kind': 'governance_evidence' if p.resolve().is_relative_to(governance_root) else ''}
             for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
    # Relocated files still count toward usage, without counting nested paths twice.
    known = {p.resolve() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    discovery = Path(current_app.config['DISCOVERY_CACHE_PATH']).resolve()
    fetch_root = Path(current_app.config['FETCH_EVIDENCE_DIR']).resolve()
    backup_root = Path(current_app.config['BACKUP_DIR']).resolve()
    extra = [*discovery.parent.glob(discovery.name + '*')]
    for folder in (fetch_root, backup_root):
        if folder.is_dir(): extra.extend(folder.rglob('*'))
    for path in extra:
        if path.is_file() and not path.is_symlink() and path.resolve() not in known:
            prefix = 'backups/' if path.resolve().is_relative_to(backup_root) else 'fetch-evidence/' if path.resolve().is_relative_to(fetch_root) else ''
            files.append({'name': prefix + path.name, 'bytes': path.stat().st_size, 'kind': ''})
            known.add(path.resolve())
    for file in files:
        actual = (root / file['name']).resolve()
        if actual.is_relative_to(backup_root): file['name'] = 'backups/' + actual.name
        elif actual.is_relative_to(fetch_root): file['name'] = 'fetch-evidence/' + actual.name
        elif actual.parent == discovery.parent and actual.name.startswith(discovery.name): file['name'] = 'discovery_cache.' + actual.name
    governance_bytes = sum(p.stat().st_size for p in governance_root.glob('*.html.gz')
                           if p.is_file() and not p.is_symlink()) if governance_root.exists() else 0
    outside_governance_bytes = governance_bytes if not governance_root.is_relative_to(root.resolve()) else 0
    pinned = db.session.query(UserAnnouncementState.announcement_id).filter_by(starred=True)
    saved_bytes = db.session.query(db.func.coalesce(db.func.sum(Announcement.content_bytes), 0)).filter(
        Announcement.id.in_(pinned)).scalar()
    from backend.services.storage_policy import policy
    from backend.services.task_fetch import evidence_root
    evidence = evidence_root()
    evidence_bytes = sum(p.stat().st_size for p in evidence.glob('*') if p.is_file() and not p.is_symlink()) if evidence.exists() else 0
    database_bytes = None
    if db.engine.dialect.name == 'postgresql':
        database_bytes = db.session.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
    return jsonify(total_bytes=sum(p['bytes'] for p in files) + (database_bytes or 0) + outside_governance_bytes, saved_body_bytes=saved_bytes,
                   policy=policy(), announcement_count=Announcement.query.count(),
                   body_cache_limit=policy()['body_cache_mb'] * 1048576,
                   discovery_cache_limit=policy()['discovery_cache_mb'] * 1048576, files=files,
                   fetch_evidence_bytes=evidence_bytes, source_governance_evidence_bytes=governance_bytes, database_bytes=database_bytes)


@bp.get('/admin/storage')
@admin_required
def storage_page():
    data = storage().get_json()
    groups = {'运行目录与通知': data.get('database_bytes') or 0, '调查缓存': 0,
              '网页抓取缓存': 0, '栏目核实证据': data.get('source_governance_evidence_bytes') or 0,
              '备份与回退资料': 0, '日志及其他文件': 0}
    for file in data['files']:
        if file.get('kind') == 'governance_evidence':
            continue
        name = file['name'].replace('\\', '/')
        if 'backup' in name or '.bak' in name or name.startswith('rollback/'):
            group = '备份与回退资料'
        elif name.startswith(('fetch-evidence/', 'fetch_evidence/')):
            group = '网页抓取缓存'
        elif name.startswith('discovery_cache.'):
            group = '调查缓存'
        elif name.startswith(('school_watcher.db', 'source_catalog.sqlite3', 'catalog-generations/')):
            group = '运行目录与通知'
        else:
            group = '日志及其他文件'
        groups[group] += file['bytes']
    from backend.services.scrape_logs import RETENTION_CHOICES
    return render_template('storage.html', storage=data, groups=groups, log_retention_choices=RETENTION_CHOICES)


@bp.put('/api/storage/policy')
@admin_required
def update_storage_policy():
    from backend.services.storage_policy import save_policy
    try:
        return jsonify(policy=save_policy(request.get_json(silent=True)))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@bp.post('/api/storage/cleanup')
@admin_required
def cleanup_storage():
    from backend.services.storage_policy import cleanup
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('mode') not in ('expired', 'all_cache', 'body', 'discovery', 'fetch', 'governance', 'backups', 'logs'):
        return jsonify(error='请选择按已保存规则清理或清空可清理缓存'), 400
    try:
        category = data['mode'] if data['mode'] not in ('expired', 'all_cache') else None
        return jsonify(cleanup(all_cache=data['mode'] == 'all_cache', category=category))
    except Timeout:
        return jsonify(error='正在检查学校来源，请完成后再清理；本次未执行清理'), 409
    except (SQLAlchemyError, OSError, RuntimeError):
        db.session.rollback()
        current_app.logger.exception('Storage cleanup failed')
        return jsonify(error='清理未全部完成，请稍后重试；通知目录和收藏仍保留'), 503


@bp.post('/api/storage/export')
@admin_required
def export_storage():
    from backend.services.data_transfer import export_data
    if request.args.get('progress') == '1':
        from backend.services.transfer_progress import streamed, export_with_progress
        save = bool(current_app.config.get('DESKTOP_MODE') and request.args.get('save') == '1')
        user_id = g.user.id
        return streamed(lambda progress: export_with_progress(progress, save, user_id))
    try:
        archive = export_data()
        if current_app.config.get('DESKTOP_MODE') and request.args.get('save') == '1':
            import shutil
            folder = Path(current_app.config['BACKUP_DIR'])
            folder.mkdir(parents=True, exist_ok=True)
            name = 'school-watcher-data-' + datetime.utcnow().strftime('%Y%m%dT%H%M%S%f') + '.zip'
            try:
                with (folder / name).open('xb') as output:
                    shutil.copyfileobj(archive, output)
            finally:
                archive.close()
            return jsonify(saved=True, name=name)
        response = send_file(archive, mimetype='application/zip', as_attachment=True,
            download_name='school-watcher-data-' + datetime.utcnow().strftime('%Y%m%dT%H%M%SZ') + '.zip')
        response.headers['Cache-Control'] = 'no-store'
        response.call_on_close(archive.close)
        return response
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        current_app.logger.exception('Data export failed')
        return jsonify(error='备份导出失败，请稍后重试'), 503


@bp.post('/api/storage/import')
@admin_required
def import_storage():
    from backend.services.data_transfer import read_backup, merge_data
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify(error='请先选择 ZIP 备份文件'), 400
    if request.args.get('progress') == '1':
        from backend.services.transfer_progress import streamed
        expanded_limit = _import_megabytes('expanded_mb', 1024)
        # Detach the upload from Flask's request lifetime, so a disconnected
        # browser cannot close the stream while the worker is reading it.
        import os
        # fileno rolls a small SpooledTemporaryFile to disk if needed. A duplicated
        # descriptor keeps the upload alive without copying a multi-GB file again.
        source = os.fdopen(os.dup(upload.stream.fileno()), 'rb')
        def operation(progress):
            source.seek(0)
            data = read_backup(source, progress, expanded_limit, disk_backed=True)
            try:
                return merge_data(data, progress)
            finally:
                if hasattr(data, 'close'):
                    data.close()
        return streamed(operation, cleanup=source.close)
    try:
        data = read_backup(upload.stream)
        return jsonify(merge_data(data))
    except ValueError as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        current_app.logger.exception('Data import failed')
        return jsonify(error='导入未完成，数据库改动已撤销，请稍后重试'), 503


@bp.get('/api/storage/export/download/<token>')
@admin_required
def download_export(token):
    import re
    from itsdangerous import BadSignature
    from backend.services.transfer_progress import signer, download_folder
    try:
        payload = signer().loads(token, max_age=3600)
    except BadSignature:
        abort(404)
    if (not isinstance(payload, dict) or payload.get('user') != g.user.id or
            not re.fullmatch(r'[0-9a-f]{32}', str(payload.get('id', '')))):
        abort(404)
    path = download_folder() / (payload['id'] + '.zip')
    if not path.is_file() or path.is_symlink():
        abort(404)
    response = send_file(path, mimetype='application/zip', as_attachment=True, download_name=payload['name'])
    response.headers['Cache-Control'] = 'no-store'
    response.direct_passthrough = False
    response.call_on_close(lambda: path.unlink(missing_ok=True))
    return response
