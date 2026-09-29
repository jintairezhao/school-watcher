"""Administrator-owned AI configuration for the whole deployment."""
from flask import Blueprint, jsonify, render_template, request
from backend.auth import admin_required
from backend.database.db import db
from backend.ai import configuration

bp = Blueprint('ai_settings', __name__)


@bp.route('/admin/ai-usage')
@admin_required
def usage_page():
    return render_template('admin_ai_usage.html', admin_section='ai-usage')


def _call(action):
    try:
        result = action()
        response = jsonify(result)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except (ValueError, TypeError, OverflowError) as exc:
        db.session.rollback()
        return jsonify(error=str(exc), error_code=getattr(exc, 'code', 'configuration_invalid')), 400


@bp.route('/api/admin/ai', methods=['GET'])
@admin_required
def settings():
    return _call(configuration.public_settings)


@bp.route('/api/admin/ai/usage', methods=['GET'])
@admin_required
def usage():
    from backend.ai.usage import usage_dashboard
    return _call(lambda: usage_dashboard(request.args.get('days', 30), request.args.get('offset', 0)))


@bp.route('/api/admin/ai/profiles', methods=['POST'])
@admin_required
def create_profile():
    return _call(lambda: configuration.save_profile(request.get_json(silent=True)))


@bp.route('/api/admin/ai/profiles/<int:profile_id>', methods=['PUT', 'DELETE'])
@admin_required
def update_profile(profile_id):
    return _call(lambda: configuration.delete_profile(profile_id) if request.method == 'DELETE'
                 else configuration.save_profile(request.get_json(silent=True), profile_id))


@bp.route('/api/admin/ai/profiles/<int:profile_id>/test', methods=['POST'])
@admin_required
def test_profile(profile_id):
    # This is the only settings operation allowed to call a paid provider.
    from backend.auth.rate_limit import check_rate_limit
    from flask import g
    if not check_rate_limit(f'ai-test:{g.user.id}', 10, 3600)[0]:
        return jsonify(error='测试较频繁，请稍后再试'), 429
    return _call(lambda: configuration.test_profile(profile_id))


@bp.route('/api/admin/ai/bindings/<purpose>', methods=['PUT'])
@admin_required
def bind_profile(purpose):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or type(payload.get('profile_id')) is not int:
        return jsonify(error='请选择服务配置'), 400
    return _call(lambda: configuration.bind_profile(purpose, payload['profile_id']))


@bp.route('/api/admin/ai/limits', methods=['PUT'])
@admin_required
def limits():
    return _call(lambda: configuration.save_limits(request.get_json(silent=True)))


@bp.route('/api/admin/ai/executions', methods=['GET'])
@admin_required
def executions():
    return _call(lambda: configuration.execution_history(request.args.get('limit', 50)))


@bp.route('/api/admin/ai/executions/reconcile', methods=['POST'])
@admin_required
def reconcile_execution():
    from backend.ai.runtime import reconcile_usage
    payload = request.get_json(silent=True)
    if (not isinstance(payload, dict) or not isinstance(payload.get('execution_id'), str)
            or type(payload.get('total_tokens')) is not int):
        return jsonify(error='请提供执行标识和核对后的实际 token 用量'), 400
    def action():
        result = reconcile_usage(payload['execution_id'], payload['total_tokens'])
        return {'execution_id': payload['execution_id'], 'status': result['status'],
                'usage': result['usage'], 'error_code': result['error_code']}
    return _call(action)
