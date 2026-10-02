"""Data-owner guards; production requests use the native launch capability.

Legacy record IDs remain valid for stored subscriptions and isolated service
tests. There is no endpoint that issues username/password sessions.
"""

import functools

from flask import g, jsonify, request, session, abort

from backend.database.models import User


def get_current_user():
    """session → User；用户已删除则清 session 返回 None"""
    user_id = session.get('user_id')
    if not user_id:
        return None
    user = User.query.get(user_id)
    if user is None or session.get('auth_version', 0) != user.auth_version:
        session.clear()
        return None
    return user


def _is_api():
    return request.path.startswith('/api/')


def _unauthorized():
    if _is_api():
        return jsonify({'error': '本机会话已失效，请重新打开应用'}), 401
    abort(403)


def _forbidden():
    if _is_api():
        return jsonify({'error': '需要管理员权限'}), 403
    abort(403)


def login_required(fn):
    """Require an active local data-owner context."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if g.get('user') is None:
            return _unauthorized()
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """仅 admin"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if g.get('user') is None:
            return _unauthorized()
        if not g.user.is_admin:
            return _forbidden()
        return fn(*args, **kwargs)
    return wrapper
