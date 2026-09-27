"""Atomic article identities and an audit of merged historical IDs.

Revision ID: 23b7da405f91
Revises: 12a6c93f4e80
"""
from alembic import op
import sqlalchemy as sa

revision = '23b7da405f91'
down_revision = '12a6c93f4e80'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('announcements', sa.Column('canonical_url', sa.Text(), nullable=True))
    op.add_column('announcements', sa.Column('url_key', sa.String(64), nullable=True))
    if not sa.inspect(op.get_bind()).has_table('announcement_merges'):
        op.create_table('announcement_merges',
                    sa.Column('old_id', sa.Integer(), primary_key=True),
                    sa.Column('survivor_id', sa.Integer(), nullable=False),
                    sa.Column('merged_at', sa.DateTime(), nullable=False))
        op.create_index('ix_announcement_merges_survivor_id', 'announcement_merges', ['survivor_id'])
    from backend.database.article_migration import merge_announcement_duplicates
    merge_announcement_duplicates(op.get_bind())
    op.create_index('uq_announcement_school_url_key', 'announcements', ['school_id', 'url_key'], unique=True)


def downgrade():
    # The merge preserves content and personal state, but cannot invent deleted
    # duplicate rows on downgrade. The audit is intentionally retained.
    op.drop_index('uq_announcement_school_url_key', table_name='announcements')
    op.drop_column('announcements', 'url_key')
    op.drop_column('announcements', 'canonical_url')
