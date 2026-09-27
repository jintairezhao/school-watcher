"""学校与部门管理 API"""
import logging

from flask import Blueprint, request, jsonify, g, url_for

from backend.database.db import db
from backend.database.models import School, Department
from backend.auth import admin_required, login_required

logger = logging.getLogger(__name__)

bp = Blueprint('schools', __name__)


@bp.route('/api/catalog/subscribe', methods=['POST'])
@login_required
def subscribe_catalog():
    from backend.services.catalog import find_entry
    from backend.services.school_registry import ensure_school
    from backend.routes.subscriptions import subscribe_school
    from backend.auth.rate_limit import check_rate_limit
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not isinstance(data.get('name'), str):
        return jsonify(error='请选择目录中的学校'), 400
    entry = find_entry(data['name'])
    if not entry:
        return jsonify(error='目录中没有这所学校，请使用补充学校入口'), 404
    if not check_rate_limit(f'catalog:{g.user.id}', 30, 3600)[0]:
        return jsonify(error='订阅操作过于频繁，请稍后再试'), 429
    try:
        school, _created = ensure_school(entry['name'], entry['url'], submitted_by=g.user.id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    if school and not school.enabled:
        return jsonify(error='该学校暂未开放订阅'), 409
    started = subscribe_school(school, g.user.id)
    from backend.services.source_governance import school_governance_status
    return jsonify(success=True, school_id=school.id, subscribed=True, scrape_started=started,
                   onboarding=school_governance_status(school.id),
                   redirect=url_for('subscriptions.manage_sources', school_id=school.id))


@bp.route('/api/schools', methods=['GET'])
@login_required
def api_schools():
    """获取所有学校"""
    schools = School.query.order_by(School.name).all()
    return jsonify([s.to_dict() for s in schools])


@bp.route('/api/schools', methods=['POST'])
@login_required
def api_add_school():
    """提交新学校（登录用户）：创建即提交者自动订阅 → 立即首抓。

    裸校首抓时引擎自动跑站点发现（engine is_bare 分支）。
    重名校不重复创建，引导去订阅。
    """
    from backend.auth.rate_limit import check_rate_limit
    from backend.routes.subscriptions import subscribe_school
    from backend.services.school_registry import ensure_school

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'].strip():
        return jsonify({'error': '学校名称不能为空'}), 400
    name = data['name'].strip()
    if len(name) > 200:
        return jsonify(error='学校名称不能超过 200 字'), 400
    from backend.scraper.http_client import validate_public_url
    base_url = data.get('url')
    try:
        validate_public_url(base_url)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if len(base_url) > 500:
        return jsonify(error='官网地址过长'), 400

    allowed, _ = check_rate_limit(f'submit:{g.user.id}', 3, 3600)
    if not allowed:
        return jsonify({'error': '提交学校过于频繁，请稍后再试'}), 429
    allowed, _ = check_rate_limit(f'submit:ip:{request.remote_addr}', 10, 3600)
    if not allowed:
        return jsonify({'error': '提交学校过于频繁，请稍后再试'}), 429

    try:
        school, created = ensure_school(name, base_url, submitted_by=g.user.id, origin='submitted')
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    if not created:
        return jsonify({'error': '学校已存在，请在学校目录中订阅它',
                        'school_id': school.id}), 409
    # An unexamined homepage is not a verified "通知公告" source.
    subscribe_school(school, g.user.id)
    logger.info(f"已提交学校: {school.name}（提交者 {g.user.username}，已订阅并首抓）")
    return jsonify({**school.to_dict(), 'subscribed': True,
                    'scrape_started': True}), 201


@bp.route('/api/schools/suggest-url')
@login_required
def api_suggest_school_url():
    """根据学校名称建议官网URL"""
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'found': False, 'url': None, 'message': '请提供学校名称'})

    from backend.scraper.discovery.university_urls import suggest_url
    url = suggest_url(name)
    if url:
        return jsonify({'found': True, 'url': url, 'message': f'已匹配 {name} 的官网'})
    else:
        return jsonify({'found': False, 'url': None, 'message': f'未找到 {name} 的官网，请手动输入'})


