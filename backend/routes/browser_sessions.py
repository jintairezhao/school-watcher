"""Administrator-only manual access verification. CSRF uses the application's global hook."""
from flask import Blueprint, g, jsonify, render_template, request

from backend.auth import admin_required
from backend.database.db import db
from backend.database.models import VerificationSession
from backend.services import browser_sessions as service

bp = Blueprint('browser_sessions', __name__)


@bp.errorhandler(service.BrowserSessionError)
def runtime_error(exc):
    return jsonify(error=str(exc), code=exc.code), exc.status


@bp.get('/admin/access-verification')
@admin_required
def page():
    return render_template('browser_verification.html')


@bp.get('/api/admin/browser-sessions')
@admin_required
def list_sessions():
    rows = VerificationSession.query.order_by(VerificationSession.created_at.desc()).limit(100).all()
    return jsonify(sessions=[service.public_record(row) for row in rows])


@bp.get('/api/admin/browser-sessions/<ident>')
@admin_required
def session_status(ident):
    return jsonify(service.status(db.get_or_404(VerificationSession, ident)))


@bp.post('/api/admin/browser-sessions/<ident>/<action>')
@admin_required
def session_action(ident, action):
    row = db.get_or_404(VerificationSession, ident)
    if action == 'open':
        value = service.open_session(row, g.user.id)
    elif action in ('verify', 'cancel', 'ticket'):
        value = service.act(row, action)
    else:
        return jsonify(error='操作不存在'), 404
    return jsonify(value)


@bp.get('/api/admin/browser-access/auth')
@admin_required
def socket_authorization():
    # Nginx auth_request forwards cookies for every initial WebSocket handshake.
    return '', 204
