"""认证页面路由：登录 / 注册 / 登出 / 按账号找回"""
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, g)
from werkzeug.security import check_password_hash, generate_password_hash

from backend.auth import hash_answer
from backend.auth.rate_limit import check_rate_limit
from backend.database.db import db
from backend.database.models import AppConfig, User

bp = Blueprint('auth', __name__)


def _safe_next(next_url):
    """防 open redirect：仅接受单斜杠开头的站内路径"""
    if next_url and next_url.startswith('/') and not next_url.startswith('//'):
        return next_url
    return url_for('pages.index')


def _find_user_by_name(username):
    username = (username or '').strip().lower()
    if not username:
        return None
    return User.query.filter(db.func.lower(User.username) == username).first()


@bp.route('/login', methods=['GET', 'POST'])
def login_page():
    """登录（用户名 + 密码，双维限流）"""
    if g.get('user'):
        return redirect(url_for('pages.index'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        allowed, _ = check_rate_limit(f'login:ip:{request.remote_addr}', 30, 900)
        if allowed:
            allowed, _ = check_rate_limit(f'login:{username.lower()}', 5, 900)
        if not allowed:
            error = '尝试次数过多，请稍后再试'
        else:
            user = _find_user_by_name(username)
            if user and check_password_hash(user.password_hash, password):
                session.clear()  # 防会话固定
                session['user_id'] = user.id
                session.permanent = True
                user.last_login_at = datetime.utcnow()
                db.session.commit()
                return redirect(_safe_next(request.args.get('next')))
            error = '用户名或密码错误'

    return render_template('login.html', error=error)


@bp.route('/register', methods=['GET', 'POST'])
def register_page():
    """注册（open_registration 开关控制）"""
    if AppConfig.get('open_registration', '1') != '1':
        return redirect(url_for('auth.login_page'))
    if g.get('user'):
        return redirect(url_for('pages.index'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        allowed, _ = check_rate_limit(f'register:ip:{request.remote_addr}', 5, 3600)
        if not allowed:
            error = '注册请求过于频繁，请稍后再试'
        elif not (2 <= len(username) <= 32):
            error = '用户名需 2-32 个字符'
        elif len(password) < 8:
            error = '密码至少 8 位'
        elif password != confirm:
            error = '两次输入的密码不一致'
        elif _find_user_by_name(username):
            error = '用户名已被占用'
        else:
            user = User(username=username,
                        password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            session.clear()
            session['user_id'] = user.id
            session.permanent = True
            return redirect(url_for('pages.index'))

    return render_template('register.html', error=error)


@bp.route('/logout', methods=['POST'])
def logout():
    """登出（仅 POST，防 CSRF 强制登出）"""
    session.clear()
    return redirect(url_for('auth.login_page'))


# ------------------------------------------------------------------
# 按账号找回（per-user 密保，不泄露账号存在性）
# ------------------------------------------------------------------

@bp.route('/recovery', methods=['GET'])
def recovery_page():
    return render_template('recovery.html')


@bp.route('/api/recovery/question', methods=['GET'])
def api_recovery_question():
    user = _find_user_by_name(request.values.get('username', ''))
    if not user or not user.security_question:
        return jsonify({'has_question': False})
    locked = bool(user.recovery_locked_until and
                  user.recovery_locked_until > datetime.utcnow())
    return jsonify({'has_question': True,
                    'question': user.security_question,
                    'locked': locked})


@bp.route('/api/recovery/verify', methods=['POST'])
def api_recovery_verify():
    data = request.get_json(silent=True) or {}
    user = _find_user_by_name(data.get('username', ''))
    if not user or not user.security_question:
        return jsonify({'error': '该账号未设置密保，请联系管理员重置'}), 400

    now = datetime.utcnow()
    if user.recovery_locked_until and user.recovery_locked_until > now:
        return jsonify({'error': '尝试次数过多，已锁定，请稍后再试'}), 429
    allowed, _ = check_rate_limit(f'recovery:{user.username}', 5, 1800)
    if not allowed:
        user.recovery_locked_until = now + timedelta(minutes=30)
        db.session.commit()
        return jsonify({'error': '尝试次数过多，锁定 30 分钟'}), 429
    if hash_answer(data.get('answer', '')) != user.security_answer_hash:
        return jsonify({'error': '密保答案错误'}), 400

    session['recovery_user_id'] = user.id
    session['recovery_verified_time'] = now.isoformat()
    return jsonify({'success': True})


@bp.route('/api/recovery/reset', methods=['POST'])
def api_recovery_reset():
    uid = session.get('recovery_user_id')
    verified_at = session.get('recovery_verified_time')
    if not uid or not verified_at:
        return jsonify({'error': '请先完成密保验证'}), 400
    try:
        elapsed = datetime.utcnow() - datetime.fromisoformat(verified_at)
    except ValueError:
        elapsed = timedelta(hours=1)
    if elapsed > timedelta(minutes=10):
        return jsonify({'error': '验证已过期，请重新验证'}), 400

    user = db.session.get(User, uid)
    if not user:
        return jsonify({'error': '账号不存在'}), 404
    password = (request.get_json(silent=True) or {}).get('password') or ''
    if len(password) < 8:
        return jsonify({'error': '密码至少 8 位'}), 400

    user.password_hash = generate_password_hash(password)
    user.recovery_locked_until = None
    db.session.commit()
    session.pop('recovery_user_id', None)
    session.pop('recovery_verified_time', None)
    return jsonify({'success': True})
