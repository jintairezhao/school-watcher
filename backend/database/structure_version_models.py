"""Transactional cursors for projections of the application's structure facts."""
from datetime import datetime

from backend.database.db import db


class StructureRevision(db.Model):
    __tablename__ = 'structure_revisions'

    # Deliberately no cascade: a removed school still needs a deletion cursor.
    school_id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    epoch = db.Column(db.String(32), nullable=False)
    revision = db.Column(db.BigInteger, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class StructureChange(db.Model):
    __tablename__ = 'structure_changes'

    school_id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    epoch = db.Column(db.String(32), primary_key=True)
    revision = db.Column(db.BigInteger, primary_key=True, autoincrement=False)
    changes = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SourceAccessObservation(db.Model):
    """Latest attributed entry response, separate from admission/ownership."""
    __tablename__ = 'source_access_observations'

    source_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), nullable=False, index=True)
    config_hash = db.Column(db.String(64), nullable=False)
    observed_at = db.Column(db.DateTime, nullable=False)
    observation_json = db.Column(db.Text, nullable=False)
