"""站点发现 API"""
import json
import logging
import threading

from flask import Blueprint, request, jsonify, Response, current_app

from backend.database.db import db
from backend.database.models import School, Department
from backend.services.discovery import run_discovery_in_background
from backend.auth import admin_required

logger = logging.getLogger(__name__)

bp = Blueprint('discovery', __name__)


@bp.route('/api/schools/<int:school_id>/discover', methods=['POST'])
@admin_required
def api_discover_school(school_id):
    """触发站点发现（后台执行，返回 session_id）"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404

    from backend.scraper.discovery.discovery_progress import create_session
    session_obj = create_session(school_id, school.name)

    # 捕获 app 实例传给后台线程（新线程里没有请求上下文，current_app 不可用）
    app_obj = current_app._get_current_object()
    thread = threading.Thread(
        target=run_discovery_in_background,
        args=(school, session_obj, app_obj),
        daemon=True,
    )
    thread.start()

    return jsonify({
        'success': True,
        'session_id': session_obj.session_id,
        'message': '发现已启动，请监听 SSE 事件',
    })


@bp.route('/api/schools/<int:school_id>/discover/events')
@admin_required
def api_discover_events(school_id):
    """SSE 端点：实时推送发现进度"""
    from backend.scraper.discovery.discovery_progress import get_school_session

    session_obj = get_school_session(school_id)
    if not session_obj:
        def no_session():
            yield f"data: {json.dumps({'type': 'error', 'message': '没有活跃的发现会话'})}\n\n"
        return Response(no_session(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return Response(
        session_obj.events_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@bp.route('/api/schools/<int:school_id>/discover/status')
@admin_required
def api_discover_status(school_id):
    """获取发现会话状态（SSE 轮询回退）"""
    from backend.scraper.discovery.discovery_progress import get_school_session

    session_obj = get_school_session(school_id)
    if not session_obj:
        return jsonify({'status': 'none', 'message': '没有活跃的发现会话'})

    return jsonify(session_obj.to_dict())


@bp.route('/api/schools/<int:school_id>/discover/apply', methods=['POST'])
@admin_required
def api_apply_discovery(school_id):
    """应用发现结果：将用户确认的部门写入数据库"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404

    data = request.get_json() or {}
    departments = data.get('departments', [])

    if not departments:
        return jsonify({'error': '未提供要应用的部门列表'}), 400

    # 删除默认的"通知公告"占位部门
    default_dept = Department.query.filter_by(
        school_id=school_id, name='通知公告'
    ).first()
    if default_dept:
        # 检查是否只有占位部门（无选择器配置）
        if not default_dept.list_selector:
            db.session.delete(default_dept)

    created = 0
    for dept_data in departments:
        # 跳过已存在的部门（同名校验）
        existing = Department.query.filter_by(
            school_id=school_id, name=dept_data['name']
        ).first()
        if existing:
            # 更新选择器
            for field in ['list_url', 'list_selector', 'title_selector',
                          'link_selector', 'date_selector', 'content_selector',
                          'group_name']:
                if field in dept_data:
                    setattr(existing, field, dept_data[field])
            created += 1
            continue

        dept = Department(
            school_id=school_id,
            name=dept_data['name'],
            list_url=dept_data.get('list_url', ''),
            list_selector=dept_data.get('list_selector', ''),
            title_selector=dept_data.get('title_selector', ''),
            link_selector=dept_data.get('link_selector', ''),
            date_selector=dept_data.get('date_selector', ''),
            content_selector=dept_data.get('content_selector', ''),
            group_name=dept_data.get('group_name', ''),
        )
        db.session.add(dept)
        created += 1

    db.session.commit()

    # 保存选择器到知识库
    from backend.scraper.selector.selector_store import save_discovered_selectors
    save_discovered_selectors(school_id, departments)

    # 更新发现会话
    from backend.scraper.discovery.discovery_progress import get_school_session
    session_obj = get_school_session(school_id)
    if session_obj:
        session_obj.complete({'applied_departments': created})

    logger.info(f"已应用 {created} 个发现部门到学校 {school.name}")
    return jsonify({
        'success': True,
        'created': created,
        'message': f'已添加 {created} 个部门',
    })
