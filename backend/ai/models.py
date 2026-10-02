"""Instance AI configuration and durable billing/execution metadata."""
from datetime import datetime
from backend.database.db import db


class AIProfile(db.Model):
    __tablename__ = 'ai_profiles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    provider = db.Column(db.String(24), nullable=False)
    model = db.Column(db.String(160), nullable=False)
    region = db.Column(db.String(32), nullable=False, default='default')
    encrypted_key = db.Column(db.Text, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    tested_version = db.Column(db.Integer)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    max_output_tokens = db.Column(db.Integer, nullable=False, default=4096)
    last_test_code = db.Column(db.String(80), nullable=False, default='not_tested')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    deleted_at = db.Column(db.DateTime)


class AIBinding(db.Model):
    __tablename__ = 'ai_bindings'
    purpose = db.Column(db.String(24), primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('ai_profiles.id', ondelete='RESTRICT'), nullable=False)


class AIBudget(db.Model):
    __tablename__ = 'ai_budgets'
    key = db.Column(db.String(48), primary_key=True)
    used_tokens = db.Column(db.BigInteger, nullable=False, default=0)
    reserved_tokens = db.Column(db.BigInteger, nullable=False, default=0)
    active_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AIExecution(db.Model):
    __tablename__ = 'ai_executions'
    execution_id = db.Column(db.String(240), primary_key=True)
    purpose = db.Column(db.String(24), nullable=False)
    profile_id = db.Column(db.Integer, db.ForeignKey('ai_profiles.id', ondelete='SET NULL'))
    config_version = db.Column(db.String(64), nullable=False)
    provider = db.Column(db.String(24), nullable=False)
    model = db.Column(db.String(160), nullable=False)
    skill_id = db.Column(db.String(80), nullable=False)
    skill_version = db.Column(db.String(32), nullable=False)
    skill_digest = db.Column(db.String(64), nullable=False)
    input_digest = db.Column(db.String(64), nullable=False)
    mode = db.Column(db.String(24), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='reserved')
    output = db.Column(db.JSON)
    usage = db.Column(db.JSON, nullable=False, default=dict)
    request_id = db.Column(db.String(200))
    error_code = db.Column(db.String(80), nullable=False, default='')
    retryable = db.Column(db.Boolean, nullable=False, default=False)
    reserved_tokens = db.Column(db.BigInteger, nullable=False, default=0)
    budget_keys = db.Column(db.JSON, nullable=False, default=list)
    active_released = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    heartbeat_at = db.Column(db.DateTime)
    diagnostics = db.Column(db.JSON, nullable=False, default=dict)
    __table_args__ = (db.Index('ix_ai_execution_status_created', 'status', 'created_at'),)
