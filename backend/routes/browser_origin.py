"""Private browser admission callback; never expose through the public proxy."""
import re
from flask import Blueprint, jsonify, request
from backend.scraper.http_client import validate_public_url
from backend.services.runtime_leases import reserve_origin, release_origin

bp = Blueprint('browser_origin', __name__)


@bp.post('/internal/browser-origin/<action>')
def origin(action):
    # Authentication is performed before normal user/CSRF hooks in auth.py.
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
        return jsonify(error='invalid execution identity'), 400
    if action == 'release':
        release_origin(token)
        return jsonify(released=True)
    if action != 'reserve':
        return jsonify(error='unknown action'), 404
    try:
        validate_public_url(data.get('url', ''))
        ttl = max(1, min(650, int(data.get('ttl', 120))))
        permit, wait = reserve_origin(data['url'], 'browser-service', token=token, ttl=ttl)
    except (ValueError, TypeError, OSError):
        return jsonify(error='invalid public destination'), 400
    return jsonify(allowed=permit is not None, retry_after=wait)
