"""Administrative source lifecycle, independent of observed website health."""
from datetime import datetime
from backend.database.db import db


class SourceLifecycle(db.Model):
    __tablename__ = 'source_lifecycles'
    source_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True)
    school_id = db.Column(db.Integer, nullable=False, index=True)
    state = db.Column(db.String(16), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.CheckConstraint("state IN ('active', 'disabled', 'removed')", name='ck_source_lifecycle_state'),)
