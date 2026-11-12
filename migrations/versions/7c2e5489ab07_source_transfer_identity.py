"""Preserve portable source identity independently of mutable names and URLs."""
from alembic import op
import sqlalchemy as sa
from datetime import datetime
from uuid import uuid4

revision = '7c2e5489ab07'
down_revision = '6b1d43789af6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('source_transfer_identities',
        sa.Column('transfer_key', sa.String(80), primary_key=True),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_source_transfer_identities_source_id', 'source_transfer_identities', ['source_id'])
    connection = op.get_bind()
    table = sa.table('source_transfer_identities', sa.column('transfer_key'), sa.column('source_id'),
                     sa.column('kind'), sa.column('created_at'))
    pending = []
    for (ident,) in connection.execute(sa.text("SELECT id FROM departments WHERE kind='column' ORDER BY id")):
        pending.append(dict(source_id=ident, transfer_key='source:' + uuid4().hex,
                            kind='record', created_at=datetime.utcnow()))
        if len(pending) == 500:
            connection.execute(table.insert(), pending)
            pending = []
    if pending:
        connection.execute(table.insert(), pending)


def downgrade():
    op.drop_index('ix_source_transfer_identities_source_id', table_name='source_transfer_identities')
    op.drop_table('source_transfer_identities')
