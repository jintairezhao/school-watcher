"""Only an explicit authenticated POST may schedule a shared paid summary."""
from flask import Blueprint, g, jsonify, request

from backend.auth.decorators import admin_required, login_required
from backend.database.models import Announcement
from backend.services.announcement_sources import source_expression
from backend.services.summaries import enqueue_batch, request_summary, summary_status

bp = Blueprint('summaries', __name__)


@bp.route('/api/announcements/<int:ann_id>/summary', methods=['GET', 'POST'])
@login_required
def announcement_summary(ann_id):
    ann = Announcement.query.filter(Announcement.id == ann_id, source_expression()).first()
    if ann is None:
        return jsonify(error='通知不存在或来源已下架'), 404
    if request.method == 'GET':
        return jsonify(summary_status(ann))
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict) or set(body) - {'force'} or type(body.get('force', False)) is not bool:
        return jsonify(error='摘要请求格式无效'), 400
    force = body.get('force', False)
    if force and not g.user.is_admin:
        return jsonify(error='只有管理员可以重新生成公共摘要'), 403
    from backend.auth.rate_limit import check_rate_limit
    if not check_rate_limit(f'summary:user:{g.user.id}', 120, 3600)[0]:
        return jsonify(error='摘要请求较频繁，请稍后重试'), 429
    try:
        result = request_summary(ann, requested_by=g.user.id, force=force)
    except ValueError as exc:
        return jsonify(error=str(exc), error_code=getattr(exc, 'code', 'ai_not_configured')), 409
    return jsonify(result), 202 if result['status'] in ('pending', 'waiting_content', 'running') else 200


@bp.post('/api/summaries/batch')
@admin_required
def summary_batch():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {'ids', 'force'} or type(body.get('force', False)) is not bool:
        return jsonify(error='请选择通知后生成摘要'), 400
    try:
        result = enqueue_batch(body.get('ids'), requested_by=g.user.id, force=body.get('force', False))
    except ValueError as exc:
        return jsonify(error=str(exc), error_code=getattr(exc, 'code', 'invalid_request')), 400
    return jsonify(result), 202
