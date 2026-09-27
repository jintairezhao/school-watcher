"""Flask CLI 运维命令"""
from flask import current_app
import click


def register_cli(app):
    @app.cli.command('promote-user')
    @click.argument('username')
    def promote_user(username):
        """Grant administrator access to an existing registered account, locally only."""
        from backend.database.models import User
        from backend.database.db import db
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException('User not found. Register an account first.')
        user.role = 'admin'
        db.session.commit()
        click.echo('Administrator access granted to ' + username)

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

    @app.cli.command('onboarding-scope')
    @click.argument('output', type=click.Path(dir_okay=False))
    def onboarding_scope(output):
        """Freeze the full built-in and registered-school denominator; no crawl."""
        import json
        from pathlib import Path
        from backend.services.onboarding_acceptance import freeze_scope
        snapshot = freeze_scope()
        with Path(output).open('x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        click.echo(f"Recorded {len(snapshot['schools'])} schools. No sources marked ready.")

    @app.cli.command('onboarding-report')
    @click.argument('scope', type=click.Path(exists=True, dir_okay=False))
    @click.argument('output', type=click.Path(dir_okay=False))
    def onboarding_report(scope, output):
        """Report every frozen school, including missing and unsubscribed entries."""
        import json
        from pathlib import Path
        from backend.services.onboarding_acceptance import acceptance_report
        report = acceptance_report(json.loads(Path(scope).read_text(encoding='utf-8')))
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        click.echo(f"Verified {report['ready_schools']} / {report['total_schools']} schools.")

    @app.cli.command('onboarding-enqueue')
    @click.argument('scope', type=click.Path(exists=True, dir_okay=False))
    def onboarding_enqueue(scope):
        """Explicitly queue the frozen school directory scope (may call configured AI)."""
        import json
        from pathlib import Path
        from backend.services.onboarding_acceptance import enqueue_scope
        rows = enqueue_scope(json.loads(Path(scope).read_text(encoding='utf-8')))
        click.echo(f"Queued/reused {len(rows)} shared directory tasks.")
