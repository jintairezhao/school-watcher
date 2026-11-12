"""Keep the latest source entry observation in the shared business database."""
from alembic import op
import sqlalchemy as sa

revision = '6b1d43789af6'
down_revision = '5a0c32678fe5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('source_access_observations',
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False),
        sa.Column('config_hash', sa.String(64), nullable=False),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('observation_json', sa.Text(), nullable=False))
    op.create_index('ix_source_access_observations_school_id', 'source_access_observations', ['school_id'])


def downgrade():
    op.drop_index('ix_source_access_observations_school_id', table_name='source_access_observations')
    op.drop_table('source_access_observations')
