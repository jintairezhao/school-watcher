"""认证钩子与 CSRF

多用户化后的职责边界：
- 身份加载（g.user）与模板注入 current_user
- CSRF 校验（写方法必须带 X-CSRF-Token 头或 csrf_token 表单域）
- public_read 开关：'0' 时整站回退为「仅登录可读」（公开服务一键降级闸门）
- 采集由独立 worker 运行

密码/限流/权限装饰器分别在 decorators.py / rate_limit.py，路由级鉴权用装饰器声明。
"""

import hashlib
import secrets
import logging

from flask import g, request, jsonify, redirect, url_for, session, current_app

from backend.database.models import AppConfig

logger = logging.getLogger(__name__)

# 认证相关路径（匿名可访问，不做 public_read 门控）
_AUTH_PATHS = ('/login', '/register', '/recovery', '/logout')
_HEALTH_PATHS = ('/health/live', '/health/ready')


def hash_answer(answer: str) -> str:
    """密保答案 SHA256（不区分大小写，去首尾空白）"""
    return hashlib.sha256(answer.strip().lower().encode('utf-8')).hexdigest()


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
        g.user = None if request.path in _HEALTH_PATHS or g.internal_browser else get_current_user()

    @app.before_request
    def _public_gate():
        """public_read='0' 时整站仅登录可读（降级回私有工具形态）"""
        if request.path in _HEALTH_PATHS or g.internal_browser:
            return None
        if AppConfig.get('public_read', '1') != '0':
            return None
        if request.path.startswith('/static/'):
            return None
        if request.path in _AUTH_PATHS or request.path.startswith('/api/recovery/'):
            return None
        if g.get('user'):
            return None
        if request.path.startswith('/api/'):
            return jsonify({'error': '未登录', 'redirect': url_for('auth.login_page')}), 401
        return redirect(url_for('auth.login_page', next=request.full_path))

    @app.before_request
    def _csrf_protect():
        """写方法必须携带 CSRF token（头或表单域），与 session 常量时间比对"""
        if g.internal_browser or request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
            return None
        token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        expected = session.get('_csrf_token')
        if not expected or not token or not secrets.compare_digest(token, expected):
            return jsonify({'error': 'CSRF 校验失败'}), 403
