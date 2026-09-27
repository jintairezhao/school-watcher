"""Instance AI, shared summaries and source governance.

Revision ID: 34c8eb5160a2
Revises: 23b7da405f91
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime
import hashlib

revision = "34c8eb5160a2"
down_revision = "23b7da405f91"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table('ai_budgets'):
        op.create_table('ai_budgets',
            sa.Column('key', sa.String(length=48), primary_key=True, nullable=False),
            sa.Column('used_tokens', sa.BigInteger(), nullable=False),
            sa.Column('reserved_tokens', sa.BigInteger(), nullable=False),
            sa.Column('active_count', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    if not sa.inspect(op.get_bind()).has_table('ai_profiles'):
        op.create_table('ai_profiles',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('provider', sa.String(length=24), nullable=False),
            sa.Column('model', sa.String(length=160), nullable=False),
            sa.Column('region', sa.String(length=32), nullable=False),
            sa.Column('encrypted_key', sa.Text(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('tested_version', sa.Integer(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('max_output_tokens', sa.Integer(), nullable=False),
            sa.Column('last_test_code', sa.String(length=80), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    if not sa.inspect(op.get_bind()).has_table('ai_bindings'):
        op.create_table('ai_bindings',
            sa.Column('purpose', sa.String(length=24), primary_key=True, nullable=False),
            sa.Column('profile_id', sa.Integer(), sa.ForeignKey('ai_profiles.id', ondelete='RESTRICT'), nullable=False),
        )
    if not sa.inspect(op.get_bind()).has_table('ai_executions'):
        op.create_table('ai_executions',
            sa.Column('execution_id', sa.String(length=240), primary_key=True, nullable=False),
            sa.Column('purpose', sa.String(length=24), nullable=False),
            sa.Column('profile_id', sa.Integer(), sa.ForeignKey('ai_profiles.id', ondelete='SET NULL'), nullable=True),
            sa.Column('config_version', sa.String(length=64), nullable=False),
            sa.Column('provider', sa.String(length=24), nullable=False),
            sa.Column('model', sa.String(length=160), nullable=False),
            sa.Column('skill_id', sa.String(length=80), nullable=False),
            sa.Column('skill_version', sa.String(length=32), nullable=False),
            sa.Column('skill_digest', sa.String(length=64), nullable=False),
            sa.Column('input_digest', sa.String(length=64), nullable=False),
            sa.Column('mode', sa.String(length=24), nullable=False),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('output', sa.JSON(), nullable=True),
            sa.Column('usage', sa.JSON(), nullable=False),
            sa.Column('request_id', sa.String(length=200), nullable=True),
            sa.Column('error_code', sa.String(length=80), nullable=False),
            sa.Column('retryable', sa.Boolean(), nullable=False),
            sa.Column('reserved_tokens', sa.BigInteger(), nullable=False),
            sa.Column('budget_keys', sa.JSON(), nullable=False),
            sa.Column('active_released', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_ai_execution_status_created', 'ai_executions', ['status', 'created_at'], unique=False)
    if not sa.inspect(op.get_bind()).has_table('school_onboarding'):
        op.create_table('school_onboarding',
            sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id', ondelete='CASCADE'), primary_key=True, nullable=False),
            sa.Column('state', sa.String(length=30), nullable=False),
            sa.Column('policy_version', sa.String(length=40), nullable=False),
            sa.Column('generation', sa.Integer(), nullable=False),
            sa.Column('pending_pages', sa.Integer(), nullable=False),
            sa.Column('checked_pages', sa.Integer(), nullable=False),
            sa.Column('scope_json', sa.Text(), nullable=False),
            sa.Column('checkpoint_json', sa.Text(), nullable=False),
            sa.Column('last_error', sa.String(length=800), nullable=False),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('next_check_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    if not sa.inspect(op.get_bind()).has_table('school_registry_entries'):
        op.create_table('school_registry_entries',
            sa.Column('registry_key', sa.String(length=64), primary_key=True, nullable=False),
            sa.Column('canonical_name', sa.String(length=200), nullable=False),
            sa.Column('root_url', sa.String(length=1000), nullable=False),
            sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id', ondelete='SET NULL'), nullable=True),
            sa.Column('aliases', sa.JSON(), nullable=False),
            sa.Column('origin', sa.String(length=20), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('school_id'),
        )
    if not sa.inspect(op.get_bind()).has_table('source_proposals'):
        op.create_table('source_proposals',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False),
            sa.Column('department_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='SET NULL'), nullable=True),
            sa.Column('identity_key', sa.String(length=64), nullable=False),
            sa.Column('proposal_key', sa.String(length=64), nullable=False),
            sa.Column('origin', sa.String(length=40), nullable=False),
            sa.Column('state', sa.String(length=30), nullable=False),
            sa.Column('candidate_json', sa.Text(), nullable=False),
            sa.Column('expected_config_hash', sa.String(length=64), nullable=False),
            sa.Column('evidence_json', sa.Text(), nullable=False),
            sa.Column('evidence_hash', sa.String(length=64), nullable=False),
            sa.Column('validation_json', sa.Text(), nullable=False),
            sa.Column('validator_version', sa.String(length=40), nullable=True),
            sa.Column('validated_hash', sa.String(length=64), nullable=True),
            sa.Column('revision', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('proposal_key'),
        )
        op.create_index('ix_source_proposals_department_id', 'source_proposals', ['department_id'], unique=False)
        op.create_index('ix_source_proposals_school_id', 'source_proposals', ['school_id'], unique=False)
        op.create_index('ix_source_proposals_state', 'source_proposals', ['state'], unique=False)
    if not sa.inspect(op.get_bind()).has_table('announcement_summaries'):
        op.create_table('announcement_summaries',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False),
            sa.Column('identity', sa.String(length=160), nullable=False),
            sa.Column('revision', sa.Integer(), nullable=False),
            sa.Column('input_hash', sa.String(length=64), nullable=False),
            sa.Column('title_snapshot', sa.Text(), nullable=False),
            sa.Column('body_snapshot', sa.Text(), nullable=False),
            sa.Column('source_content_hash', sa.String(length=64), nullable=True),
            sa.Column('input_scope', sa.JSON(), nullable=False),
            sa.Column('binding', sa.JSON(), nullable=False),
            sa.Column('skill_version', sa.String(length=100), nullable=False),
            sa.Column('state', sa.String(length=24), nullable=False),
            sa.Column('summary', sa.Text(), nullable=False),
            sa.Column('output', sa.JSON(), nullable=False),
            sa.Column('provenance', sa.JSON(), nullable=False),
            sa.Column('progress', sa.JSON(), nullable=False),
            sa.Column('error_code', sa.String(length=100), nullable=False),
            sa.Column('error', sa.String(length=300), nullable=False),
            sa.Column('task_id', sa.Integer(), sa.ForeignKey('background_tasks.id', ondelete='SET NULL'), nullable=True),
            sa.Column('requested_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('announcement_id', 'input_hash', 'revision', name='uq_summary_input_revision'),
            sa.UniqueConstraint('identity'),
        )
        op.create_index('ix_summary_announcement_state', 'announcement_summaries', ['announcement_id', 'state', 'id'], unique=False)
    if not sa.inspect(op.get_bind()).has_table('source_config_versions'):
        op.create_table('source_config_versions',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('department_id', sa.Integer(), sa.ForeignKey('departments.id', ondelete='CASCADE'), nullable=False),
            sa.Column('proposal_id', sa.Integer(), sa.ForeignKey('source_proposals.id', ondelete='SET NULL'), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('config_json', sa.Text(), nullable=False),
            sa.Column('config_hash', sa.String(length=64), nullable=False),
            sa.Column('previous_json', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('proposal_id'),
            sa.UniqueConstraint('department_id', 'version', name='uq_source_version'),
        )
        op.create_index('ix_source_config_versions_department_id', 'source_config_versions', ['department_id'], unique=False)
    if not sa.inspect(op.get_bind()).has_table('source_review_events'):
        op.create_table('source_review_events',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('proposal_id', sa.Integer(), sa.ForeignKey('source_proposals.id', ondelete='CASCADE'), nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('action', sa.String(length=40), nullable=False),
            sa.Column('note', sa.Text(), nullable=False),
            sa.Column('detail_json', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_source_review_events_proposal_id', 'source_review_events', ['proposal_id'], unique=False)

    # Preserve unknown-provenance historical summaries without inventing input versions.
    metadata = sa.MetaData()
    notices = sa.Table('announcements', metadata, autoload_with=op.get_bind())
    summaries = sa.Table('announcement_summaries', metadata, autoload_with=op.get_bind())
    rows = op.get_bind().execute(sa.select(notices.c.id, notices.c.summary).where(
        notices.c.summary.is_not(None), notices.c.summary != '')).mappings()
    while batch := rows.fetchmany(500):
        for row in batch:
            identity = 'legacy:' + str(row['id'])
            if op.get_bind().execute(sa.select(summaries.c.id).where(summaries.c.identity == identity)).first():
                continue
            op.get_bind().execute(summaries.insert(), dict(
                announcement_id=row['id'], identity=identity, revision=1,
                input_hash=hashlib.sha256(('legacy:' + row['summary']).encode()).hexdigest(),
                title_snapshot='', body_snapshot='', input_scope={}, binding={},
                skill_version='', state='historical', summary=row['summary'], output={},
                provenance={'origin': 'legacy', 'version_known': False}, progress={},
                error_code='', error='', created_at=datetime.utcnow()))


def downgrade():
    # Downgrade removes new metadata, never source articles or personal state.
    op.drop_table('source_review_events')
    op.drop_table('source_config_versions')
    op.drop_table('announcement_summaries')
    op.drop_table('source_proposals')
    op.drop_table('school_registry_entries')
    op.drop_table('school_onboarding')
    op.drop_table('ai_executions')
    op.drop_table('ai_bindings')
    op.drop_table('ai_profiles')
    op.drop_table('ai_budgets')
