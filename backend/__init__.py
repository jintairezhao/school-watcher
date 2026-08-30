"""学校通知扒取工具 — 应用工厂"""
import logging
from datetime import timedelta

from flask import Flask, render_template

from backend.core import ROOT_DIR, DATA_DIR, ensure_secret_key, load_config_yaml, get_database_uri
from backend.core import db, migrate

logger = logging.getLogger(__name__)


def create_app():
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
    # SQLite 多线程访问：禁用同线程校验，并加长等待锁的时间，避免后台线程写库时报错
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'check_same_thread': False, 'timeout': 30},
    }

    # ---- 扩展 ----
    db.init_app(app)
    migrate.init_app(app, db)

    # ---- 模型（确保表结构注册） ----
    from backend.database import models  # noqa: F401

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

    # ---- 错误处理 ----
    @app.errorhandler(404)
    def not_found(e):
        return render_template('base.html', content='<div class="empty-state"><h2>404</h2><p>页面未找到</p></div>'), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template('base.html', content='<div class="empty-state"><h2>500</h2><p>服务器内部错误</p></div>'), 500

    # ---- 从 YAML 导入学校配置（表不存在时静默跳过，迁移生成阶段会触发） ----
    try:
        with app.app_context():
            load_config_yaml()
    except Exception as e:
        logger.warning(f"YAML 种子导入跳过: {e}")

    # ---- 清理上次进程中断遗留的 running 抓取记录（避免成功率失真） ----
    try:
        with app.app_context():
            from backend.database.models import ScrapeLog
            stuck = ScrapeLog.query.filter_by(status='running').update(
                {'status': 'failed', 'error_message': '进程中断（启动时清理）'})
            if stuck:
                db.session.commit()
                logger.info(f"已清理 {stuck} 条中断的抓取记录")
    except Exception:
        pass

    return app
