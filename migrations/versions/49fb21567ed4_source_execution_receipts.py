"""Keep execution receipts with the configuration transaction they describe."""
from alembic import op
import sqlalchemy as sa

revision = '49fb21567ed4'
down_revision = '38ea10456dc3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('source_config_versions', sa.Column('execution_json', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('source_config_versions') as batch:
        batch.drop_column('execution_json')
