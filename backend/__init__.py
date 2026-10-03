"""学校通知扒取工具 — 应用工厂"""
import logging
import os
from datetime import timedelta

from flask import Flask, render_template, request, jsonify

from backend.core import ROOT_DIR, DATA_DIR, ensure_secret_key, load_config_yaml, get_database_uri
from backend.core import db, migrate

logger = logging.getLogger(__name__)


def create_app(test_config=None):
    """创建并配置 Flask 应用实例"""
    app = Flask(
        __name__,
        template_folder=str(ROOT_DIR / 'frontend' / 'templates'),
        static_folder=str(ROOT_DIR / 'frontend' / 'static'),
    )

    # ---- 应用配置 ----
    app.config['SECRET_KEY'] = ensure_secret_key()
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = get_database_uri()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JSON_AS_ASCII'] = False
    app.config.update(
        SESSION_COOKIE_SECURE=os.environ.get('WATCHER_ENV') == 'production',
        SOURCE_CATALOG_PATH=str(DATA_DIR / 'source_catalog.sqlite3'),
        DISCOVERY_CACHE_PATH=os.environ.get('WATCHER_DISCOVERY_CACHE_PATH', str(DATA_DIR / 'discovery_cache.sqlite3')),
        BODY_CACHE_BYTES=150 * 1024 * 1024,
        BODY_CACHE_DAYS=30,
        BACKUP_DIR=os.environ.get('WATCHER_BACKUP_DIR', str(DATA_DIR / 'backups')),
        BACKUP_COPY_DIR=os.environ.get('WATCHER_BACKUP_COPY_DIR', ''),
        BROWSER_SERVICE_URL=os.environ.get('WATCHER_BROWSER_URL', 'http://127.0.0.1:8765'),
        BROWSER_SERVICE_TOKEN=os.environ.get('WATCHER_BROWSER_TOKEN', ''),
        FETCH_EVIDENCE_DIR=os.environ.get('WATCHER_FETCH_EVIDENCE_DIR', str(DATA_DIR / 'fetch-evidence')),
        BROWSER_ENABLED=os.environ.get('WATCHER_BROWSER', '0') == '1',
        DESKTOP_MODE=os.environ.get('WATCHER_DESKTOP') == '1',
        DESKTOP_FRAMELESS=os.environ.get('WATCHER_DESKTOP') == '1' and os.name == 'nt',
        DESKTOP_TOKEN=os.environ.get('WATCHER_DESKTOP_TOKEN', ''),
        DESKTOP_ORIGIN=os.environ.get('WATCHER_PUBLIC_ORIGIN', 'http://localhost'),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config['TESTING'] and not app.config['DESKTOP_MODE']:
        raise RuntimeError('School Watcher 0.2.0 uses the desktop runtime. Start with python app.py.')
    from pathlib import Path
    app.config.setdefault('SOURCE_GOVERNANCE_EVIDENCE_PATH', os.environ.get('WATCHER_SOURCE_EVIDENCE_DIR') or
        str(Path(app.config.get('SOURCE_INVENTORY_PATH') or app.config['SOURCE_CATALOG_PATH']).parent / 'source-governance-evidence'))
    if not app.config.get('TESTING'):
        from backend.core.config import ensure_field_encryption_key
        ensure_field_encryption_key()
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:'):
        app.config.setdefault('SQLALCHEMY_ENGINE_OPTIONS', {
            'connect_args': {'check_same_thread': False, 'timeout': 30}})
    else:
        app.config.setdefault('SQLALCHEMY_ENGINE_OPTIONS', {
            'pool_size': int(os.environ.get('WATCHER_DB_POOL_SIZE', '4')),
            'max_overflow': int(os.environ.get('WATCHER_DB_POOL_OVERFLOW', '4')),
            'pool_timeout': 15, 'pool_pre_ping': True, 'pool_recycle': 1800,
            'connect_args': {'options': '-c timezone=UTC'}})
    if os.environ.get('WATCHER_TRUST_PROXY') == '1':
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ---- 扩展 ----
    db.init_app(app)
    migrate.init_app(app, db)

    # ---- 模型（确保表结构注册） ----
    from backend.database import models  # noqa: F401
    from backend.database import school_registry_models, source_governance_models  # noqa: F401
    from backend.database import student_information_models  # noqa: F401
    from backend.ai import models as ai_models, summary_models  # noqa: F401
    from backend.services import announcement_identity  # noqa: F401

    # ---- 蓝图 ----
    from backend.routes import register_blueprints
    register_blueprints(app)

    # ---- CLI 运维命令 ----
    from backend.cli import register_cli
    register_cli(app)

    # ---- 钩子 ----
    from backend.auth import register_hooks
    from backend.auth import register_security_headers
    register_hooks(app)
    register_security_headers(app)

    from backend.services.source_labels import source_breadcrumb
    app.jinja_env.filters['source_breadcrumb'] = source_breadcrumb
    from backend.services.channel_views import channel_breadcrumb, publication_links
    app.jinja_env.filters['channel_breadcrumb'] = channel_breadcrumb
    app.jinja_env.globals['publication_links'] = publication_links
    from backend.services.article_images import render_article_html
    app.jinja_env.filters['article_html'] = render_article_html
    from backend.services.student_information import article_view, source_view
    app.jinja_env.globals['student_article_view'] = article_view
    app.jinja_env.globals['student_source_view'] = source_view

    # ---- 错误处理 ----
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/api/'):
            return jsonify(error='请求的内容不存在。'), 404
        return render_template('error.html', error_code=404), 404

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if request.path.startswith('/api/'):
            return jsonify(error='操作未完成，请稍后重试。'), 500
        return render_template('error.html', error_code=500), 500

    # ---- 从 YAML 导入学校配置（表不存在时静默跳过，迁移生成阶段会触发） ----
    if not app.config.get('TESTING') and os.environ.get('WATCHER_SEED_ON_START', '1') == '1':
        try:
            with app.app_context():
                load_config_yaml()
        except Exception as e:
            logger.warning(f"YAML 种子导入跳过: {e}")

    return app
