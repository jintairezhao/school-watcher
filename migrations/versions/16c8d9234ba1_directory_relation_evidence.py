"""Retain typed evidence on existing directory memberships."""
from alembic import op
import sqlalchemy as sa

revision = '16c8d9234ba1'
down_revision = '05b7c8123a90'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('department_directory_entries',
        sa.Column('facts_json', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    with op.batch_alter_table('department_directory_entries') as batch:
        batch.drop_column('facts_json')
