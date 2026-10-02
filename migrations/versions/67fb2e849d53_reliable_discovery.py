"""Durable directory inputs and live model execution heartbeats."""
from alembic import op
import sqlalchemy as sa

revision = '67fb2e849d53'
down_revision = '56ea1d738c42'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ai_executions', sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
    op.add_column('ai_executions', sa.Column('diagnostics', sa.JSON(), nullable=False, server_default='{}'))
    op.execute('UPDATE ai_executions SET heartbeat_at = created_at')
    op.create_table('discovery_work_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('identity', sa.String(64), nullable=False, unique=True),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('group_key', sa.String(240), nullable=False),
        sa.Column('candidate_id', sa.String(120), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('reference_url', sa.Text(), nullable=False, server_default=''),
        sa.Column('material_hash', sa.String(64), nullable=False),
        sa.Column('input_json', sa.JSON(), nullable=False),
        sa.Column('result_json', sa.JSON()),
        sa.Column('state', sa.String(30), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('execution_id', sa.String(240)),
        sa.Column('error_code', sa.String(80), nullable=False, server_default=''),
        sa.Column('next_run_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    for column in ('school_id', 'group_key', 'state'):
        op.create_index('ix_discovery_work_items_' + column, 'discovery_work_items', [column])


def downgrade():
    op.drop_table('discovery_work_items')
    with op.batch_alter_table('ai_executions') as batch:
        batch.drop_column('heartbeat_at')
        batch.drop_column('diagnostics')
