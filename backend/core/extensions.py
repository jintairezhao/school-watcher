"""Flask 扩展单例：SQLAlchemy 与 Flask-Migrate"""
from sqlalchemy import event
from sqlalchemy.engine import Engine
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite 并发优化。

    应用使用多线程访问 SQLite（请求线程 + 后台爬取线程 + 定时任务线程），
    默认的 rollback-journal 模式在并发写时容易触发 "database is locked"。
    切换为 WAL 模式并设置 busy_timeout，让写操作在锁冲突时等待而非立即报错。
    """
    import sqlite3
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")  # user_reads 等 ondelete CASCADE 生效
        cursor.close()
    except Exception:
        # 非 SQLite 连接或 PRAGMA 不支持时静默跳过
        pass
