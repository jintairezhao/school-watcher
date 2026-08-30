"""当前用户与权限装饰器

多用户化的身份锚点：session['user_id'] → g.user。
login_required / admin_required 对 /api/* 返回 JSON，对页面返回重定向/403。
"""

import functools

from flask import g, jsonify, redirect, request, session, url_for, abort

from backend.database.models import User


def get_current_user():
    """session → User；用户已删除则清 session 返回 None"""
    user_id = session.get('user_id')
    if not user_id:
        return None
    user = User.query.get(user_id)
    if user is None:
        session.pop('user_id', None)
    return user


def _is_api():
    return request.path.startswith('/api/')


def _unauthorized():
    if _is_api():
        return jsonify({'error': '未登录', 'redirect': url_for('auth.login_page')}), 401
    return redirect(url_for('auth.login_page', next=request.full_path))


def _forbidden():
    if _is_api():
        return jsonify({'error': '需要管理员权限'}), 403
    abort(403)


def login_required(fn):
    """登录用户（含 admin）"""
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
