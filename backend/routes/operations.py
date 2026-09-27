"""Minimal health probes and authenticated job/storage inspection."""
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, current_app, render_template, request, send_file
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


@bp.app_errorhandler(RequestEntityTooLarge)
def upload_too_large(error):
    return jsonify(error='备份文件过大，上传请求不可超过 100 MB'), 413


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
                   body_cache_limit=current_app.config['BODY_CACHE_BYTES'],
                   discovery_cache_limit=100 * 1024 * 1024, files=files,
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
    if not isinstance(data, dict) or data.get('mode') not in ('expired', 'all_cache'):
        return jsonify(error='请选择按已保存规则清理或清空可清理缓存'), 400
    try:
        return jsonify(cleanup(all_cache=data['mode'] == 'all_cache'))
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
    try:
        archive = export_data()
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
