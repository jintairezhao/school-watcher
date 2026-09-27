"""Stable public school identities, independent of a subscriber or website host."""
from datetime import datetime

from backend.database.db import db


class SchoolRegistryEntry(db.Model):
    __tablename__ = 'school_registry_entries'

    registry_key = db.Column(db.String(64), primary_key=True)
    canonical_name = db.Column(db.String(200), nullable=False)
    root_url = db.Column(db.String(1000), nullable=False)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='SET NULL'), unique=True)
    aliases = db.Column(db.JSON, nullable=False, default=list)
    origin = db.Column(db.String(20), nullable=False, default='catalog')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