@bp.route('/api/schools/<int:school_id>', methods=['PUT'])
@admin_required
def api_update_school(school_id):
    """更新学校信息"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='学校信息格式无效'), 400
    if 'name' in data and data['name'] != school.name:
        from backend.services.school_registry import rename_school
        try:
            rename_school(school, data['name'])
        except ValueError as exc:
            db.session.rollback()
            return jsonify(error=str(exc)), 409
    if any(field in data and data[field] != getattr(school, field) for field in ('url', 'enabled', 'config')):
        from backend.services.tasks import invalidate_source
        for department in school.departments:
            invalidate_source(department.id)
    if 'url' in data:
        school.url = data['url']
    if 'enabled' in data:
        school.enabled = data['enabled']
    if 'config' in data:
        school.config = data['config']

    db.session.commit()
    return jsonify(school.to_dict())


@bp.route('/api/schools/<int:school_id>', methods=['DELETE'])
@admin_required
def api_delete_school(school_id):
    """删除学校及其所有数据"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404

    name = school.name
    from backend.services.announcement_sources import preserve_shared_articles
    preserve_shared_articles([d.id for d in school.departments])
    db.session.delete(school)
    db.session.commit()
    logger.info(f"已删除学校: {name}")
    return jsonify({'message': f'已删除: {name}'}), 200


@bp.route('/api/schools/<int:school_id>/departments', methods=['GET'])
@admin_required
def api_school_departments(school_id):
    """获取某学校的所有部门"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'error': '学校不存在'}), 404
    depts = school.departments.order_by(Department.name).all()
    return jsonify([d.to_dict() for d in depts])


@bp.route('/api/departments', methods=['POST'])
@admin_required
def api_add_department():
    """Submit a source for independent verification before it becomes active."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('school_id')) is not int:
        return jsonify(error='请提供学校与来源配置'), 400
    if not db.session.get(School, data['school_id']):
        return jsonify(error='学校不存在'), 404
    from backend.services.source_governance import queue_source_review
    try:
        result = queue_source_review(data['school_id'], data, requested_by=g.user.id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(success=True, **result), 202


@bp.route('/api/departments/<int:dept_id>', methods=['PUT'])
@admin_required
def api_update_department(dept_id):
    """Preserve the current working version while reviewing a replacement."""
    dept = db.session.get(Department, dept_id)
    if not dept:
        return jsonify(error='部门不存在'), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='来源配置格式不正确'), 400
    from backend.services.source_governance import queue_source_review, source_config, FIELDS
    candidate = source_config(dept)
    candidate.update({key: value for key, value in data.items() if key in FIELDS})
    try:
        result = queue_source_review(dept.school_id, candidate, department_id=dept.id, requested_by=g.user.id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(success=True, **result), 202


@bp.route('/api/departments/<int:dept_id>', methods=['DELETE'])
@admin_required
def api_delete_department(dept_id):
    """删除部门"""
    dept = db.session.get(Department, dept_id)
    if not dept:
        return jsonify({'error': '部门不存在'}), 404

    from backend.services.announcement_sources import preserve_shared_articles
    preserve_shared_articles([dept.id])
    db.session.delete(dept)
    db.session.commit()
    return jsonify({'message': f'已删除: {dept.name}'}), 200


@bp.route('/api/departments/test-selectors', methods=['POST'])
@admin_required
def api_test_selectors():
    """测试 CSS 选择器，返回匹配样本"""
    data = request.get_json() or {}
    url = data.get('url', '')
    list_selector = data.get('list_selector', '')
    title_selector = data.get('title_selector', 'a')
    link_selector = data.get('link_selector', 'a')
    date_selector = data.get('date_selector', 'span')

    if not url or not list_selector:
        return jsonify({'error': 'URL 和 list_selector 为必填项'}), 400

    from backend.scraper.http_client import validate_public_url
    from backend.services.tasks import enqueue
    import hashlib
    try:
        validate_public_url(url, resolve=False)
        payload = {k: data.get(k, '') for k in ('url', 'list_selector', 'title_selector', 'link_selector', 'date_selector')}
        if any(not isinstance(v, str) or len(v) > 2000 for v in payload.values()):
            raise ValueError('选择器字段无效')
        import json
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        task = enqueue('selectors', key, payload)
        return jsonify(success=True, task_id=task.id, message='测试已加入队列'), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
