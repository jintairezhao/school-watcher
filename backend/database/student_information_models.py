"""Derived assessments; original notices and subscription choices remain authoritative."""
from datetime import datetime
from backend.database.db import db


class StudentAssessment(db.Model):
    __tablename__ = 'student_assessments'
    subject_key = db.Column(db.String(80), primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), index=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcements.id', ondelete='CASCADE'), index=True)
    input_hash = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(24), nullable=False, default='pending')
    result = db.Column(db.JSON, nullable=False, default=dict)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
