"""学校与部门管理 API"""
import logging

from flask import Blueprint, request, jsonify, g

from backend.database.db import db
from backend.database.models import School, Department
from backend.auth import admin_required, login_required

logger = logging.getLogger(__name__)

bp = Blueprint('schools', __name__)


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
    from backend.routes.subscriptions import start_background_scrape
    from backend.database.models import Subscription

    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': '学校名称不能为空'}), 400

    allowed, _ = check_rate_limit(f'submit:{g.user.id}', 3, 3600)
    if not allowed:
        return jsonify({'error': '提交学校过于频繁，请稍后再试'}), 429
    allowed, _ = check_rate_limit(f'submit:ip:{request.remote_addr}', 10, 3600)
    if not allowed:
        return jsonify({'error': '提交学校过于频繁，请稍后再试'}), 429

    name = data['name'].strip()
    existing = School.query.filter(db.func.lower(School.name) == name.lower()).first()
    if existing:
        return jsonify({'error': '学校已存在，请在学校目录中订阅它',
                        'school_id': existing.id}), 409

    school = School(name=name, url=data.get('url', ''),
                    submitted_by=g.user.id, config=data.get('config', '{}'))
    db.session.add(school)
    db.session.flush()  # 获取 school.id

    # 默认部门，选择器留空 → 首抓时自动探测
    base_url = data.get('url', '').strip()
    db.session.add(Department(
        school_id=school.id, name='通知公告',
        list_url=base_url, list_selector='', title_selector='',
        link_selector='', date_selector='', content_selector=''))

    # 提交者自动订阅 → subscriber_count=1 → 进入抓取范围
    db.session.add(Subscription(user_id=g.user.id, school_id=school.id))
    db.session.execute(
        db.update(School).where(School.id == school.id)
        .values(subscriber_count=1))
    db.session.commit()

    start_background_scrape(school.id)
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

    data = request.get_json()
    if data.get('name'):
        school.name = data['name']
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
    """添加部门"""
    data = request.get_json()
    if not data or not data.get('name') or not data.get('school_id'):
        return jsonify({'error': '部门名称和学校ID不能为空'}), 400

    dept = Department(
        school_id=data['school_id'],
        name=data['name'],
        list_url=data.get('list_url', ''),
        list_selector=data.get('list_selector', ''),
        title_selector=data.get('title_selector', ''),
        link_selector=data.get('link_selector', ''),
        date_selector=data.get('date_selector', ''),
        content_selector=data.get('content_selector', ''),
        group_name=data.get('group_name', ''),
    )
    db.session.add(dept)
    db.session.commit()
    logger.info(f"已添加部门: {dept.name}")
    return jsonify(dept.to_dict()), 201


@bp.route('/api/departments/<int:dept_id>', methods=['PUT'])
@admin_required
def api_update_department(dept_id):
    """更新部门信息"""
    dept = db.session.get(Department, dept_id)
    if not dept:
        return jsonify({'error': '部门不存在'}), 404

    data = request.get_json()
    selector_fields = ['list_selector', 'title_selector', 'link_selector',
                       'date_selector', 'content_selector']
    old_selectors = {f: getattr(dept, f) for f in selector_fields}
    for field in ['name', 'list_url', 'list_selector', 'title_selector',
                  'link_selector', 'date_selector', 'content_selector',
                  'group_name']:
        if field in data:
            setattr(dept, field, data[field])

    db.session.commit()

    # 人工改选择器留痕（审计日志），并清除该部门的复核标记
    if any(f in data and data[f] != old_selectors[f] for f in selector_fields):
        from backend.scraper.selector_monitor import (
            record_selector_change, clear_review)
        record_selector_change(
            dept, old_selectors,
            {f: getattr(dept, f) for f in selector_fields},
            'human', '管理界面修改')
        clear_review(dept)
    return jsonify(dept.to_dict())


@bp.route('/api/selector-audit', methods=['GET'])
@admin_required
def api_selector_audit():
    """选择器审计：变更日志 + 待人工复核列表"""
    from backend.scraper.selector_monitor import get_audit_changes, get_review_list
    return jsonify({
        'changes': get_audit_changes(50),
        'review': get_review_list(),
    })


@bp.route('/api/departments/<int:dept_id>/selector-rollback', methods=['POST'])
@admin_required
def api_selector_rollback(dept_id):
    """回滚部门选择器到审计日志中最近一次变更前的状态"""
    dept = db.session.get(Department, dept_id)
    if not dept:
        return jsonify({'error': '部门不存在'}), 404
    from backend.scraper.selector_monitor import rollback_selectors
    if rollback_selectors(dept):
        return jsonify({'success': True, 'message': f'已回滚 {dept.name} 的选择器',
                        'department': dept.to_dict()})
    return jsonify({'success': False, 'error': '该部门没有可回滚的变更记录'}), 404


@bp.route('/api/departments/<int:dept_id>', methods=['DELETE'])
@admin_required
def api_delete_department(dept_id):
    """删除部门"""
    dept = db.session.get(Department, dept_id)
    if not dept:
        return jsonify({'error': '部门不存在'}), 404

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

    try:
        from backend.scraper.engine import _fetch_html
        html = _fetch_html(url)
        if not html:
            return jsonify({'error': '无法获取页面内容，请检查URL是否正确'}), 400

        from backend.scraper.detectors.list_detector import test_selectors
        result = test_selectors(html, url, list_selector,
                                title_selector, link_selector, date_selector)
        return jsonify(result)
    except Exception as e:
        logger.error(f"选择器测试失败: {e}")
        return jsonify({'error': f'测试失败: {str(e)}'}), 500
