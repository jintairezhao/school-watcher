"""订阅 API — 订阅驱动抓取

规则：有人订阅才抓取；0→1 复活立即后台首抓；归零暂停（调度过滤生效）。
subscriber_count 为缓存列，在订阅事务内用 SQL 算术维护。
"""
import logging

from flask import Blueprint, g, jsonify, request, current_app, render_template, redirect, url_for, flash

from backend.database.db import db
from backend.database.models import School, Subscription, Department, ScrapeLog
from backend.database.dialect import insert
from backend.auth import login_required

logger = logging.getLogger(__name__)

bp = Blueprint('subscriptions', __name__)
def start_background_scrape(school_id):
    """Persist work; never start a crawler in a request process."""
    from backend.services.tasks import enqueue
    return enqueue('scrape', school_id, {'school_id': school_id})


def subscribe_school(school, user_id):
    result = db.session.execute(insert(Subscription).values(
        user_id=user_id, school_id=school.id).on_conflict_do_nothing(
            index_elements=['user_id', 'school_id']))
    created = result.rowcount == 1
    revived = created and (school.subscriber_count or 0) == 0
    if created:
        db.session.execute(db.update(School).where(School.id == school.id).values(
            subscriber_count=School.subscriber_count + 1))
    db.session.commit()
    if revived:
        start_background_scrape(school.id)
    return revived


@bp.route('/api/subscriptions', methods=['GET'])
@login_required
def api_my_subscriptions():
    subs = (Subscription.query.filter_by(user_id=g.user.id)
            .order_by(Subscription.created_at).all())
    return jsonify([{
        'school_id': s.school_id,
        'school_name': s.school.name,
        'department_ids': s.department_ids,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    } for s in subs])


@bp.route('/api/subscriptions', methods=['POST'])
@login_required
def api_subscribe():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or type(data.get('school_id')) is not int:
        return jsonify(error='学校编号无效'), 400
    school = db.session.get(School, data.get('school_id') or 0)
    if not school or not school.enabled:
        return jsonify({'error': '学校不存在或已下架'}), 404

    revived = subscribe_school(school, g.user.id)

    fresh = db.session.get(School, school.id)
    return jsonify({'success': True, 'subscribed': True,
                    'subscriber_count': fresh.subscriber_count or 0,
                    'scrape_started': revived})


@bp.route('/api/subscriptions/<int:school_id>', methods=['DELETE'])
@login_required
def api_unsubscribe(school_id):
    sub = Subscription.query.filter_by(user_id=g.user.id,
                                       school_id=school_id).first()
    if not sub:
        return jsonify({'success': True, 'subscribed': False})

    deleted = db.session.execute(db.delete(Subscription).where(
        Subscription.user_id == g.user.id, Subscription.school_id == school_id))
    if deleted.rowcount:
        db.session.execute(
            db.update(School).where(School.id == school_id)
            .values(subscriber_count=db.case((School.subscriber_count > 0, School.subscriber_count - 1), else_=0)))
    db.session.commit()

    school = db.session.get(School, school_id)
    if (school.subscriber_count or 0) == 0:
        logger.info(f"[订阅] {school.name} 订阅归零，暂停抓取")
    return jsonify({'success': True, 'subscribed': False,
                    'subscriber_count': school.subscriber_count or 0})


@bp.route('/subscriptions/<int:school_id>', methods=['GET', 'POST'])
@login_required
def manage_sources(school_id):
    from backend.services.inbox import source_groups
    school = db.get_or_404(School, school_id)
    sub = Subscription.query.filter_by(user_id=g.user.id, school_id=school_id).first()
    if not sub or not school.enabled:
        flash('请先订阅这所学校', 'error')
        return redirect(url_for('pages.explore'))
    departments = school.departments.order_by(Department.id).all()
    if request.method == 'POST':
        mode = request.form.get('mode')
        ids = request.form.getlist('department', type=int)
        valid = {d.id for d in departments if d.name.upper() != 'DAILY NEWS'}
        if mode not in ('all', 'selected') or (mode == 'selected' and (not ids or set(ids) - valid)):
            flash('至少选择一个本校栏目，或选择全部栏目', 'error')
        else:
            sub.department_ids = None if mode == 'all' else sorted(set(ids))
            db.session.commit()
            start_background_scrape(school.id)
            flash('栏目订阅已保存', 'success')
            return redirect(url_for('pages.index', school=school.id))
    latest = ScrapeLog.query.filter_by(school_id=school.id).order_by(ScrapeLog.id.desc()).first()
    from backend.services.source_inventory import Inventory, DEFAULT_PATH, site_key
    from backend.services.source_relationships import SourceRelationships
    from backend.services.runtime_catalog import runtime_catalog, relationships_for
    inventory = runtime_catalog()
    key = site_key(school.url)
    relationships = relationships_for(inventory, key)
    from backend.services.source_governance import school_governance_status
    return render_template('sources.html', school=school, subscription=sub,
                           onboarding=school_governance_status(school.id),
                           groups=source_groups(departments), latest=latest,
                           source_paths={d.id: relationships.paths_for(d.list_url) for d in departments})


@bp.route('/subscriptions/<int:school_id>/sources', methods=['POST'])
@login_required
def add_official_source(school_id):
    from backend.scraper.http_client import validate_public_url, same_school_url
    from backend.auth.rate_limit import check_rate_limit
    school = db.get_or_404(School, school_id)
    sub = Subscription.query.filter_by(user_id=g.user.id, school_id=school_id).first()
    if not sub or not school.enabled:
        return jsonify(error='请先订阅已上架学校'), 403
    name = request.form.get('name', '').strip()
    url = request.form.get('url', '').strip()
    group = request.form.get('group', '').strip()
    try:
        if not name or len(name) > 200 or len(group) > 200 or len(url) > 1000:
            raise ValueError('请填写有效的栏目名称和地址')
        validate_public_url(url)
        if not same_school_url(url, school.url):
            raise ValueError('栏目地址必须属于这所学校的官网域名')
        if not check_rate_limit(f'add-source:{g.user.id}', 10, 3600)[0]:
            raise ValueError('添加过于频繁，请稍后再试')
        from backend.services.source_governance import queue_source_review
        result = queue_source_review(school.id, {'name': name, 'list_url': url, 'group_name': group},
                                     requested_by=g.user.id, subscribe=True)
        flash(result['message'] if result['state'] == 'activated' else
              '栏目已提交核实，通过检查后会自动加入你的订阅', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('subscriptions.manage_sources', school_id=school.id))
