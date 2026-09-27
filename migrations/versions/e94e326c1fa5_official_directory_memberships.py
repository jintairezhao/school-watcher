"""Keep official directory membership separate from notification columns."""
from alembic import op
import sqlalchemy as sa

revision = 'e94e326c1fa5'
down_revision = 'c83d215b0e94'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('department_directory_entries',
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('department_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_directory_entry_department', 'department_directory_entries', ['department_id'])


def downgrade():
    op.drop_table('department_directory_entries')
