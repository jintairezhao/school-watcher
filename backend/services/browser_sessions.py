"""Persistent verification records bridge the admin UI to disposable browser sessions."""
from datetime import datetime, timedelta
import os
import secrets
from urllib.parse import quote

import requests
from flask import current_app

from backend.database.db import db
from backend.database.models import VerificationSession


class BrowserSessionError(Exception):
    def __init__(self, message, status=503, code='browser_unavailable'):
        super().__init__(message)
        self.status, self.code = status, code


def runtime_request(method, path, payload=None, generation=None):
    base = current_app.config.get('BROWSER_SERVICE_URL') or os.environ.get('WATCHER_BROWSER_URL', 'http://127.0.0.1:8765')
    token = current_app.config.get('BROWSER_SERVICE_TOKEN') or os.environ.get('WATCHER_BROWSER_TOKEN', '')
    if not token:
        raise BrowserSessionError('浏览器服务尚未配置，请检查启动设置')
    headers = {'X-Watcher-Token': token}
    if generation:
        headers['X-Session-Generation'] = generation
    # Routes have already committed ownership changes; release any read transaction
    # while the external browser runs, on both SQLite and PostgreSQL.
    db.session.rollback()
    try:
        with requests.Session() as client:
            client.trust_env = False  # Internal service traffic never uses a user's proxy.
            response = client.request(method, base.rstrip('/') + path, json=payload,
                headers=headers, timeout=(3, 100 if path == '/v1/manual' else 100 if path.endswith('/verify') else 10))
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BrowserSessionError('浏览器服务暂时不可用，请稍后重试') from exc
    if not response.ok:
        error = data.get('error', {})
        raise BrowserSessionError(error.get('message', '浏览器操作未完成'), response.status_code,
            error.get('code', 'browser_unavailable'))
    return data


def public_record(row):
    return {'id': row.id, 'source_id': row.source_id, 'url': row.url,
        'status': row.status, 'error': row.error_message or '',
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'expires_at': row.expires_at.isoformat() if row.expires_at else None}


def _save_generation(row, generation, values, expected=None):
    query = VerificationSession.query.filter_by(id=row.id, generation=generation)
    if expected:
        query = query.filter(VerificationSession.status.in_(expected))
    changed = query.update(values, synchronize_session=False)
    db.session.commit()
    db.session.refresh(row)
    if not changed:
        raise BrowserSessionError('验证会话已更新，请刷新后重试', 409, 'stale_session')


def _refresh(row):
    if row.status not in ('opening', 'active'):
        return None
    if row.expires_at and row.expires_at <= datetime.utcnow():
        row.status, row.completed_at = 'expired', datetime.utcnow()
        db.session.commit()
        return None
    if row.status == 'opening':
        return None
    generation, runtime_id = row.generation, row.runtime_id
    try:
        value = runtime_request('GET', '/v1/manual/' + quote(row.id, safe=''), generation=generation)
    except BrowserSessionError as exc:
        if exc.code in ('session_missing', 'stale_session'):
            _save_generation(row, generation, {'status': 'expired', 'error_message': '验证窗口已关闭，请重新打开',
                'completed_at': datetime.utcnow()}, ('active',))
            return None
        raise
    changes = {'status': value['state']}
    if value['runtime_id'] != runtime_id:
        changes = {'status': 'expired', 'error_message': '浏览器服务已重启，请重新打开验证'}
    _save_generation(row, generation, changes, ('active',))
    return value


def status(row):
    runtime = _refresh(row)
    result = public_record(row)
    if runtime:
        result.update(remote=runtime['remote'], remaining_seconds=runtime['remaining_seconds'])
    return result


def open_session(row, user_id):
    _refresh(row)
    if row.status == 'active':
        return status(row)
    generation = secrets.token_hex(32)
    # Opening is committed before the slow request. A second admin cannot replace ownership.
    old_generation = row.generation
    conditions = [VerificationSession.id == row.id, VerificationSession.status != 'opening']
    conditions.append(VerificationSession.generation == old_generation)
    changed = VerificationSession.query.filter(*conditions).update({'status': 'opening',
        'generation': generation, 'created_by': user_id, 'expires_at': datetime.utcnow() + timedelta(minutes=10),
        'completed_at': None, 'error_code': '', 'error_message': ''}, synchronize_session=False)
    db.session.commit()
    if not changed:
        raise BrowserSessionError('验证窗口正在打开，请稍后刷新', 409, 'session_busy')
    db.session.refresh(row)
    payload = {**(getattr(row, 'request_payload', None) or {}),
        'id': row.id, 'generation': generation, 'url': row.url, 'source_id': row.source_id}
    payload.setdefault('purpose', 'list')
    payload.setdefault('timeout_seconds', 45)
    payload.setdefault('policy_version', '1')
    try:
        value = runtime_request('POST', '/v1/manual', payload)
    except BrowserSessionError as exc:
        _save_generation(row, generation, {'status': 'failed', 'error_code': exc.code,
            'error_message': str(exc)}, ('opening',))
        raise
    _save_generation(row, generation, {'runtime_id': value['runtime_id'], 'status': value['state']}, ('opening',))
    return {**public_record(row), 'remote': value['remote'], 'remaining_seconds': value['remaining_seconds']}


def act(row, action):
    _refresh(row)
    if row.status != 'active':
        raise BrowserSessionError('验证窗口已结束，请重新打开', 409, 'session_closed')
    generation = row.generation
    value = runtime_request('POST', '/v1/manual/' + quote(row.id, safe='') + '/' + action,
        {'generation': generation})
    if action == 'ticket':
        return {'websocket_path': '/browser-access/' + quote(row.id, safe='') + '/websocket?ticket=' + quote(value['ticket'], safe='')}
    _save_generation(row, generation, {'status': value['state'], 'completed_at': datetime.utcnow()}, ('active',))
    if row.status == 'verified':
        from backend.services.tasks import resume_verification
        resume_verification(row.source_id)
    return public_record(row)
