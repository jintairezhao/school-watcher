"""统一后台管理（开发者后台，仅 admin）

概览统计 / feature 开关。学校/用户/抓取等操作复用
既有 admin API（schools/account/scrape/settings 蓝图）。
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import func

from backend.auth import admin_required
from backend.database.db import db
from backend.database.models import (Announcement, AppConfig, School,
                                     ScrapeLog, Subscription, User)

bp = Blueprint('admin', __name__)


@bp.get('/api/admin/scrape-logs')
@admin_required
def api_scrape_logs():
    from backend.services.scrape_logs import log_page
    try:
        return jsonify(log_page(request.args))
    except (ValueError, TypeError, KeyError):
        return jsonify(error='记录筛选或分页参数不正确'), 400


@bp.route('/api/admin/scrape-logs/retention', methods=['GET', 'PUT'])
@admin_required
def api_log_retention():
    from backend.services.scrape_logs import retention_days, RETENTION_CHOICES
    if request.method == 'PUT':
        data = request.get_json(silent=True)
        days = data.get('days') if isinstance(data, dict) else None
        if type(days) is not int or days not in RETENTION_CHOICES:
            return jsonify(error='请选择有效的记录保留时长'), 400
        AppConfig.set('scrape_log_retention_days', str(days))
    return jsonify(days=retention_days(), choices=list(RETENTION_CHOICES),
                   message='保留规则已保存，将在每日清理时应用。')


@bp.route('/admin')
@admin_required
def admin_page():
    # 学校管理标签页的列表为服务端渲染（partial 内 for 循环）
    return render_template('admin.html',
                           schools=School.query.order_by(School.name).all())


@bp.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    """后台概览仪表盘数据（单接口聚合，naive utcnow 与存量时间一致）"""
    from backend.scraper.engine import active_schools_query

    now = datetime.utcnow()
    d7 = now - timedelta(days=7)
    h24 = now - timedelta(hours=24)

    # A browser handoff or origin-rate-limit wait can emit many process records
    # before one result. They are not completed collection attempts.
    finished_statuses = ('success', 'partial', 'failed', 'interrupted')
    counts = dict(db.session.query(ScrapeLog.status, func.count(ScrapeLog.id))
                  .filter(ScrapeLog.started_at >= d7, ScrapeLog.status.in_(finished_statuses))
                  .group_by(ScrapeLog.status).all())
    outcomes = {status: counts.get(status, 0) for status in finished_statuses}
    done = sum(outcomes.values())
    ok = outcomes['success']
    new7 = db.session.query(func.sum(ScrapeLog.new_count)).filter(
        ScrapeLog.started_at >= d7).scalar() or 0

    return jsonify({
        'users': User.query.count(),
        'admins': User.query.filter_by(role='admin').count(),
        'schools': School.query.count(),
        'schools_enabled': School.query.filter(School.enabled.is_(True)).count(),
        'schools_active': active_schools_query().count(),
        'subscriptions': Subscription.query.count(),
        'announcements': Announcement.query.count(),
        'announcements_24h': Announcement.query.filter(
            Announcement.created_at >= h24).count(),
        'scrapes_7d': done,
        'scrape_success_rate': round(ok / done, 4) if done else None,
        'scrape_outcomes_7d': outcomes,
        'scrape_new_7d': int(new7),
        'scrape_interval': AppConfig.get('scrape_interval', '30'),
        'open_registration': AppConfig.get('open_registration', '1'),
        'public_read': AppConfig.get('public_read', '1'),
        'recent_logs': [log.to_dict() for log in
                        ScrapeLog.query.filter(ScrapeLog.status.in_(finished_statuses))
                        .order_by(ScrapeLog.started_at.desc(), ScrapeLog.id.desc()).limit(10)],
    })


_TOGGLE_KEYS = ('open_registration', 'public_read')


@bp.route('/api/admin/toggles', methods=['GET', 'POST'])
@admin_required
def api_admin_toggles():
    """feature 开关：白名单两键，0/1 字符串，保存即生效"""
    if request.method == 'GET':
        return jsonify({k: AppConfig.get(k, '1') for k in _TOGGLE_KEYS})
    data = request.get_json(silent=True) or {}
    for key, val in data.items():
        if key not in _TOGGLE_KEYS:
            return jsonify({'error': f'未知开关: {key}'}), 400
        if str(val) not in ('0', '1'):
            return jsonify({'error': '开关值必须为 0 或 1'}), 400
        AppConfig.set(key, str(val))
    return jsonify({'success': True,
                    **{k: AppConfig.get(k, '1') for k in _TOGGLE_KEYS}})
