"""Local opt-in tuning, with the existing desktop capability and CSRF hooks."""
from flask import Blueprint, abort, jsonify, render_template, request
from backend.services import discovery_tuning as service

bp = Blueprint('discovery_tuning', __name__)


@bp.before_request
def local_surface():
    # No account or administrator gate. Keep this local desktop surface off
    # remotely reachable deployments; the normal desktop/CSRF hooks still run.
    if request.remote_addr not in ('127.0.0.1', '::1'):
        abort(404)


@bp.get('/discovery-tuning')
def page():
    return render_template('discovery_tuning.html')


@bp.get('/api/discovery-tuning')
def state():
    try:
        return jsonify(service.state()), 200, {'Cache-Control': 'no-store'}
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@bp.post('/api/discovery-tuning/<action>')
def action(action):
    try:
        payload = request.get_json(silent=True)
        result = service.suggest(payload) if action == 'suggest' else service.change(action, payload)
        return jsonify(result), 200, {'Cache-Control': 'no-store'}
    except service.TuningConflict as exc:
        return jsonify(error=str(exc)), 409
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400
