"""Native launch capability, internal browser authentication and CSRF."""

import secrets
import logging

from flask import g, request, jsonify, redirect, url_for, session, current_app

logger = logging.getLogger(__name__)

_REMOVED_ACCOUNT_PATHS = ('/login', '/register', '/recovery', '/logout', '/me', '/api/admin/toggles')
_HEALTH_PATHS = ('/health/live', '/health/ready')


def generate_csrf_token() -> str:
    """发放（或复用）当前会话的 CSRF token"""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def register_hooks(app):
    """注册 before_request 钩子与模板全局变量"""
    from backend.auth.decorators import get_current_user

    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    @app.before_request
    def _authenticate_internal_browser():
        g.internal_browser = False
        if request.path.startswith('/internal/browser-origin/'):
            expected = current_app.config.get('BROWSER_SERVICE_TOKEN', '')
            supplied = request.headers.get('X-Watcher-Token', '')
            if len(expected) < 32 or not secrets.compare_digest(expected, supplied):
                return jsonify(error='unauthorized'), 401
            g.internal_browser = True

    @app.context_processor
    def _inject_user():
        return {'current_user': g.get('user')}

    @app.before_request
    def _load_user():
        if current_app.config.get('DESKTOP_MODE') and request.path not in _HEALTH_PATHS and not g.internal_browser:
            from urllib.parse import urlsplit
            from backend.auth.desktop import local_owner
            g.user = None
            expected = current_app.config.get('DESKTOP_TOKEN', '')
            origin = urlsplit(current_app.config['DESKTOP_ORIGIN'])
            if request.remote_addr not in ('127.0.0.1', '::1') or request.host != origin.netloc or len(expected) < 32:
                return jsonify(error='请从桌面应用打开学校通知'), 403
            if request.path == '/_desktop/open':
                supplied = request.args.get('token', '')
                if request.method != 'GET' or not supplied.isascii() or not secrets.compare_digest(expected, supplied):
                    return jsonify(error='桌面会话已失效，请重新打开应用'), 403
                session.clear()
                session['desktop_access'] = expected
                session.permanent = True
                return redirect(url_for('pages.index'))
            supplied = session.get('desktop_access', '')
            if not isinstance(supplied, str) or not secrets.compare_digest(expected, supplied):
                return jsonify(error='请从桌面应用打开学校通知'), 403
            g.user = local_owner()
            if g.user is None:
                return jsonify(error='本机数据尚未准备好，请重新打开应用'), 503
            return None
        g.user = None if request.path in _HEALTH_PATHS or g.internal_browser else get_current_user()

    @app.before_request
    def _removed_account_routes():
        if request.path in _REMOVED_ACCOUNT_PATHS or request.path.startswith(
                ('/api/me/', '/api/recovery/', '/api/admin/users')):
            return jsonify(error='本机应用无需账号'), 404

    @app.before_request
    def _csrf_protect():
        """写方法必须携带 CSRF token（头或表单域），与 session 常量时间比对"""
        if g.internal_browser or request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
            return None
        token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        expected = session.get('_csrf_token')
        if not expected or not token or not secrets.compare_digest(token, expected):
            return jsonify({'error': 'CSRF 校验失败'}), 403
