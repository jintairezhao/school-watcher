"""Keep deleted AI service provenance without listing or reusing the service."""
from alembic import op
import sqlalchemy as sa

revision = '56ea1d738c42'
down_revision = '45d9fc6271b3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ai_profiles', sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('ai_profiles') as batch:
        batch.drop_column('deleted_at')
