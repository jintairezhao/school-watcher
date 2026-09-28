"""Password replacement invalidates both login and recovery cookies atomically."""
from sqlalchemy import update
from werkzeug.security import generate_password_hash
from backend.database.db import db
from backend.database.models import User


def replace_password(user, password):
    result = db.session.execute(update(User).where(
        User.id == user.id, User.auth_version == user.auth_version,
    ).values(password_hash=generate_password_hash(password), auth_version=User.auth_version + 1))
    if result.rowcount != 1:
        db.session.rollback()
        return False
    db.session.refresh(user)
    return True
