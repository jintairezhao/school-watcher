"""个人中心与用户管理 API

/me 自服务：改密码 / 设密保 / 我的订阅。
admin：用户列表 / 重置密码（一次性临时密码）/ 角色切换。
"""
import secrets

from flask import Blueprint, g, jsonify, request, render_template, session
from werkzeug.security import check_password_hash

from backend.auth import admin_required, hash_answer, login_required
from backend.database.db import db
from backend.database.models import Subscription, User
from backend.auth.passwords import replace_password

bp = Blueprint('account', __name__)


@bp.route('/me')
@login_required
def me_page():
    subscriptions = Subscription.query.filter_by(user_id=g.user.id).all()
    return render_template('me.html', subscriptions=subscriptions)


@bp.route('/api/me/password', methods=['POST'])
@login_required
def api_change_password():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user.password_hash,
                               data.get('current_password') or ''):
        return jsonify({'error': '当前密码不正确'}), 403
    new = data.get('new_password') or ''
    if len(new) < 8:
        return jsonify({'error': '密码至少 8 位'}), 400
    if not replace_password(g.user, new):
        return jsonify({'error': '账号已更新，请重新登录'}), 409
    db.session.commit()
    session['auth_version'] = g.user.auth_version
    return jsonify({'success': True})


@bp.route('/api/me/security', methods=['POST'])
@login_required
def api_set_security():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user.password_hash,
                               data.get('current_password') or ''):
        return jsonify({'error': '当前密码不正确'}), 403
    question = (data.get('question') or '').strip()
    answer = (data.get('answer') or '').strip()
    if len(question) < 2 or not answer:
        return jsonify({'error': '问题与答案均不能为空'}), 400
    g.user.security_question = question
    g.user.security_answer_hash = hash_answer(answer)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/admin/users', methods=['GET'])
@admin_required
def api_admin_users():
    return jsonify([u.to_dict() for u in User.query.order_by(User.id).all()])


@bp.route('/api/admin/users/<int:uid>/reset-password', methods=['POST'])
@admin_required
def api_admin_reset_password(uid):
    user = db.session.get(User, uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    temp = secrets.token_urlsafe(9)
    if not replace_password(user, temp):
        return jsonify({'error': '账号已更新，请重试'}), 409
    db.session.commit()
    return jsonify({'success': True, 'temp_password': temp})


@bp.route('/api/admin/users/<int:uid>/role', methods=['POST'])
@admin_required
def api_admin_set_role(uid):
    user = db.session.get(User, uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if user.id == g.user.id:
        return jsonify({'error': '不能修改自己的角色'}), 400
    role = (request.get_json(silent=True) or {}).get('role')
    if role not in ('admin', 'user'):
        return jsonify({'error': '无效角色'}), 400
    user.role = role
    db.session.commit()
    return jsonify({'success': True})
