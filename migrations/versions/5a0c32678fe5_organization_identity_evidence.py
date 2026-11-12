"""Retain independently evidenced organization identity on the existing entity."""
from alembic import op
import sqlalchemy as sa

revision = '5a0c32678fe5'
down_revision = '49fb21567ed4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('departments', sa.Column('identity_json', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('departments') as batch:
        batch.drop_column('identity_json')
