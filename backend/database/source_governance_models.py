"""Portable source review records; candidate discovery never changes live rules."""
from datetime import datetime
from backend.database.db import db


class SourceProposal(db.Model):
    __tablename__ = 'source_proposals'
    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='SET NULL'), index=True)
    identity_key = db.Column(db.String(64), nullable=False)
    proposal_key = db.Column(db.String(64), nullable=False, unique=True)
    origin = db.Column(db.String(40), nullable=False)
    state = db.Column(db.String(30), nullable=False, default='proposed', index=True)
    candidate_json = db.Column(db.Text, nullable=False)
    expected_config_hash = db.Column(db.String(64), nullable=False)
    evidence_json = db.Column(db.Text, nullable=False, default='{}')
    evidence_hash = db.Column(db.String(64), nullable=False)
    validation_json = db.Column(db.Text, nullable=False, default='{}')
    validator_version = db.Column(db.String(40))
    validated_hash = db.Column(db.String(64))
    revision = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class SourceConfigVersion(db.Model):
    __tablename__ = 'source_config_versions'
    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), nullable=False, index=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey('source_proposals.id', ondelete='SET NULL'), unique=True)
    version = db.Column(db.Integer, nullable=False)
    config_json = db.Column(db.Text, nullable=False)
    config_hash = db.Column(db.String(64), nullable=False)
    previous_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('department_id', 'version', name='uq_source_version'),)


class SourceReviewEvent(db.Model):
    __tablename__ = 'source_review_events'
    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey('source_proposals.id', ondelete='CASCADE'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    action = db.Column(db.String(40), nullable=False)
    note = db.Column(db.Text, nullable=False, default='')
    detail_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SchoolOnboarding(db.Model):
    __tablename__ = 'school_onboarding'
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), primary_key=True)
    state = db.Column(db.String(30), nullable=False, default='not_checked')
    policy_version = db.Column(db.String(40), nullable=False, default='1')
    generation = db.Column(db.Integer, nullable=False, default=1)
    pending_pages = db.Column(db.Integer, nullable=False, default=0)
    checked_pages = db.Column(db.Integer, nullable=False, default=0)
    scope_json = db.Column(db.Text, nullable=False, default='[]')
    checkpoint_json = db.Column(db.Text, nullable=False, default='{}')
    last_error = db.Column(db.String(800), nullable=False, default='')
    reviewed_at = db.Column(db.DateTime)
    next_check_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class DiscoveryWorkItem(db.Model):
    """One versioned input has one result and a shared three-dispatch budget."""
    __tablename__ = 'discovery_work_items'
    id = db.Column(db.Integer, primary_key=True)
    identity = db.Column(db.String(64), nullable=False, unique=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False, index=True)
    generation = db.Column(db.Integer, nullable=False)
    group_key = db.Column(db.String(240), nullable=False, index=True)
    candidate_id = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(30), nullable=False)
    reference_url = db.Column(db.Text, nullable=False, default='')
    material_hash = db.Column(db.String(64), nullable=False)
    input_json = db.Column(db.JSON, nullable=False)
    result_json = db.Column(db.JSON)
    state = db.Column(db.String(30), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    execution_id = db.Column(db.String(240))
    error_code = db.Column(db.String(80), nullable=False, default='')
    next_run_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
