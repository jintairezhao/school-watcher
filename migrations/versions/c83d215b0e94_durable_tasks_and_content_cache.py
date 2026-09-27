"""Durable worker jobs and bounded article content cache.

Revision ID: c83d215b0e94
Revises: b72f104a9d83
"""
from alembic import op
import sqlalchemy as sa

revision = 'c83d215b0e94'
down_revision = 'b72f104a9d83'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('background_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('identity', sa.String(240), nullable=False, unique=True),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('available_at', sa.DateTime(), nullable=False),
        sa.Column('lease_until', sa.DateTime()),
        sa.Column('token', sa.String(32)),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime()))
    op.create_index('ix_tasks_claim', 'background_tasks', ['state', 'available_at'])
    with op.batch_alter_table('announcements') as batch:
        batch.add_column(sa.Column('content_cached_at', sa.DateTime()))
        batch.add_column(sa.Column('content_accessed_at', sa.DateTime()))
        batch.add_column(sa.Column('content_bytes', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('content_error', sa.String(300), nullable=False, server_default=''))
        batch.create_index('ix_content_accessed', ['content_accessed_at'])
    # Existing bodies remain readable and are counted before applying any retention policy.
    connection = op.get_bind()
    rows = connection.execute(sa.text('SELECT id,content_html,content_text,title FROM announcements'))
    import datetime
    now = datetime.datetime.utcnow()
    for row in rows:
        html, text = row[1] or '', row[2] or ''
        if html or (text.strip() and text.strip() != (row[3] or '').strip()):
            connection.execute(sa.text('UPDATE announcements SET content_bytes=:size, '
                'content_cached_at=:now,content_accessed_at=:now WHERE id=:id'),
                {'id': row[0], 'size': len(html.encode()) + len(text.encode()), 'now': now})


def downgrade():
    with op.batch_alter_table('announcements') as batch:
        batch.drop_index('ix_content_accessed')
        for name in ['content_cached_at', 'content_accessed_at', 'content_bytes', 'content_error']:
            batch.drop_column(name)
    op.drop_table('background_tasks')
