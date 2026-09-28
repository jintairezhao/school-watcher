"""Bind account sessions to a revocable authentication generation."""
from alembic import op
import sqlalchemy as sa

revision = '45d9fc6271b3'
down_revision = '34c8eb5160a2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('auth_version', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('users') as batch:
        batch.drop_column('auth_version')
