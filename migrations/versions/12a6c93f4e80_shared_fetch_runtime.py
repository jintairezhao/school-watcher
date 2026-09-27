"""Shared task ownership, browser handoff, official-host budgets and verification."""
from alembic import op
import sqlalchemy as sa

revision = '12a6c93f4e80'
down_revision = 'f05f437d20b6'
branch_labels = None
depends_on = None


def upgrade():
    columns = [
        sa.Column('capability', sa.String(16), nullable=False, server_default='http'),
        sa.Column('phase', sa.String(32), nullable=False, server_default='fetch'),
        sa.Column('generation', sa.Integer, nullable=False, server_default='0'),
        sa.Column('policy_version', sa.String(64), nullable=False, server_default='1'),
        sa.Column('checkpoint', sa.JSON, nullable=False, server_default='{}'),
        sa.Column('claim_count', sa.Integer, nullable=False, server_default='0'),
        sa.Column('worker_id', sa.String(100)),
        sa.Column('error_code', sa.String(64), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime), sa.Column('queued_at', sa.DateTime),
        sa.Column('checked_at', sa.DateTime), sa.Column('next_run_at', sa.DateTime),
        sa.Column('deadline_at', sa.DateTime),
    ]
    for column in columns:
        op.add_column('background_tasks', column)
    op.execute("UPDATE background_tasks SET created_at = updated_at, queued_at = updated_at, "
               "checked_at = CASE WHEN state IN ('done', 'failed') THEN COALESCE(finished_at, updated_at) ELSE NULL END, "
               "claim_count = attempts, attempts = CASE WHEN state = 'failed' THEN attempts ELSE 0 END")
    op.execute("UPDATE background_tasks SET capability = 'directory' WHERE kind IN ('directory', 'discover')")
    with op.batch_alter_table('background_tasks') as batch:
        batch.alter_column('created_at', existing_type=sa.DateTime, nullable=False)
        batch.alter_column('queued_at', existing_type=sa.DateTime, nullable=False)
        batch.create_index('ix_tasks_capability_claim', ['capability', 'state', 'available_at'])
    op.create_table('runtime_leases',
        sa.Column('key', sa.String(240), primary_key=True), sa.Column('token', sa.String(64), nullable=False),
        sa.Column('owner_id', sa.String(100), nullable=False), sa.Column('expires_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False))
    op.create_table('worker_heartbeats', sa.Column('worker_id', sa.String(100), primary_key=True),
        sa.Column('roles', sa.JSON, nullable=False), sa.Column('started_at', sa.DateTime, nullable=False),
        sa.Column('heartbeat_at', sa.DateTime, nullable=False), sa.Column('stopped_at', sa.DateTime))
    op.create_table('origin_budgets', sa.Column('origin', sa.String(512), primary_key=True),
        sa.Column('next_start_at', sa.DateTime, nullable=False), sa.Column('cooldown_until', sa.DateTime))
    op.create_table('origin_permits', sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('origin', sa.String(512), nullable=False), sa.Column('owner_id', sa.String(100), nullable=False),
        sa.Column('expires_at', sa.DateTime, nullable=False))
    op.create_index('ix_origin_permits_origin', 'origin_permits', ['origin'])
    op.create_table('verification_sessions', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('source_id', sa.String(128), nullable=False), sa.Column('origin', sa.String(512), nullable=False),
        sa.Column('url', sa.Text, nullable=False), sa.Column('status', sa.String(32), nullable=False),
        sa.Column('runtime_id', sa.String(64)), sa.Column('generation', sa.String(64)),
        sa.Column('task_id', sa.Integer, sa.ForeignKey('background_tasks.id', ondelete='SET NULL')),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime, nullable=False), sa.Column('expires_at', sa.DateTime),
        sa.Column('completed_at', sa.DateTime), sa.Column('error_code', sa.String(64), nullable=False, server_default=''),
        sa.Column('error_message', sa.Text, nullable=False, server_default=''),
        sa.Column('request_payload', sa.JSON))
    op.create_index('ix_verification_sessions_source_id', 'verification_sessions', ['source_id'])
    op.create_index('ix_verification_sessions_origin', 'verification_sessions', ['origin'])
    op.create_table('verification_waiters',
        sa.Column('task_id', sa.Integer, sa.ForeignKey('background_tasks.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('session_id', sa.String(64), sa.ForeignKey('verification_sessions.id', ondelete='CASCADE'), nullable=False))
    op.create_index('ix_verification_waiters_session_id', 'verification_waiters', ['session_id'])
    op.create_table('catalog_publications', sa.Column('site_key', sa.String(128), primary_key=True),
        sa.Column('generation', sa.String(64), nullable=False), sa.Column('path', sa.Text, nullable=False),
        sa.Column('activated_at', sa.DateTime, nullable=False),
        sa.Column('policy_version', sa.String(64), nullable=False, server_default='1'))
    with op.batch_alter_table('scrape_logs') as batch:
        batch.add_column(sa.Column('task_id', sa.Integer))
        batch.add_column(sa.Column('task_generation', sa.Integer))
        batch.add_column(sa.Column('task_token', sa.String(32)))
        batch.create_foreign_key('fk_scrape_logs_task', 'background_tasks', ['task_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('scrape_logs') as batch:
        batch.drop_constraint('fk_scrape_logs_task', type_='foreignkey')
        batch.drop_column('task_generation')
        batch.drop_column('task_token')
        batch.drop_column('task_id')
    for table in ('catalog_publications', 'verification_waiters', 'verification_sessions', 'origin_permits',
                  'origin_budgets', 'worker_heartbeats', 'runtime_leases'):
        op.drop_table(table)
    with op.batch_alter_table('background_tasks') as batch:
        batch.drop_index('ix_tasks_capability_claim')
        for column in ('capability', 'phase', 'generation', 'policy_version', 'checkpoint', 'claim_count',
                       'worker_id', 'error_code', 'created_at', 'queued_at', 'checked_at', 'next_run_at', 'deadline_at'):
            batch.drop_column(column)
