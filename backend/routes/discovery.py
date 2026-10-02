"""站点发现 API"""
import json
import logging

from flask import Blueprint, request, jsonify, Response, current_app

from backend.database.db import db
from backend.database.models import School, Department
from backend.auth import admin_required

logger = logging.getLogger(__name__)

bp = Blueprint('discovery', __name__)


@bp.route('/api/schools/<int:school_id>/discover', methods=['POST'])
@admin_required
def api_discover_school(school_id):
    db.get_or_404(School, school_id)
    from backend.services.tasks import enqueue
    task = enqueue('discover', school_id, {'school_id': school_id, 'refresh': True,
                                         'ai_assist': True, 'trigger': 'manual_changes'})
    return jsonify(success=True, session_id=str(task.id), task_id=task.id, message='来源检查已加入队列'), 202


@bp.route('/api/schools/<int:school_id>/discover/events')
@admin_required
def api_discover_events(school_id):
    from backend.routes.scrape import event_response
    return event_response('discover', school_id)


@bp.route('/api/schools/<int:school_id>/discover/status')
@admin_required
def api_discover_status(school_id):
    from backend.services.tasks import task_status
    return jsonify(task_status('discover', school_id))


@bp.route('/api/schools/<int:school_id>/discover/apply', methods=['POST'])
@admin_required
def api_apply_discovery(school_id):
    """应用发现结果：将用户确认的部门写入数据库"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='来源列表格式不正确'), 400
    departments = data.get('departments', [])

    if not departments:
        return jsonify({'error': '未提供要应用的部门列表'}), 400

    from backend.services.source_governance import queue_source_review
    try:
        results = [queue_source_review(school_id, config) for config in departments]
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(success=True, created=0, proposals=results,
                   message=f'{len(results)} 个候选栏目已加入核实队列，通过检查后生效'), 202
