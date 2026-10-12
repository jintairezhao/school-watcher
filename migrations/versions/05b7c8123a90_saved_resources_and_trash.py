"""Keep saved article resources and a dated, recoverable favorite trash state."""
from alembic import op
import sqlalchemy as sa

revision = '05b7c8123a90'
down_revision = 'c65e317af402'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user_announcement_states', sa.Column('trashed_at', sa.DateTime(), nullable=True))
    op.create_index('ix_user_announcement_states_trashed_at', 'user_announcement_states', ['trashed_at'])
    op.create_index('ix_state_retention_pin', 'user_announcement_states', ['announcement_id','starred','trashed_at'])
    op.add_column('announcements', sa.Column('resources_scanned_at', sa.DateTime(), nullable=True))
    op.create_table('announcement_resources',
        sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('url_key', sa.String(64), primary_key=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('filename', sa.String(1000), nullable=False, server_default=''),
        sa.Column('kind', sa.String(20), nullable=False, server_default='attachment'),
        sa.Column('state', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('mime_type', sa.String(200), nullable=False, server_default=''),
        sa.Column('byte_size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.String(64), nullable=False, server_default=''),
        sa.Column('payload', sa.LargeBinary(), nullable=True),
        sa.Column('saved_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.String(500), nullable=False, server_default=''))


def downgrade():
    op.drop_table('announcement_resources')
    op.drop_index('ix_state_retention_pin', table_name='user_announcement_states')
    op.drop_index('ix_user_announcement_states_trashed_at', table_name='user_announcement_states')
    with op.batch_alter_table('user_announcement_states') as batch:
        batch.drop_column('trashed_at')
    with op.batch_alter_table('announcements') as batch:
        batch.drop_column('resources_scanned_at')
