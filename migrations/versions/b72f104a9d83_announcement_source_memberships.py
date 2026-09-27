"""Preserve every observed column membership without duplicating articles.

Revision ID: b72f104a9d83
Revises: a91c2e4f7b30
"""
from alembic import op
import sqlalchemy as sa

revision = 'b72f104a9d83'
down_revision = 'a91c2e4f7b30'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'announcement_sources',
        sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('department_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('list_url', sa.String(1000), nullable=False, server_default=''),
        sa.Column('article_url', sa.String(2000), nullable=False, server_default=''),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    )
    op.create_index('idx_announcement_source_department', 'announcement_sources', ['department_id', 'announcement_id'])
    op.execute(sa.text("""INSERT INTO announcement_sources
        (announcement_id,department_id,list_url,article_url,first_seen_at,last_seen_at)
        SELECT a.id,a.department_id,coalesce(d.list_url,''),coalesce(a.url,''),
               coalesce(a.created_at,CURRENT_TIMESTAMP),coalesce(a.created_at,CURRENT_TIMESTAMP)
        FROM announcements a JOIN departments d ON d.id=a.department_id"""))


def downgrade():
    op.drop_index('idx_announcement_source_department', table_name='announcement_sources')
    op.drop_table('announcement_sources')
