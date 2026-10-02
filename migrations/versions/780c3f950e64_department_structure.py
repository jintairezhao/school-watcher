"""Distinguish official units from collectible columns without changing source IDs."""
from alembic import op
import sqlalchemy as sa

revision = '780c3f950e64'
down_revision = '67fb2e849d53'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('departments', sa.Column('kind', sa.String(16), nullable=False, server_default='column'))
    op.add_column('departments', sa.Column('structure_key', sa.String(64), nullable=True))
    op.create_index('ix_departments_structure_key', 'departments', ['structure_key'], unique=True)


def downgrade():
    op.drop_index('ix_departments_structure_key', table_name='departments')
    with op.batch_alter_table('departments') as batch:
        batch.drop_column('structure_key')
        batch.drop_column('kind')
