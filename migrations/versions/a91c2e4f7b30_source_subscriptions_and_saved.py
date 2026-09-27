"""Add optional column subscriptions and personal message management.

Revision ID: a91c2e4f7b30
Revises: d4693c99491f
"""
from alembic import op
import sqlalchemy as sa

revision = 'a91c2e4f7b30'
down_revision = 'd4693c99491f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('subscriptions', sa.Column('department_ids', sa.JSON(), nullable=True))
    op.create_table(
        'user_announcement_states',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('starred', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default='0'),
    )


def downgrade():
    op.drop_table('user_announcement_states')
    op.drop_column('subscriptions', 'department_ids')
