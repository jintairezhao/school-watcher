"""Instance-wide, versioned summaries. Legacy Announcement.summary stays historical."""
from datetime import datetime

from backend.database.db import db


class AnnouncementSummary(db.Model):
    __tablename__ = 'announcement_summaries'

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False)
    identity = db.Column(db.String(160), nullable=False, unique=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    input_hash = db.Column(db.String(64), nullable=False)
    title_snapshot = db.Column(db.Text, nullable=False)
    body_snapshot = db.Column(db.Text, nullable=False, default='')
    source_content_hash = db.Column(db.String(64))
    input_scope = db.Column(db.JSON, nullable=False, default=dict)
    binding = db.Column(db.JSON, nullable=False, default=dict)
    skill_version = db.Column(db.String(100), nullable=False, default='')
    state = db.Column(db.String(24), nullable=False, default='pending')
    summary = db.Column(db.Text, nullable=False, default='')
    output = db.Column(db.JSON, nullable=False, default=dict)
    provenance = db.Column(db.JSON, nullable=False, default=dict)
    progress = db.Column(db.JSON, nullable=False, default=dict)
    error_code = db.Column(db.String(100), nullable=False, default='')
    error = db.Column(db.String(300), nullable=False, default='')
    task_id = db.Column(db.Integer, db.ForeignKey('background_tasks.id', ondelete='SET NULL'))
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('announcement_id', 'input_hash', 'revision', name='uq_summary_input_revision'),
        db.Index('ix_summary_announcement_state', 'announcement_id', 'state', 'id'),
    )
