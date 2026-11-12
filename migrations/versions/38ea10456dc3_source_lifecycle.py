"""Retain source identity and history when collection is disabled or removed."""
from alembic import op
import sqlalchemy as sa
revision = '38ea10456dc3'
down_revision = '27d9e0345cb2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('source_lifecycles',
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('active', 'disabled', 'removed')", name='ck_source_lifecycle_state'))
    op.create_index('ix_source_lifecycles_school_id', 'source_lifecycles', ['school_id'])


def downgrade():
    op.drop_table('source_lifecycles')
