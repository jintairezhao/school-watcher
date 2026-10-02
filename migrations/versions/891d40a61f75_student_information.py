"""Evidence-backed student value, independent of deadlines and subscription scope."""
from alembic import op
import sqlalchemy as sa

revision = '891d40a61f75'
down_revision = '780c3f950e64'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('student_assessments',
        sa.Column('subject_key', sa.String(80), primary_key=True),
        sa.Column('school_id', sa.Integer, sa.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False),
        sa.Column('department_id', sa.Integer, sa.ForeignKey('departments.id', ondelete='CASCADE')),
        sa.Column('announcement_id', sa.Integer, sa.ForeignKey('announcements.id', ondelete='CASCADE')),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('result', sa.JSON, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False))
    for field in ('school_id', 'department_id', 'announcement_id'):
        op.create_index('ix_student_assessments_' + field, 'student_assessments', [field])


def downgrade():
    op.drop_table('student_assessments')
