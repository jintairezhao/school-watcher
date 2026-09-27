"""Collection APIs enqueue durable work; progress is read across processes."""
import json
from flask import Blueprint, request, jsonify, Response, g
from backend.database.db import db
from backend.database.models import School
from backend.auth import admin_required, login_required
from backend.services.tasks import enqueue, task_status

bp = Blueprint('scrape', __name__)


@bp.route('/api/inbox/refresh', methods=['GET', 'POST'])
@login_required
def inbox_refresh():
    from backend.services.inbox_refresh import (resolve_scope, queue_sources, refresh_status,
                                                restore_refresh_status, remember_refresh_scope,
                                                refresh_tracking_baseline)
    if request.method == 'GET' and request.args.get('resume') == '1':
        response = jsonify(restore_refresh_status(g.user.id))
        response.headers['Cache-Control'] = 'no-store'
        return response
    if request.method == 'POST':
        if not g.user.is_admin:
            return jsonify(error='只有管理员可以手动抓取；刷新页面即可查看已有通知。'), 403
        payload = request.get_json(silent=True)
    else:
        try:
            payload = {'scope': request.args.get('scope', 'current'),
                       'school_id': int(request.args['school']) if request.args.get('school') else None,
                       'department_ids': [int(i) for i in request.args.getlist('dept')],
                       'group': request.args.get('group', '')}
        except ValueError:
            return jsonify(error='刷新范围格式不正确'), 400
    departments = resolve_scope(g.user.id, payload)
    if request.method == 'POST':
        from backend.auth.rate_limit import check_rate_limit
        if not check_rate_limit(f'inbox-refresh:{g.user.id}', 30, 60)[0]:
            return jsonify(error='操作较频繁，请稍后刷新；正在进行的更新会继续。'), 429
        baseline = refresh_tracking_baseline(g.user.id)
        queue_sources(departments, manual=True)
        remember_refresh_scope(g.user.id, departments, payload.get('scope', 'current'), baseline=baseline)
        data = restore_refresh_status(g.user.id)
    else:
        data = refresh_status(departments)
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store'
    return response, 202 if request.method == 'POST' else 200


@bp.route('/api/inbox/sync', methods=['POST'])
@login_required
def inbox_sync():
    """An open-site check can reuse work, but cannot bypass the shared interval."""
    from backend.database.models import BackgroundTask
    from backend.services.inbox_refresh import (resolve_scope, queue_sources,
        refresh_tracking_baseline, remember_refresh_scope, restore_refresh_status)
    from backend.auth.rate_limit import check_rate_limit
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        payload = {'scope': 'all', **payload}
    departments = resolve_scope(g.user.id, payload)
    if not check_rate_limit(f'inbox-sync:{g.user.id}', 30, 60)[0]:
        return jsonify(error='检查较频繁，请稍后重试；已有通知仍可查看。'), 429
    baseline = refresh_tracking_baseline(g.user.id)
    jobs = queue_sources(departments)
    if jobs:
        tracked_ids = {t.payload.get('department_id') for t in
                       BackgroundTask.query.filter(BackgroundTask.id.in_(jobs))}
        remember_refresh_scope(g.user.id, [d for d in departments if d.id in tracked_ids],
                               payload['scope'], baseline=baseline)
    data = restore_refresh_status(g.user.id)
    data['scheduled'] = len(jobs)
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store'
    return response, 202 if jobs else 200


def event_response(kind, key):
    data = task_status(kind, key)
    event = dict(data, type=data['status'] if data['status'] in ('completed', 'failed') else 'phase',
                 progress_pct=data.get('progress', 0), current_phase=data.get('phase', 'pending'))
    return Response('retry: 2000\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n',
                    mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/api/scrape/<int:school_id>', methods=['POST'])
@admin_required
def api_trigger_scrape(school_id):
    db.get_or_404(School, school_id)
    task = enqueue('scrape', school_id, {'school_id': school_id, 'manual': True},
                   min_interval=60, expedite=True)
    return jsonify(success=True, session_id=str(task.id), task_id=task.id, message='已加入同步队列'), 202


@bp.route('/api/scrape/<int:school_id>/events')
@admin_required
def api_scrape_events(school_id):
    return event_response('scrape', school_id)


@bp.route('/api/scrape/<int:school_id>/status')
@admin_required
def api_scrape_status(school_id):
    return jsonify(task_status('scrape', school_id))


@bp.route('/api/scrape/all', methods=['POST'])
@admin_required
def api_trigger_scrape_all():
    schools = School.query.filter(School.enabled.is_(True), School.subscriber_count > 0).all()
    sessions = {s.id: str(enqueue('scrape', s.id, {'school_id': s.id, 'manual': True},
                                 min_interval=60, expedite=True).id) for s in schools}
    return jsonify(success=True, session_ids=sessions, message='已将订阅中的学校加入同步队列'), 202


@bp.route('/api/summarize', methods=['POST'])
@admin_required
def api_trigger_summarize():
    from backend.services.summaries import enqueue_batch
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='请选择需要生成摘要的通知'), 400
    try:
        result = enqueue_batch(data.get('ids'), requested_by=g.user.id)
        return jsonify(success=True, **result, message='所选通知的摘要已加入队列'), 202
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400
