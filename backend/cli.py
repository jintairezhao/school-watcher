"""Flask CLI 运维命令"""
from flask import current_app


def register_cli(app):
    @app.cli.command('sync-subscriber-counts')
    def sync_subscriber_counts():
        """按 subscriptions 表重算 schools.subscriber_count（缓存漂移兜底）"""
        from backend.database.db import db
        db.session.execute(db.text(
            "UPDATE schools SET subscriber_count = COALESCE(("
            "SELECT COUNT(*) FROM subscriptions s "
            "WHERE s.school_id = schools.id), 0)"))
        db.session.commit()
        current_app.logger.info("subscriber_count 已重算")
