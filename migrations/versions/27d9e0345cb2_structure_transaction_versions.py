"""Publish per-school structure cursors in the fact mutation transaction."""
from alembic import op
from datetime import datetime
from uuid import uuid4
import sqlalchemy as sa

revision = '27d9e0345cb2'
down_revision = '16c8d9234ba1'
branch_labels = None
depends_on = None


def upgrade():
    revisions = op.create_table('structure_revisions',
        sa.Column('school_id', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('epoch', sa.String(32), nullable=False),
        sa.Column('revision', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    changes = op.create_table('structure_changes',
        sa.Column('school_id', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('epoch', sa.String(32), primary_key=True),
        sa.Column('revision', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('changes', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    # An old database (including a restored pre-version backup) must not retain
    # an ambiguous null/0 cursor. No structure or personal-state rows change.
    connection = op.get_bind()
    schools = sa.table('schools', sa.column('id', sa.Integer()))
    for school_id in connection.execute(sa.select(schools.c.id)).scalars():
        epoch, stamp = uuid4().hex, datetime.utcnow()
        connection.execute(revisions.insert().values(school_id=school_id, epoch=epoch,
            revision=1, updated_at=stamp))
        connection.execute(changes.insert().values(school_id=school_id, epoch=epoch,
            revision=1, created_at=stamp, changes=[dict(entity_type='epoch',
                entity_id=str(school_id), operation='reset', fields=[],
                reason='structure_versions_initialized')]))


def downgrade():
    op.drop_table('structure_changes')
    op.drop_table('structure_revisions')
