"""A local data owner, accessed only by the native window's launch capability."""
import secrets

from backend.database.db import db
from backend.database.models import AppConfig, User


def ensure_local_owner():
    """Reuse the v0.1 owner so subscriptions and reading history keep their IDs."""
    saved = AppConfig.get('desktop_owner_id')
    owner = db.session.get(User, int(saved)) if saved and saved.isdigit() else None
    if owner is None:
        owner = User.query.filter_by(role='admin').order_by(User.id).first()
    if owner is None:
        owner = User.query.order_by(User.id).first()
    if owner is None:
        owner = User(username='local-' + secrets.token_hex(6),
                     password_hash='!desktop-only', role='admin')
        db.session.add(owner)
        db.session.flush()
    owner.role = 'admin'
    AppConfig.set('desktop_owner_id', str(owner.id))
    return owner


def local_owner():
    saved = AppConfig.get('desktop_owner_id')
    return db.session.get(User, int(saved)) if saved and saved.isdigit() else None
