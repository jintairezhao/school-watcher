"""Persist compact automatic source validation evidence.

Revision ID: c65e317af402
Revises: 9a2e51b72086
"""
from alembic import op
import sqlalchemy as sa

revision = 'c65e317af402'
down_revision = '9a2e51b72086'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('source_validations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('config_hash', sa.String(length=64), nullable=False),
        sa.Column('validation_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_source_validations_school_id', 'source_validations', ['school_id'])
    op.create_index('ix_source_validations_department_id', 'source_validations', ['department_id'])


def downgrade():
    op.drop_index('ix_source_validations_department_id', table_name='source_validations')
    op.drop_index('ix_source_validations_school_id', table_name='source_validations')
    op.drop_table('source_validations')
