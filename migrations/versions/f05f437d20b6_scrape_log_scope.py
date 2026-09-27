"""Keep the source label with each attempt and index administrator history."""
from alembic import op
import sqlalchemy as sa

revision = 'f05f437d20b6'
down_revision = 'e94e326c1fa5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('scrape_logs', sa.Column('source_name', sa.String(200), nullable=False, server_default=''))
    op.create_index('ix_scrape_logs_started_id', 'scrape_logs', ['started_at', 'id'])
    op.create_index('ix_scrape_logs_school_started', 'scrape_logs', ['school_id', 'started_at', 'id'])


def downgrade():
    op.drop_index('ix_scrape_logs_school_started', table_name='scrape_logs')
    op.drop_index('ix_scrape_logs_started_id', table_name='scrape_logs')
    with op.batch_alter_table('scrape_logs') as batch:
        batch.drop_column('source_name')
